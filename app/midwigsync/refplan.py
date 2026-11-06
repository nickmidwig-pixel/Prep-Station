"""Recorder tracks: what each channel is, splitting multichannel files into mono tracks for Auto-Align and
packing the aligned tracks back, and (audio-only) choosing the reference mic for each stretch of the day.

Roles come from track names (iXML / bext sTRK, or the recorder folder name like 02LAV, 06BOOM):
  boom, plant, lav (also any person's name), mix (MIX, L/R, X1...), tc (LTC / timecode), empty (no signal).
Only boom / plant / lav tracks are aligned; mix, timecode and empty tracks pass through untouched.

Reference choice (audio-only): for every stretch, the boom if one is rolling with signal (the louder of two),
else a plant, else the lav that hears everyone best (highest envelope correlation with the other mics).
The choice only changes where the set of rolling files changes, and sticks with the previous mic if it can.
"""
import os
import re
import numpy as np

from . import tc as TC
from .bwf import WavInfo, WavWriter, build_bext, build_ixml, wav_header

SR = TC.SR
ALIGNABLE = ('boom', 'plant', 'lav')
PRIORITY = {'boom': 0, 'plant': 1, 'lav': 2}

_PATTERNS = [
    ('tc', r'\b(ltc|smpte|t/?c|timecode|time ?code)\b'),
    ('mix', r'\b(mix|mx|mixl|mixr|mix[ _-]?[lr]|x\d|stereo|main|pgm|program)\b'),
    ('boom', r'(boom|\bbm\d*\b|shotgun|\bshot\b|\b416\b|\b8060\b|\b8050\b|cmit|schoeps|mkh|\bpole\b|overhead|\boh\b|\bcmc\b)'),
    ('plant', r'(plant|\bpzm\b|\bhide\b)'),
    ('lav', r'(lav|lavalier|\brf\d*\b|\bwl\d*\b|wireless|radio|\bcos\b|\bdpa\b|\bb6\b|\btram\b|sanken|countryman)'),
]


def role_of(name):
    n = re.sub(r'[_\-.]+', ' ', str(name or '')).strip().lower()
    n = re.sub(r'^\d+\s*', '', n) if re.match(r'^\d+\s*[a-z]', n) else n   # "02LAV" -> "lav"
    if n in ('l', 'r', 'lr', 'left', 'right', 'mix l', 'mix r'):
        return 'mix'
    for role, pat in _PATTERNS:
        if re.search(pat, n):
            return role
    return 'lav'           # a person's name, "Jane", "ISO 3"... is a body mic


def _level_db(w, ch, n_windows=24, win_s=1.0):
    """Loudest 1-second RMS (dBFS) over a handful of spots through the file."""
    n = w.nframes
    W = int(win_s * w.sr)
    if n <= W:
        x = w.read(0, n, ch)
        return float(20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2) + 1e-20)))
    best = -200.0
    with open(w.path, 'rb') as fh:
        for k in range(n_windows):
            t = int((n - W) * (k + 0.5) / n_windows)
            x = w.read(t, W, ch, fh).astype(np.float64)
            best = max(best, float(20 * np.log10(np.sqrt(np.mean(x ** 2) + 1e-20))))
    return best


def inventory(source, ltc_channel=None):
    """[dict(ch, name, role, level_db)] for every channel of a recorder file."""
    w = WavInfo(source['path'])
    names = w.track_names()
    out = []
    for k in range(w.channels):
        nm = names.get(k + 1) or (source['roll'] if w.channels == 1 else f"{source['roll']}_{k + 1}")
        role = role_of(nm)
        if w.channels == 1 and role == 'lav' and not names.get(1):
            role = role_of(source['roll'])
        if ltc_channel is not None and k == ltc_channel:
            role = 'tc'
        lvl = _level_db(w, k)
        if role != 'tc' and lvl < -65:
            role = 'empty'
        out.append(dict(ch=k, name=nm, role=role, level_db=round(lvl, 1)))
    return out


def _safe(s):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', str(s)).strip('_') or 'ch'


def write_mono(src_path, ch, out_path, start, fps, df, track_name, roll):
    """Bit-exact copy of one channel of a WAV into a mono BWF with the same start timecode."""
    w = WavInfo(src_path)
    bps = w.block_align // w.channels
    tcs = TC.samples_to_tc(start, fps, df)
    tr = int(round(TC.wrap(start, fps) * w.sr / SR))
    desc = f'sTRK1={track_name}\r\ndTC={tcs}\r\ndFRAMERATE={TC.rate_label(fps)}{"DF" if df else "ND"}\r\n'
    be = build_bext(desc, None, roll[:32], '', '', tr, f'A=PCM,F={w.sr},W={w.bits},M=mono,T=ch {ch + 1} of {os.path.basename(src_path)}\r\n')
    fr = TC.parse_rate(fps)
    ix = build_ixml(roll, TC.rate_label(fr), f'{fr.numerator}/{fr.denominator}', df, tr, w.sr, w.bits, [track_name])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + '.partial'
    blk = 10 * w.sr
    with open(src_path, 'rb') as fh, open(tmp, 'wb') as dst:
        dst.write(wav_header(w.nframes, 1, w.sr, w.bits, w.format_tag if w.format_tag in (1, 3) else 1, be, ix))
        for s0 in range(0, w.nframes, blk):
            n = min(blk, w.nframes - s0)
            raw = w.read_raw(s0, n, fh)
            a = np.frombuffer(raw, np.uint8)[:n * w.block_align].reshape(n, w.block_align)
            dst.write(a[:, ch * bps:(ch + 1) * bps].tobytes())
        if (w.nframes * bps) & 1:
            dst.write(b'\0')
    os.replace(tmp, out_path)
    return out_path


def split_sources(sources, work_dir, fps, df, log=print, ltc=None):
    """sources: [dict(path, name, start, length, roll, channels, clip)] ->
    (monos to align, packs to rebuild multichannel files, pass-through files).
    Mono originals are used as they are; multichannel files are split into WORK/split/<roll>/...WAV."""
    monos, packs, passthru = [], [], []
    for s in sources:
        lt = (s.get('clip') or {}).get('ltc')
        inv = inventory(s, lt['channel'] if lt else None)
        s['inventory'] = inv
        if s['channels'] == 1:
            role = inv[0]['role']
            if role in ALIGNABLE:
                monos.append(dict(s, role=role, track=s['roll'], ch=0, level_db=inv[0]['level_db'], split=False))
            else:
                passthru.append(dict(s, role=role))
            continue
        pack = dict(source=s, inv=inv, monos={})
        stem = os.path.splitext(s['name'])[0]
        for c in inv:
            if c['role'] not in ALIGNABLE:
                continue
            out = os.path.join(work_dir, _safe(s['roll']), f"{stem}__{c['ch'] + 1:02d}_{_safe(c['name'])}.WAV")
            if not os.path.exists(out):
                write_mono(s['path'], c['ch'], out, s['start'], fps, df, c['name'], s['roll'])
            m = dict(path=out, name=os.path.basename(out), start=s['start'], length=s['length'],
                     roll=f"{s['roll']} {c['name']}", track=f"{s['roll']} {c['name']}", channels=1, role=c['role'],
                     ch=c['ch'], level_db=c['level_db'], split=True, pack=s['path'], clip=s.get('clip'))
            monos.append(m)
            pack['monos'][c['ch']] = m
        if pack['monos']:
            packs.append(pack)
        else:
            passthru.append(dict(s, role='mix'))
        n_al = len(pack['monos'])
        log(f"  {s['name']}: {s['channels']} tracks, {n_al} to align "
            f"({', '.join(c['name'] + '=' + c['role'] for c in inv)})")
    return monos, packs, passthru


# ---------------------------------------------------------------- audio-only reference choice
def _env(m, cache_dir):
    from .wavesync import item_envelope
    item = dict(kind='audio', path=m['path'], sr=WavInfo(m['path']).sr)
    env, _ = item_envelope(item, 0, None, cache_dir)
    return env


def plan_refs(monos, cache_dir=None, min_seg_s=2.0, log=print):
    """-> ref pieces [dict(path, name, start (piece start), length, file_start, track, role)] covering the day
    wherever at least one alignable mic is rolling, plus a human-readable summary."""
    cands = [m for m in monos if m['role'] in ALIGNABLE and m.get('level_db', 0) > -55]
    if not cands:
        return [], []
    env = {}

    def hub(m, others, a, b):
        """How well m hears the others between a and b (mean envelope correlation)."""
        try:
            if m['path'] not in env:
                env[m['path']] = _env(m, cache_dir)
            for o in others:
                if o['path'] not in env:
                    env[o['path']] = _env(o, cache_dir)
        except Exception:  # noqa: BLE001
            return m.get('level_db', -60) / 100.0
        hop = SR // 200
        scores = []
        for o in others:
            if o is m:
                continue
            i0m, i0o = (a - m['start']) // hop, (a - o['start']) // hop
            L = (b - a) // hop
            x, y = env[m['path']][i0m:i0m + L], env[o['path']][i0o:i0o + L]
            n = min(len(x), len(y))
            if n < 200:
                continue
            x, y = x[:n] - x[:n].mean(), y[:n] - y[:n].mean()
            d = np.sqrt((x * x).sum() * (y * y).sum())
            if d > 0:
                scores.append(float((x * y).sum() / d))
        return float(np.mean(scores)) if scores else 0.0

    pts = sorted({m['start'] for m in cands} | {m['start'] + m['length'] for m in cands})
    choice = []
    prev = None
    for a, b in zip(pts, pts[1:]):
        if b - a < min_seg_s * SR:
            continue
        cov = [m for m in cands if m['start'] <= a and m['start'] + m['length'] >= b]
        if not cov:
            prev = None
            continue
        top = min(PRIORITY[m['role']] for m in cov)
        best_set = [m for m in cov if PRIORITY[m['role']] == top]
        if prev is not None and prev in best_set:
            pick = prev
        elif len(best_set) == 1:
            pick = best_set[0]
        elif top == PRIORITY['lav']:
            pick = max(best_set, key=lambda m: hub(m, cov, a, b))
        else:
            pick = max(best_set, key=lambda m: m.get('level_db', -99))
        if choice and choice[-1]['m'] is pick and choice[-1]['b'] == a:
            choice[-1]['b'] = b
        else:
            choice.append(dict(m=pick, a=a, b=b))
        prev = pick
    refs, summary = [], []
    for c in choice:
        m = c['m']
        refs.append(dict(path=m['path'], name=m['name'], start=c['a'], length=c['b'] - c['a'], file_start=m['start'],
                         track=m['track'], role=m['role']))
        summary.append(dict(start=c['a'], end=c['b'], track=m['track'], role=m['role']))
    return refs, summary


def ref_windows(refs):
    """{mono path: [(a, b)]} stretches where a mono track IS the reference (it passes through unchanged)."""
    out = {}
    for r in refs:
        out.setdefault(r['path'], []).append((r['start'], r['start'] + r['length']))
    return out


# ---------------------------------------------------------------- repack
def repack(packs, aligned_by_mono, out_root, name_for, note='', log=print, cancel=None):
    """Rebuild each multichannel file: aligned tracks from their aligned monos, everything else (mix, timecode,
    empty) copied as recorded. Same length, start, bext and iXML as the original."""
    results = []
    for pk in packs:
        if cancel and cancel():
            raise KeyboardInterrupt
        s = pk['source']
        w = WavInfo(s['path'])
        stem = os.path.splitext(s['name'])[0]
        fn = os.path.join(out_root, s['roll'], name_for(stem) + '.WAV')
        os.makedirs(os.path.dirname(fn), exist_ok=True)
        hist = (f'A=PCM,F={w.sr},W={w.bits},M={w.channels},T=Auto-Align Post 2 on '
                f'{len(pk["monos"])} of {w.channels} tracks ref {note}\r\n')
        be = build_bext(base=w.bext_raw, coding_history=hist) if w.bext_raw else \
            build_bext('', None, s['roll'], '', '', TC.wrap(s['start'], TC.parse_rate('23.976')), hist)
        ix = w.ixml_raw.split(b'\0')[0] if w.ixml_raw else None
        fmt = w.format_tag if w.format_tag in (1, 3) else 1
        al = {ch: WavInfo(aligned_by_mono[m['path']]) for ch, m in pk['monos'].items() if m['path'] in aligned_by_mono}
        ww = WavWriter(fn, w.nframes, w.channels, w.sr, w.bits, fmt, be, ix)
        blk = 60 * w.sr
        try:
            with open(s['path'], 'rb') as fh:
                for s0 in range(0, w.nframes, blk):
                    n = min(blk, w.nframes - s0)
                    x = w.read(s0, n, None, fh)
                    x = x.reshape(n, -1) if x.ndim == 1 else x
                    for ch, wa in al.items():
                        x[:, ch] = wa.read(s0, n, 0)
                    ww.write_float(x)
            ww.close()
        except BaseException:
            ww.abort()
            raise
        results.append(dict(source=s['path'], aligned=fn, roll=s['roll'], start=s['start'], n=w.nframes,
                            sections=None, aligned_s=None, total_s=round(w.nframes / w.sr, 1), flags=[],
                            tracks=f"{len(al)} of {w.channels} aligned"))
        log(f"  {os.path.basename(fn)}: {len(al)} of {w.channels} tracks aligned, the rest as recorded")
    return results


def delivery_style(sources):
    """'per_take' for a sound mixer's file-per-take polywavs, 'iso' for long mono ISOs (Zaxcom style)."""
    if not sources:
        return 'iso'
    multi = sum(1 for s in sources if s['channels'] >= 2)
    med_len = float(np.median([s['length'] for s in sources])) / SR
    if multi >= 0.6 * len(sources) and med_len < 25 * 60:
        return 'per_take'
    return 'iso'
