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
    'short_silent': 'short accidental clip, skipped', 'sync_corrected': 'timecode corrected by waveform',
    'sync_found': 'placed by waveform', 'sync_failed': 'could not be placed (left out)',
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
    w('=' * 78)
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
    if aligned:
        w('')
        w('  Aligned ISOs (same start TC + length as the source, metadata carried over):')
        for a in aligned:
            v = verify.get(a['aligned'], []) if verify else []
            lags = [abs(l) for (_, l, cc) in v if abs(cc) > 0.2]
            vtxt = f"  check vs reference: {max(lags)} samples max" if lags else ''
            w(f"   {os.path.basename(a['aligned'])}: {a['sections']} AAP sections, {a['aligned_s'] / 60:.1f} of "
              f"{a['total_s'] / 60:.1f} min under camera{vtxt}")
            for f in a['flags']:
                w(f"      note: {f}")
    w('')
    w('POLYWAV')
    for p in polys or []:
        w(f"  {os.path.basename(p['path'])}  starts {p['tc']}  {p['n'] / SR / 3600:.2f} h  {p['bytes'] / 1e9:.2f} GB")
    if polys:
        w(f"  Channels: {', '.join(settings.get('poly_channels', []))}")
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
