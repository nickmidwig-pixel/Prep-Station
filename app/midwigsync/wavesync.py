"""Waveform sync: place files that have no / bad timecode, and verify every roll's timecode.

Coarse: high-passed log-energy envelopes at 200 Hz, normalized cross-correlation against a
day-long "bed" per roll. Fine: band-passed waveform NCC (+/-30 ms) at 48 kHz.
"""
import os
import hashlib
from collections import defaultdict
import numpy as np

from . import tc as TC
from . import media
from .bwf import WavInfo

ENV_RATE = 200
HOP = TC.SR // ENV_RATE          # 240 samples @48k
TOL_SAME = 3                     # frames: rolls within this are "in sync" (AAP handles the rest)
FIX_THRESHOLD_S = 0.20           # bigger than this -> treat as a TC error and correct it
MIN_SCORE = 0.30
MIN_MARGIN = 1.25


# ---------------------------------------------------------------- features
class EnvelopeBuilder:
    """Streaming envelope: feed float blocks at sample rate sr."""

    def __init__(self, sr):
        self.sr = sr
        self.hop = max(1, int(round(sr / ENV_RATE)))
        self.scale = self.hop * ENV_RATE / sr
        self.carry = np.zeros(0, np.float32)
        self.prev = 0.0
        self.out = []

    def feed(self, x):
        x = np.asarray(x, np.float32)
        if not len(x):
            return
        y = np.empty_like(x)
        y[0] = x[0] - 0.95 * self.prev
        y[1:] = x[1:] - 0.95 * x[:-1]
        self.prev = float(x[-1])
        y = np.concatenate([self.carry, y])
        n = len(y) // self.hop
        if n:
            e = (y[:n * self.hop].reshape(n, self.hop).astype(np.float64) ** 2).mean(1)
            self.out.append(e.astype(np.float32))
        self.carry = y[n * self.hop:]

    def result(self):
        e = np.concatenate(self.out) if self.out else np.zeros(0, np.float32)
        if abs(self.scale - 1) > 1e-6 and len(e) > 1:  # non-48k-friendly rates: resample time axis
            t = np.arange(int(len(e) * self.scale)) / self.scale
            e = np.interp(t, np.arange(len(e)), e).astype(np.float32)
        return e


def feature(energy):
    """Energy per hop -> zero-mean syllable-structure feature (robust across different mics)."""
    if len(energy) < 10:
        return np.zeros(len(energy), np.float32)
    db = 10 * np.log10(energy.astype(np.float64) + 1e-12)
    floor = np.percentile(db, 15)
    db = np.maximum(db, floor + 3)
    k = int(0.4 * ENV_RATE)
    ker = np.ones(k) / k
    pad = np.pad(db, (k // 2, k - k // 2 - 1), mode='edge')
    hp = db - np.convolve(pad, ker, mode='valid')
    hp = np.clip(hp, -20, 20)
    s = hp.std()
    return (hp / s).astype(np.float32) if s > 1e-6 else np.zeros(len(hp), np.float32)


def item_envelope(item, channel=0, max_seconds=None, cache_dir=None, sink=None):
    """Envelope (feature array at ENV_RATE) of an item's audio from the file start, plus the audio's
    start offset (48k samples) relative to the clip start. `sink(block, sr)` receives raw audio."""
    key = None
    if cache_dir and sink is None:
        st = os.stat(item['path'])
        key = hashlib.sha1(f"{item['path']}|{st.st_size}|{st.st_mtime}|{channel}|{max_seconds}".encode()).hexdigest()
        cp = os.path.join(cache_dir, key + '.npz')
        if os.path.exists(cp):
            d = np.load(cp)
            return d['env'], int(d['off'])
    sr = media.audio_sr(item, channel)
    eb = EnvelopeBuilder(sr)
    off = 0
    maxn = int(max_seconds * sr) if max_seconds else None
    for o, blk in media.iter_audio(item, channel, max_samples=maxn):
        if o is not None:
            off = int(round(o * TC.SR / sr))
        eb.feed(blk)
        if sink:
            sink(blk, sr)
    env = feature(eb.result())
    if key:
        os.makedirs(cache_dir, exist_ok=True)
        np.savez(os.path.join(cache_dir, key + '.npz'), env=env, off=off)
    return env, off


def ref_rolls(ref):
    """Every card (roll) of the reference camera body; older settings only name one."""
    return ref.get('rolls') or [ref['roll']]


# ---------------------------------------------------------------- beds
class Bed:
    """A roll's envelopes laid out on the day timeline."""

    def __init__(self, origin, n):
        self.origin = origin          # samples @48k, multiple of HOP
        self.env = np.zeros(n, np.float32)
        self.mask = np.zeros(n, np.float32)
        self.owner = np.full(n, -1, np.int32)   # clip id per hop

    def add(self, start, env, cid):
        i0 = int(round((start - self.origin) / HOP))
        a, b = max(i0, 0), min(i0 + len(env), len(self.env))
        if b <= a:
            return
        self.env[a:b] = env[a - i0:b - i0]
        self.mask[a:b] = 1.0
        self.owner[a:b] = cid

    def clip_at(self, sample):
        i = int((sample - self.origin) // HOP)
        if 0 <= i < len(self.owner):
            return int(self.owner[i])
        return -1


def _fft_corr(a, b, n):
    return np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)


def ncc_search(target, bed, lag_lo=None, lag_hi=None, min_overlap_s=8.0):
    """Normalized cross-correlation of target against bed. Lag L means target[0] sits at bed index L.
    Returns (lag, score, margin, overlap_fraction) or None."""
    T = target.astype(np.float64)
    Lt = len(T)
    if Lt < ENV_RATE * 3:
        return None
    if lag_lo is None:
        lag_lo, lag_hi = -Lt + int(min_overlap_s * ENV_RATE), len(bed.env) - int(min_overlap_s * ENV_RATE)
    lag_lo, lag_hi = int(lag_lo), int(lag_hi)
    if lag_hi <= lag_lo:
        return None
    # slice the part of the bed that can be touched
    s0, s1 = max(lag_lo, 0), min(lag_hi + Lt, len(bed.env))
    if s1 - s0 < ENV_RATE * 3:
        return None
    B = bed.env[s0:s1].astype(np.float64)
    M = bed.mask[s0:s1].astype(np.float64)
    n = 1 << int(np.ceil(np.log2(len(B) + Lt + 1)))
    num = _fft_corr(B, T, n)                 # num[k] = sum_j B[k+j] T[j]
    tm = _fft_corr(M, T * T, n)              # target energy over the masked overlap
    bm = _fft_corr(B * B, np.ones(Lt), n)    # bed energy in window
    cov = _fft_corr(M, np.ones(Lt), n)       # overlap length (hops)
    ks = np.arange(lag_lo, lag_hi + 1)
    idx = (ks - s0) % n
    num, tm, bm, cov = num[idx], tm[idx], bm[idx], cov[idx]
    need = min(min_overlap_s * ENV_RATE, 0.5 * Lt)
    den = np.sqrt(np.maximum(tm, 1e-9) * np.maximum(bm, 1e-9))
    score = np.where(cov >= need, num / den, 0.0)
    if not np.any(score > 0):
        return None
    k = int(np.argmax(score))
    best = float(score[k])
    excl = int(1.0 * ENV_RATE)
    other = score.copy()
    other[max(0, k - excl):k + excl + 1] = -1
    second = float(other.max()) if len(other) > 2 * excl else 0.0
    margin = best / max(second, 0.05)
    return int(ks[k]), best, margin, float(cov[k] / Lt)


# ---------------------------------------------------------------- refine
def _bandpass(x, lo=150, hi=5000, sr=TC.SR):
    n = len(x)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / sr)
    X[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(X, n)


def read_abs(item, start_abs, count, channel=0, max_decode_s=240):
    """Audio of item at absolute day time [start_abs, start_abs+count), 48k mono float."""
    rel = start_abs - item['start']
    if item['kind'] == 'audio':
        w = WavInfo(item['path'])
        if w.sr != TC.SR:
            return None
        ch = 0 if channel == 'mix' else min(channel, w.channels - 1)
        return w.read(rel, count, ch).astype(np.float64)
    if rel + count > max_decode_s * TC.SR or rel < -count:
        return None
    buf, got, off = [], 0, 0
    for o, blk in media.iter_audio(item, channel, max_samples=max(0, rel + count + TC.SR)):
        if o is not None:
            off = o
        buf.append(blk)
        got += len(blk)
    x = np.concatenate(buf) if buf else np.zeros(0, np.float32)
    out = np.zeros(count)
    a = rel - off
    lo, hi = max(a, 0), min(a + count, len(x))
    if hi > lo:
        out[lo - a:hi - a] = x[lo:hi]
    return out


def refine(target, t_start, target_env, cand, cand_channel=0, target_channel=0, search_ms=30, win_s=6.0,
           max_target_s=90, target_off=0):
    """Sample-accurate refinement. Returns (delta_samples, |cc|) to add to t_start, or (0, 0)."""
    W = int(win_s * TC.SR)
    R = int(search_ms * TC.SR / 1000)
    lim = len(target_env)
    if target['kind'] == 'video':
        lim = min(lim, int(max_target_s * ENV_RATE))
    # candidate coverage limits
    c0 = cand['start'] - t_start
    c1 = c0 + cand['length']
    lo_h = max(0, int(c0 / HOP) + ENV_RATE)
    hi_h = min(lim, int(c1 / HOP) - ENV_RATE) - int(win_s * ENV_RATE)
    if hi_h <= lo_h:
        return 0, 0.0
    e = target_env[lo_h:hi_h + int(win_s * ENV_RATE)]
    if not len(e):
        return 0, 0.0
    k = int(win_s * ENV_RATE)
    act = np.convolve(np.abs(e), np.ones(k), mode='valid')
    w0h = lo_h + int(np.argmax(act))
    w0 = w0h * HOP
    t_item = dict(target, start=0)
    x = read_abs(t_item, w0 + target_off, W, target_channel)
    y = read_abs(cand, t_start + target_off + w0 - R, W + 2 * R, cand_channel)
    if x is None or y is None or not np.any(x) or not np.any(y):
        return 0, 0.0
    x, y = _bandpass(x), _bandpass(y)
    n = 1 << int(np.ceil(np.log2(len(y) + len(x))))
    c = _fft_corr(y, x, n)[:2 * R + 1]
    yy = np.concatenate([[0], np.cumsum(y * y)])
    ey = yy[W:W + 2 * R + 1] - yy[:2 * R + 1]
    cc = c / np.sqrt((x * x).sum() * np.maximum(ey, 1e-12))
    j = int(np.argmax(np.abs(cc)))
    return j - R, float(abs(cc[j]))


def robust_center(vals, tol):
    """Median of the biggest group of values that agree within tol (ties -> the group nearest zero)."""
    v = np.sort(np.asarray(vals, np.float64))
    best = None
    for x in v:
        g = v[np.abs(v - x) <= tol]
        key = (len(g), -abs(float(np.median(g))))
        if best is None or key > best[0]:
            best = (key, float(np.median(g)))
    return best[1]


# ---------------------------------------------------------------- the whole-day analysis
class SyncAnalyzer:
    def __init__(self, project, cache_dir=None, log=print, progress=None, ref=None, ref_sink_factory=None,
                 cam_check_s=180, cancel=None):
        self.p = project
        self.cache = cache_dir
        self.log = log
        self.progress = progress or (lambda *a: None)
        self.ref = ref or project.default_reference()
        self.ref_sink_factory = ref_sink_factory
        self.cam_check_s = cam_check_s
        self.env = {}       # clip id -> (env, audio_offset)
        self.beds = {}
        self.cancel = cancel or (lambda: False)
        self.results = []

    def _trusted_tc(self, c):
        if c.get('manual'):
            return True
        return c['orig_start'] is not None and not ({'no_tc', 'zero_tc', 'tc_far'} & set(c['flags']))

    @staticmethod
    def _claimed(c):
        """Where the clip says it is: a position set by hand wins over the file's own timecode."""
        return c['manual']['start'] if c.get('manual') else c['orig_start']

    def _ch(self, c):
        """Channel to listen to: the reference channel on the reference roll, else the first channel that
        isn't timecode-as-audio (LTC). None = nothing usable."""
        if self.ref and c['kind'] == 'video' and c['roll'] in ref_rolls(self.ref):
            return self.ref['channel']
        if c.get('ltc'):
            return c.get('listen_ch')
        return 0

    def envelopes(self, workers=None):
        """Listen to every clip. Several at once (reading one file while another is being worked out); each clip's
        result is the same as listening one by one. PREP_LISTEN_THREADS=1 listens one at a time."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        todo = [c for c in self.p.clips if c['include'] and media.total_audio_channels(c) > 0]
        workers = workers or int(os.environ.get('PREP_LISTEN_THREADS', '0') or 0) or min(3, os.cpu_count() or 2)

        def one(c):
            if self.cancel():
                return c, None
            is_ref = self.ref and c['kind'] == 'video' and c['roll'] in ref_rolls(self.ref)
            ch = self._ch(c)
            if ch is None:
                return c, None
            sink = self.ref_sink_factory(c) if (is_ref and self.ref_sink_factory) else None
            maxs = None
            if c['kind'] == 'video' and not is_ref:
                maxs = None if not self._trusted_tc(c) else self.cam_check_s
            try:
                env, off = item_envelope(c, ch, maxs, self.cache, sink=sink)
                if sink:
                    sink.close(off)
            except Exception as e:  # noqa: BLE001
                self.log(f"  could not read audio of {c['name']}: {e}")
                if sink:
                    sink.abort()
                return c, None
            return c, (env, off)

        self.progress(0, len(todo), 'Listening')
        if workers <= 1:
            for i, c in enumerate(todo):
                if self.cancel():
                    raise KeyboardInterrupt
                self.progress(i, len(todo), f"Listening: {c['name']}")
                c, r = one(c)
                if r is not None:
                    self.env[c['id']] = r
        else:
            ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='listen')
            try:
                futs = [ex.submit(one, c) for c in todo]
                for k, f in enumerate(as_completed(futs)):
                    c, r = f.result()
                    if r is not None:
                        self.env[c['id']] = r
                    self.progress(k + 1, len(todo), f"Listening: {c['name']}")
                    if self.cancel():
                        raise KeyboardInterrupt
            finally:
                ex.shutdown(wait=True, cancel_futures=True)
        if self.cancel():
            raise KeyboardInterrupt
        self.progress(len(todo), len(todo), 'Listening done')

    def build_beds(self, trusted_rolls=None, corrected=None):
        span = self.p.day_span()
        if not span:
            return
        corrected = corrected or {}
        origin = (span[0] // HOP - 600 * ENV_RATE) * HOP
        n = int((span[1] - origin) // HOP + 600 * ENV_RATE)
        beds = {}
        for c in self.p.clips:
            if c['id'] not in self.env or not c['include']:
                continue
            if c['id'] in corrected:
                start = corrected[c['id']]
            elif (trusted_rolls is None or c['roll'] in trusted_rolls or c.get('manual')) and self._trusted_tc(c) \
                    and ('tc_overlap' not in c['flags'] or c.get('manual')):
                start = self._claimed(c)
            else:
                continue
            env, off = self.env[c['id']]
            b = beds.get(c['roll'])
            if b is None:
                b = beds[c['roll']] = Bed(origin, n)
            b.add(start + off, env, c['id'])
        self.beds = beds

    def coverage(self, c, start, rolls):
        """Fraction of clip c (placed at start) during which any of `rolls` was recording."""
        tot = np.zeros(max(1, c['length'] // HOP), np.float32)
        for r in rolls:
            b = self.beds.get(r)
            if b is None or r == c['roll']:
                continue
            i0 = int((start - b.origin) // HOP)
            a, e = max(i0, 0), min(i0 + len(tot), len(b.mask))
            if e > a:
                tot[a - i0:e - i0] = np.maximum(tot[a - i0:e - i0], b.mask[a:e])
        return float(tot.mean())

    def measure(self, c, roll, start_guess=None, window_s=90):
        """Offset (samples) of clip c vs roll's bed: true_start = claimed + offset."""
        bed = self.beds.get(roll)
        if bed is None or c['id'] not in self.env:
            return None
        env, off = self.env[c['id']]
        if start_guess is not None:
            L0 = int(round((start_guess + off - bed.origin) / HOP))
            r = ncc_search(env, bed, L0 - window_s * ENV_RATE, L0 + window_s * ENV_RATE)
            if r is None:
                return None
            lag, score, margin, cov = r
            return dict(offset=(lag - L0) * HOP, score=score, margin=margin, cov=cov, roll=roll)
        r = ncc_search(env, bed)
        if r is None:
            return None
        lag, score, margin, cov = r
        return dict(start=bed.origin + lag * HOP - off, score=score, margin=margin, cov=cov, roll=roll)

    @staticmethod
    def confident(m, strict=False):
        if not m:
            return False
        return m['score'] >= (0.4 if strict else MIN_SCORE) and m['margin'] >= (1.5 if strict else MIN_MARGIN)

    def run(self):
        p = self.p
        spf = float(TC.samples_per_frame(p.fps))
        self.envelopes()
        self.build_beds()
        rolls = sorted(self.beds)
        # ---- every trusted clip vs every other roll, near its claimed TC
        pair = defaultdict(list)
        per_clip = defaultdict(dict)
        tc_clips = [c for c in p.clips if c['id'] in self.env and self._trusted_tc(c) and c['include']
                    and 'tc_overlap' not in c['flags'] and not c.get('manual')]
        for i, c in enumerate(tc_clips):
            if self.cancel():
                raise KeyboardInterrupt
            self.progress(i, len(tc_clips), f"Checking timecode: {c['name']}")
            for r in rolls:
                if r == c['roll']:
                    continue
                m = self.measure(c, r, c['orig_start'], window_s=120)
                if self.confident(m):
                    m['start'] = c['orig_start'] + m['offset']
                    per_clip[c['id']][r] = m
                    pair[(c['roll'], r)].append(m['offset'])
        med = {k: robust_center(v, TOL_SAME * spf) for k, v in pair.items()}

        def rel(a, b):  # how far roll a's content sits from roll b's (a needs +rel to line up with b)
            if (a, b) in med:
                return med[(a, b)]
            if (b, a) in med:
                return -med[(b, a)]
            return None

        all_rolls = sorted({c['roll'] for c in p.clips})
        agree = {a: [b for b in all_rolls if b != a and rel(a, b) is not None and abs(rel(a, b)) <= TOL_SAME * spf]
                 for a in all_rolls}
        ref_set = set(ref_rolls(self.ref)) if self.ref else set()
        anchor = max(all_rolls, key=lambda a: (len(agree[a]), a in ref_set,
                                               sum(1 for c in p.clips if c['roll'] == a))) if all_rolls else None
        cluster = ({anchor} | set(agree.get(anchor, []))) if anchor else set()
        shift = {}
        for r in cluster:
            shift[r] = 0.0 if r == anchor else (rel(r, anchor) or 0.0)
        report = dict(anchor=anchor, cluster=sorted(cluster), clock_ms={r: round(v / 48, 1) for r, v in shift.items()},
                      roll_offsets={}, clips={})
        for a in all_rolls:
            vals = [rel(a, b) + shift[b] for b in cluster if b != a and rel(a, b) is not None]
            report['roll_offsets'][a] = 0.0 if a in cluster else (float(np.median(vals)) if vals else None)
        if len(cluster) < len(all_rolls):
            self.log(f"  Timecode agrees across: {', '.join(sorted(cluster))}")

        def to_own(c, start_in_r, r):
            """Position measured in roll r's clock -> clip c's own clock (anchor clock if its roll is off)."""
            return start_in_r + shift.get(r, 0.0) - shift.get(c['roll'], 0.0)

        moves = {}   # clip id -> dict(start, roll, score, method, ...)
        for c in p.clips:
            c['sync'] = None
            if c.get('manual'):
                c['start'] = c['manual']['start']
        for c in tc_clips:
            ms = [m for r, m in per_clip[c['id']].items() if r in cluster]
            if not ms and self.coverage(c, c['orig_start'], cluster) > 0.5:
                # others were rolling but nothing matched near the claimed TC: maybe a missed jam -> search the day
                g = [self.measure(c, r) for r in cluster if r != c['roll']]
                g = [m for m in g if self.confident(m, strict=True)]
                if g:
                    best = max(g, key=lambda m: m['score'])
                    st = to_own(c, best['start'], best['roll'])
                    if abs(st - c['orig_start']) > FIX_THRESHOLD_S * TC.SR:
                        moves[c['id']] = dict(start=st, raw=best['start'], roll=best['roll'], score=best['score'],
                                              method='tc_check', note='found by full-day search')
                continue
            if not ms:
                if c['roll'] not in cluster and report['roll_offsets'].get(c['roll']) is not None:
                    off = report['roll_offsets'][c['roll']]
                    if abs(off) > FIX_THRESHOLD_S * TC.SR:
                        moves[c['id']] = dict(start=c['orig_start'] + off, raw=None, roll=None, score=None,
                                              method='tc_check', note='roll median')
                continue
            starts = [to_own(c, m['start'], m['roll']) for m in ms]
            st = float(np.median(starts))
            off = st - c['orig_start']
            best = max(ms, key=lambda m: m['score'])
            if abs(off) > FIX_THRESHOLD_S * TC.SR and (self.confident(best, strict=True) or len(ms) >= 2):
                moves[c['id']] = dict(start=st, raw=best['start'], roll=best['roll'], score=best['score'],
                                      method='tc_check')
            elif abs(off) > TOL_SAME * spf:
                c['sync'] = dict(method='tc_check', offset=int(round(off)), score=round(best['score'], 2),
                                 note='small offset left for Auto-Align')
        # ---- clips with no usable TC: search the whole day against the trusted rolls
        self.build_beds(trusted_rolls=cluster, corrected={k: int(v['start']) for k, v in moves.items()})
        lost = [c for c in p.clips if c['include'] and c['id'] in self.env and c['id'] not in moves and
                not c.get('manual') and (not self._trusted_tc(c) or 'tc_overlap' in c['flags'])]
        for i, c in enumerate(lost):
            if self.cancel():
                raise KeyboardInterrupt
            self.progress(i, len(lost), f"Finding by waveform: {c['name']}")
            cands = []
            for r in self.beds:
                if r not in cluster:
                    continue
                if 'tc_overlap' in c['flags'] and c['orig_start'] is not None:
                    m = self.measure(c, r, c['orig_start'], window_s=600)
                    if m:
                        m['start'] = c['orig_start'] + m['offset']
                else:
                    m = self.measure(c, r)
                if self.confident(m):
                    cands.append(m)
            if not cands:
                c['sync'] = dict(method='waveform', found=False)
                if 'sync_failed' not in c['flags']:
                    c['flags'].append('sync_failed')
                continue
            best = max(cands, key=lambda m: m['score'])
            agree_n = sum(1 for m in cands if abs(to_own(c, m['start'], m['roll']) - to_own(c, best['start'], best['roll']))
                          < 0.1 * TC.SR)
            moves[c['id']] = dict(start=to_own(c, best['start'], best['roll']), raw=best['start'], roll=best['roll'],
                                  score=best['score'], margin=best['margin'], agree=agree_n, method='waveform')
        # ---- sample-accurate refinement against the roll that matched best
        for cid, mv in moves.items():
            c = p.by_id(cid)
            env, off = self.env[cid]
            info = dict(method=mv['method'], score=round(mv['score'], 2) if mv['score'] else None,
                        against=mv['roll'] or 'roll median')
            for k in ('note', 'margin', 'agree'):
                if k in mv:
                    info[k] = round(mv[k], 2) if isinstance(mv[k], float) else mv[k]
            if mv['raw'] is not None:
                cand = self._refine_candidate(c, int(mv['raw']), mv['roll'])
                if cand is not None:
                    cch = self._ch(cand) or 0
                    tch = self._ch(c) or 0
                    try:
                        d, cc = refine(c, int(mv['raw']), env, cand, cand_channel=cch, target_channel=tch, target_off=off)
                    except Exception as e:  # noqa: BLE001
                        self.log(f'  fine sync failed for {c["name"]}: {e}')
                        d, cc = 0, 0.0
                    if cc >= 0.2:
                        mv['start'] = to_own(c, mv['raw'] + d, mv['roll'])
                        info['fine_cc'] = round(cc, 2)
                        info['fine_vs'] = cand['name']
            c['start'] = int(round(mv['start']))
            if c['orig_start'] is not None:
                info['offset'] = c['start'] - c['orig_start']
            c['sync'] = info
            fl = 'sync_corrected' if c['orig_start'] is not None else 'sync_found'
            if fl not in c['flags']:
                c['flags'].append(fl)
            if 'sync_failed' in c['flags']:
                c['flags'].remove('sync_failed')
        report['moved'] = {p.by_id(k)['name']: p.by_id(k)['sync'] for k in moves}
        self.results = report
        return report

    def _refine_candidate(self, c, start, roll):
        """Clip of `roll` (trusted TC) overlapping c placed at `start` (in that roll's clock) the most."""
        best, best_ov = None, 0
        for o in self.p.clips:
            if o['id'] == c['id'] or o['roll'] != roll or not o['include'] or o['start'] is None:
                continue
            if not self._trusted_tc(o) or 'sync_corrected' in o['flags'] or \
                    ('tc_overlap' in o['flags'] and not o.get('manual')):
                continue
            if self._ch(o) is None:
                continue
            ov = min(start + c['length'], o['start'] + o['length']) - max(start, o['start'])
            if ov >= 10 * TC.SR and ov > best_ov:
                best, best_ov = o, ov
        return best
