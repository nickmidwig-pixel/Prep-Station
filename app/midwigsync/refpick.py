"""Which onboard camera mic Auto-Align should align to, chosen by listening.

For each camera with sound, each channel is compared with the recorder's mics over a handful of clips spread
across the day (envelope correlation, searching ±1 s so small timecode errors don't matter):
  - silent channels and LTC (timecode) channels are skipped;
  - a channel that is really the mixer's feed (matches the recorder's mix almost exactly, at no delay) is not an
    onboard mic, so it's skipped too: aligning to a mix is never right;
  - of the rest, the channel that matches the mics best wins (ties go to the lower channel: ch 1 before 3/4).
The camera is the one that covers the most of the recorded sound, weighted by how well its best channel matches.
"""
import os
import numpy as np

from . import media
from . import tc as TC
from .wavesync import ENV_RATE, EnvelopeBuilder, feature, HOP

SR = TC.SR
FEED_CORR = 0.70       # this much of a camera channel rebuilt from the recorder's tracks = it's a feed, not a mic
SILENT_DB = -60.0


_WAIT = [None]        # set by pick_camera_reference: called as (clip name, seconds) while a read is taking long


def _waiter(item):
    f = _WAIT[0]
    return (lambda secs: f(item['name'], secs)) if f else None


def _env(item, ch, a, b, max_s):
    """Never gives up on a clip: a slow read is waited for, and the window says which clip and for how long."""
    return media.patiently(_env0, (item, ch, a, b, max_s), _waiter(item))


def _read(item, ch, a, n):
    return media.patiently(_read0, (item, ch, a, n), _waiter(item))


@media.timed('refpick.env')
def _env0(item, ch, a, b, max_s):
    """Envelope + mean level (dBFS) of one channel of a clip, from absolute sample a to b (capped at max_s)."""
    sr = media.audio_sr(item, ch)
    start_rel = max(0, a - item['start'])
    skip = int(start_rel * sr / SR)
    n = int(min(b - a, max_s * SR) * sr / SR)
    eb = EnvelopeBuilder(sr)
    got = 0
    pos = 0
    lvl = []
    pos = skip
    for o, blk in media.iter_audio(item, ch, max_samples=n, start=skip):
        if pos + len(blk) <= skip:
            pos += len(blk)
            continue
        if pos < skip:
            blk = blk[skip - pos:]
            pos = skip
        eb.feed(blk)
        lvl.append(float(np.mean(blk.astype(np.float64) ** 2)) if len(blk) else 0.0)
        got += len(blk)
        pos += len(blk)
        if got >= n:
            break
    e = feature(eb.result())
    db = 10 * np.log10(max(np.mean(lvl), 1e-12)) if lvl else -120.0
    return e, db


def _ncc(a, b, max_lag):
    """Best normalized correlation of two envelopes over lags of ±max_lag frames -> (corr, lag)."""
    n = min(len(a), len(b))
    if n < ENV_RATE * 4:
        return 0.0, 0
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    best, lag = -1.0, 0
    for L in range(-max_lag, max_lag + 1, 1):
        if L >= 0:
            x, y = a[L:], b[:n - L]
        else:
            x, y = a[:n + L], b[-L:]
        d = np.sqrt((x * x).sum() * (y * y).sum())
        if d <= 0:
            continue
        c = float((x * y).sum() / d)
        if c > best:
            best, lag = c, L
    return best, lag


@media.timed('refpick.read')
def _read0(item, ch, a, n):
    """n samples (48k) of one channel from absolute sample a (zeros where the clip has none)."""
    out = np.zeros(n, np.float32)
    rel = a - item['start']
    if item['kind'] == 'audio' and str(item['path']).lower().endswith(('.wav', '.bwf', '.rf64', '.w64')):
        from .bwf import WavInfo
        w = WavInfo(item['path'])
        if w.sr != SR:
            return out
        lo, hi = max(0, rel), min(w.nframes, rel + n)
        if hi > lo:
            x = w.read(int(lo), int(hi - lo))
            x = x[:, ch] if x.ndim == 2 else x
            out[lo - rel:hi - rel] = x
        return out
    if media.audio_sr(item, ch) != SR:
        return out
    pos = max(0, rel)
    for o, blk in media.iter_audio(item, ch, max_samples=n, start=pos):
        b0, b1 = pos, pos + len(blk)
        lo, hi = max(b0, rel), min(b1, rel + n)
        if hi > lo:
            out[lo - rel:hi - rel] = blk[lo - b0:hi - b0]
        pos = b1
        if pos >= rel + n:
            break
    return out


def feed_score(cam, ch, regs):
    """How much of a camera channel's waveform is a straight blend of the recorder's tracks (0..1).
    A wireless feed of the mix is (~0.9+); an onboard mic hears the room from feet away and isn't (~0.3-0.6)."""
    if not regs or not np.any(cam):
        return 0.0
    X = np.stack([r / (np.std(r) + 1e-9) for r in regs], 1)
    s = X.sum(1)
    n = len(cam)
    L = 1 << int(np.ceil(np.log2(2 * n)))
    xc = np.fft.irfft(np.fft.rfft(cam, L) * np.conj(np.fft.rfft(s, L)), L)
    maxlag = int(0.02 * SR)
    lags = np.r_[xc[:maxlag + 1], xc[-maxlag:]]
    k = int(np.argmax(np.abs(lags)))
    d = k if k <= maxlag else k - len(lags)
    Xd = np.roll(X, d, axis=0)
    g, *_ = np.linalg.lstsq(Xd, cam, rcond=None)
    res = cam - Xd @ g
    r2 = 1.0 - float(np.dot(res, res)) / max(float(np.dot(cam, cam)), 1e-12)
    return float(np.sqrt(max(0.0, r2)))


def _overlap(a0, a1, b0, b1):
    return max(0, min(a1, b1) - max(a0, b0))


def pick_camera_reference(project, log=print, cancel=None, max_clips=6, seconds=75, only_lane=None, progress=None):
    clips = [c for c in project.clips if c['include'] and c['start'] is not None and not c.get('slowmo')]
    cams = [c for c in clips if c['kind'] == 'video' and media.total_audio_channels(c) > 0]
    snd = [c for c in clips if c['kind'] == 'audio']
    if not cams:
        return None
    rolls = {}          # one entry per camera body (all its cards: the timeline lane), else per card
    for c in cams:
        rolls.setdefault(c.get('lane') or c['roll'], []).append(c)
    if only_lane is not None:       # you chose the camera; only its channels are listened to
        keep = {k for k in rolls if k == only_lane or any(c['roll'] == only_lane for c in rolls[k])}
        rolls = {k: v for k, v in rolls.items() if k in keep} or rolls
    from .scan import Project
    roles_of = lambda s: s.get('roles') or Project.clip_roles(s)

    def cover(roll_clips):
        return sum(_overlap(c['start'], c['start'] + c['length'], s['start'], s['start'] + s['length'])
                   for c in roll_clips for s in snd)

    def rep(key):
        """A card name of that camera (the one with the most sound under it); the job widens it to every card."""
        by = {}
        for c in rolls[key]:
            by.setdefault(c['roll'], []).append(c)
        return max(sorted(by), key=lambda r: cover(by[r]))

    order = sorted(rolls, key=lambda r: -cover(rolls[r]))
    if not snd:
        r = max(rolls, key=lambda r: sum(c['length'] for c in rolls[r]))
        return dict(roll=rep(r), channel=0, why='no sound files to compare with, so channel 1')
    results = []
    state = [0, 1]

    def waiting(name, secs):
        if progress:
            progress(state[0], state[1], f"Still reading {name} · {int(secs) // 60}:{int(secs) % 60:02d}")
    _WAIT[0] = waiting
    for roll in order[:3]:
        rc = sorted([c for c in rolls[roll] if any(_overlap(c['start'], c['start'] + c['length'], s['start'],
                                                             s['start'] + s['length']) > 8 * SR for s in snd)],
                    key=lambda c: c['start'])
        if not rc:
            continue
        pick = [rc[int(i)] for i in np.linspace(0, len(rc) - 1, min(max_clips, len(rc)))]
        nch = max(media.total_audio_channels(c) for c in pick)
        stats = {ch: dict(corr=[], feed=[], db=[]) for ch in range(min(nch, 8))}
        for k_, c in enumerate(pick):
            if cancel and cancel():
                raise KeyboardInterrupt
            if progress:
                state[0], state[1] = order[:3].index(roll) * max_clips + k_, min(3, len(order)) * max_clips
                progress(state[0], state[1], f"Listening to {c['name']}")
            # the recorder file that overlaps this clip the most
            s = max(snd, key=lambda s: _overlap(c['start'], c['start'] + c['length'], s['start'], s['start'] + s['length']))
            a = max(c['start'], s['start'])
            b = min(c['start'] + c['length'], s['start'] + s['length'])
            if b - a < 8 * SR:
                continue
            roles = roles_of(s)
            mic = [i for i, r in enumerate(roles) if r in ('boom', 'plant', 'lav')]
            mix = [i for i, r in enumerate(roles) if r == 'mix']
            try:
                mic_env = None
                for i in mic[:3]:
                    e, db = _env(s, i, a, b, seconds)
                    if db > SILENT_DB:
                        mic_env = e if mic_env is None else np.maximum(mic_env[:len(e)], e[:len(mic_env)])
                mix_env = _env(s, mix[0], a, b, seconds)[0] if mix else None
            except Exception:  # noqa: BLE001
                continue
            if mic_env is None and mix_env is None:
                continue
            # a 20 s stretch with speech, for the waveform-level feed test (every recorder track overlapping it)
            win_n = int(20 * SR)
            if mic_env is not None and len(mic_env) > 20 * ENV_RATE:
                k = 20 * ENV_RATE
                cs = np.convolve(mic_env - mic_env.min(), np.ones(k), 'valid')
                w0 = a + int(np.argmax(cs)) * HOP
            else:
                w0 = a
            w0 = min(w0, max(a, b - win_n))
            regs = []
            for s2 in snd:
                if _overlap(w0, w0 + win_n, s2['start'], s2['start'] + s2['length']) <= 0:
                    continue
                for i, r in enumerate(roles_of(s2)):
                    if r in ('boom', 'plant', 'lav', 'mix'):
                        try:
                            x = _read(s2, i, w0, win_n)
                        except Exception:  # noqa: BLE001
                            continue
                        if np.any(x):
                            regs.append(x)
            ltc_ch = (c.get('ltc') or {}).get('channel')
            for ch in stats:
                if ch >= media.total_audio_channels(c) or ch == ltc_ch:
                    continue
                try:
                    e, db = _env(c, ch, a, b, seconds)
                except Exception:  # noqa: BLE001
                    continue
                stats[ch]['db'].append(db)
                if db <= SILENT_DB:
                    continue
                if mic_env is not None:
                    stats[ch]['corr'].append(_ncc(e, mic_env, ENV_RATE)[0])
                try:
                    stats[ch]['feed'].append(feed_score(_read(c, ch, w0, win_n), ch, regs))
                except Exception:  # noqa: BLE001
                    pass
        rows = []
        for ch, st in stats.items():
            if not st['db']:
                continue
            db = float(np.median(st['db']))
            corr = float(np.median(st['corr'])) if st['corr'] else 0.0
            feed = float(np.median(st['feed'])) if st['feed'] else 0.0
            what = 'silent' if db <= SILENT_DB else ('mixer feed' if feed >= FEED_CORR else 'mic')
            rows.append(dict(ch=ch, db=round(db, 1), corr=round(corr, 3), feed=round(feed, 3), what=what))
        mics = [r for r in rows if r['what'] == 'mic']
        if not mics:
            continue
        top = max(r['corr'] for r in mics)
        best = min((r for r in mics if r['corr'] >= top - 0.03), key=lambda r: r['ch'])   # near-tie: lower channel
        results.append(dict(roll=rep(roll), ch=best['ch'], corr=best['corr'], cover=cover(rolls[roll]), rows=rows))
        log(f"  {rep(roll)}{' (+%d cards)' % (len({c['roll'] for c in rolls[roll]}) - 1) if len({c['roll'] for c in rolls[roll]}) > 1 else ''}: " + ', '.join(f"ch{r['ch'] + 1} {r['what']} {r['corr']:.2f}" + (f" (feed {r['feed']:.2f})" if r['what'] == 'mixer feed' else '')
                                       for r in rows))
    if not results:
        r = order[0]
        return dict(roll=rep(r), channel=0, why='no camera channel matched the sound clearly, so channel 1')
    tot = max(x['cover'] for x in results) or 1
    win = max(results, key=lambda x: (x['cover'] / tot) * max(x['corr'], 0.05))
    why = f"onboard mic that matches the recorder's mics best ({win['corr']:.2f})"
    feeds = [r['ch'] + 1 for r in win['rows'] if r['what'] == 'mixer feed']
    if feeds:
        why += f"; ch {', '.join(map(str, feeds))} {'is' if len(feeds) == 1 else 'are'} the mixer's feed, not used"
    silent = [r['ch'] + 1 for r in win['rows'] if r['what'] == 'silent']
    if silent:
        why += f"; ch {', '.join(map(str, silent))} silent"
    win['ch'] = win['ch']
    return dict(roll=win['roll'], channel=win['ch'], why=why, table=results)
