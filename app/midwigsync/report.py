"""Verification + the plain-text report written next to the outputs."""
from .brand import APP_NAME
import datetime as dt
import os
import numpy as np

from . import tc as TC
from .bwf import WavInfo

SR = TC.SR

FLAG_TEXT = {
    'no_tc': 'no timecode', 'zero_tc': 'timecode at 00:00:00:00', 'tc_far': 'timecode hours away from the rest',
    'tc_overlap': 'timecode overlaps another clip on the same card (TC jump)', 'sample_rate': 'not 48 kHz',
    'fps_mismatch': 'different frame rate', 'no_audio': 'no audio', 'ubits': 'user bits differ',
    'short_silent': 'short accidental clip, skipped', 'false_take': 'in the recorder\'s False Takes folder, left out', 'sync_corrected': 'timecode corrected by waveform',
    'sync_found': 'placed by waveform', 'sync_failed': 'could not be placed (left out)',
    'sync_manual': 'timecode set by hand', 'ltc': 'timecode read from LTC audio', 'converted': 'converted to 48 kHz WAV',
    'raw': 'RAW camera file (audio + timecode only)', 'duplicate': 'duplicate of another file (left out)',
}


def verify_against_ref(aligned, refs, n_checks=4, win_s=8, max_lag_ms=200):
    """For each aligned ISO: NCC lag vs the reference WAVs at a few loud spots. Lag ~0 = aligned."""
    out = {}
    R = int(max_lag_ms * SR / 1000)
    for a in aligned:
        wa = WavInfo(a['aligned'])
        res = []
        for r in refs:
            wr = WavInfo(r['path'])
            s0 = max(a['start'], r['start'])
            s1 = min(a['start'] + wa.nframes, r['start'] + wr.nframes)
            if s1 - s0 < (win_s + 2) * SR:
                continue
            for k in range(n_checks):
                t = s0 + SR + int((s1 - s0 - (win_s + 2) * SR) * (k + 0.5) / n_checks)
                W = win_s * SR
                x = wa.read(t - a['start'], W)
                x = x.mean(1) if x.ndim == 2 else x
                y = wr.read(t - r['start'] - R, W + 2 * R)
                y = y.mean(1) if y.ndim == 2 else y
                if np.abs(x).max() < 1e-3 or np.abs(y).max() < 1e-3:
                    continue
                n = 1 << int(np.ceil(np.log2(len(y) + W)))
                c = np.fft.irfft(np.fft.rfft(y, n) * np.conj(np.fft.rfft(x, n)), n)[:2 * R + 1]
                yy = np.concatenate([[0], np.cumsum(y.astype(np.float64) ** 2)])
                cc = c / np.sqrt((x.astype(np.float64) ** 2).sum() * np.maximum(yy[W:W + 2 * R + 1] - yy[:2 * R + 1], 1e-20))
                j = int(np.argmax(np.abs(cc)))
                res.append((t, j - R, float(cc[j])))
            if len(res) >= n_checks:
                break
        out[a['aligned']] = res
    return out


def write_report(path, job, project, sync_report, stamped, aap_info, aligned, verify, polys, xmls, settings, log_lines):
    fps = project.fps
    L = []
    w = L.append
    w(f"{APP_NAME.upper()} REPORT — {job}")
    w(f"Generated {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}   Frame rate {TC.rate_label(fps)}{' DF' if project.df else ''}")
    mode = settings.get('_mode')
    if mode == 'audio':
        w('Mode: audio only (reference mic chosen per stretch)')
    elif mode == 'camera':
        ref = settings.get('_ref') or {}
        w(f"Mode: camera reference {ref.get('label') or ref.get('roll', '?')} ch {ref.get('channel', 0) + 1}")
        if len(ref.get('rolls') or []) > 1:
            w(f"      cards: {', '.join(ref['rolls'])}")
    from . import safety
    sm = safety.summary()
    w(f"Originals: {sm['originals']} files read-only, untouched"
      + (f"; {len(sm['blocked'])} change(s) refused" if sm['blocked'] else ''))
    for act, pth in sm['blocked'][:20]:
        w(f"   refused: {act} {pth}")
    w('=' * 78)
    w('')
    fi = settings.get('_fps_info') or {}
    if fi:
        w(f"Frame rate: {fi.get('label')} ({'set by you' if fi.get('status') == 'set' else 'checked'}). {fi.get('reason', '')}")
        for e in fi.get('evidence', []):
            w(f"   {e['kind']:7s} {e['name']:24s} {e['label']:9s} {'' if e['votes'] else '(not counted) '}{e.get('note', '')}")
        w('')
    slow = settings.get('_slow') or []
    if slow:
        w(f'SLOW MOTION + OTHER FRAME RATES (not synced, sound not used): {len(slow)} clip(s) -> EDITORIAL/*_SLOW_MOTION.xml')
        for c in sorted(slow, key=lambda c: (c['roll'], c.get('orig_start') or 0, c['name'])):
            sm = c.get('slowmo') or {}
            tcs = TC.samples_to_tc(c['orig_start'], c.get('fps') or fps) if c.get('orig_start') is not None else 'no TC'
            w(f"   {c['roll']:14s} {c['name']:34s} {tcs}  {TC.rate_label(c['fps']) if c.get('fps') else '?':7s} {sm.get('why', '')}")
        w('')
    w('INVENTORY')
    for r in sorted(project.rolls().values(), key=lambda r: (r['kind'] != 'camera', r['track_no'] or 999, r['name'])):
        clips = r['clips']
        inc = [c for c in clips if c['include']]
        span = [c for c in inc if c['start'] is not None]
        rng = ''
        if span:
            a = min(c['start'] for c in span)
            b = max(c['start'] + c['length'] for c in span)
            rng = f"{TC.samples_to_tc(a, fps)} → {TC.samples_to_tc(b, fps)}"
        label = f"{r['name']}" + (f" ({r['camera']})" if r.get('camera') else '')
        w(f"  {label:28} {r['kind']:6} {len(inc):3} of {len(clips):3} clips   {rng}")
    w('')
    w('TIMECODE CHECK')
    if sync_report:
        w(f"  Rolls whose timecode agrees: {', '.join(sync_report.get('cluster', []))}")
        ck = sync_report.get('clock_ms') or {}
        smalls = {k: v for k, v in ck.items() if abs(v) >= 1}
        if smalls:
            w('  Clock offsets vs the reference (left for Auto-Align): ' +
              ', '.join(f'{k} {v:+.0f} ms' for k, v in sorted(smalls.items())))
    flagged = [c for c in project.clips if c['flags']]
    if not flagged:
        w('  Nothing looked off.')
    for c in sorted(flagged, key=lambda c: (c['roll'], c['name'])):
        txt = '; '.join(FLAG_TEXT.get(f, f) for f in c['flags'])
        extra = ''
        if c.get('sync') and c['start'] is not None:
            s = c['sync']
            moved = ''
            if c['orig_start'] is not None:
                d = c['start'] - c['orig_start']
                moved = f" moved {d / SR:+.3f} s ({TC.samples_to_tc(c['orig_start'], fps)} → {TC.samples_to_tc(c['start'], fps)})"
            else:
                moved = f" placed at {TC.samples_to_tc(c['start'], fps)}"
            extra = f" —{moved}; match {s.get('score')} vs {s.get('against')}"
            if s.get('note'):
                extra += f" ({s['note']})"
        w(f"  {c['roll']}/{c['name']}: {txt}{extra}")
    if stamped:
        w('')
        w('TIMECODE WRITTEN TO COPIES (originals untouched)')
        for s in stamped:
            w(f"  {s['name']} → {s['out']}  TC {s['tc']}")
    w('')
    w('AUTO-ALIGN POST')
    if aap_info:
        for k, v in aap_info.items():
            w(f'  {k}: {v}')
    rs = settings.get('_ref_summary') or []
    if rs:
        w('')
        w('  Reference mic, stretch by stretch:')
        for s in rs[:300]:
            w(f"   {TC.samples_to_tc(s['start'], fps)} – {TC.samples_to_tc(s['end'], fps)}  {s['track']} ({s['role']})")
    if aligned:
        w('')
        w('  Aligned files (same start TC + length as the source, metadata carried over):')
        for a in aligned:
            v = verify.get(a['aligned'], []) if verify else []
            lags = [abs(l) for (_, l, cc) in v if abs(cc) > 0.2]
            vtxt = f"  check vs reference: {max(lags)} samples max" if lags else ''
            if a.get('passthrough'):
                w(f"   {os.path.basename(a['aligned'])}: kept as recorded ({'; '.join(a['flags'])})")
                continue
            if a.get('tracks'):
                w(f"   {os.path.basename(a['aligned'])}: {a['tracks']}{vtxt}")
                continue
            w(f"   {os.path.basename(a['aligned'])}: {a['sections']} aligned sections, {a['aligned_s'] / 60:.1f} of "
              f"{a['total_s'] / 60:.1f} min covered by the reference{vtxt}")
            for f in a['flags']:
                w(f"      note: {f}")
    w('')
    w('POLYWAV')
    for p in polys or []:
        tcs = p.get('tc') or TC.samples_to_tc(p['start'], fps)
        size = p.get('bytes') or p.get('size') or (os.path.getsize(p['path']) if os.path.exists(p['path']) else 0)
        w(f"  {os.path.relpath(p['path'], os.path.dirname(path))}  starts {tcs}  {p['n'] / SR / 60:.1f} min  {size / 1e9:.2f} GB")
    if polys:
        lays = []
        for p in polys:
            key = (p.get('layout') or '', tuple(p.get('channels') or []))
            if key[1] and key not in lays:
                lays.append(key)
        if len(lays) <= 1:
            w(f"  Channels: {', '.join(settings.get('poly_channels', []))}")
        else:
            w(f"  {len(lays)} track layouts on this day, one polywav set per layout:")
            for sub, ch in lays:
                w(f"    set {sub}: {', '.join(ch)}")
    w('')
    w('XML')
    for x in xmls or []:
        w(f'  {x}')
    w('')
    w('LOG')
    for line in log_lines[-200:]:
        w('  ' + line)
    with open(path, 'w') as f:
        f.write('\n'.join(L) + '\n')
    return path
