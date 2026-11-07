"""End-to-end test on the simulated day, with a stand-in for Auto-Align Post that writes renders the way
AAP does (plain WAV, no TC, <src>_si0ch0off<N>-AtAlgn-<hash>.wav, some negative N, one truncated render,
one polarity flip). Then checks every output."""
import json
import os
import shutil
import sys
import time
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync import tc as TC  # noqa: E402
from midwigsync.bwf import WavInfo, WavWriter  # noqa: E402
from midwigsync.scan import Project  # noqa: E402
from midwigsync.pipeline import Job, default_job_name, default_out_dir  # noqa: E402

DAY = sys.argv[1] if len(sys.argv) > 1 else '/home/claude/testday'
SR = 48000
FPS = TC.parse_rate('23.976')
T0 = TC.tc_to_samples('09:16:56:00', FPS)
AAP2 = os.environ.get('AAP2') == '1'     # render names the way Auto-Align Post 2 writes them under Resolve 21


def zax(t_abs):
    return 0.045 + 0.010 * np.sin(2 * np.pi * (t_abs - T0) / SR / 300.0)


def ideal_aligned(path, start, a, b):
    """What a perfect aligner would output for [a,b) of the ISO at path (file starts at `start`)."""
    w = WavInfo(path)
    tt = np.arange(a, b, dtype=np.float64)
    warp = zax(tt) * SR if ('LAV' in path or 'BOOM' in path) else 0.0   # only the Zaxcom recorders drift
    idx = tt - warp - start
    lo = int(np.floor(idx.min())) - 2
    hi = int(np.ceil(idx.max())) + 2
    x = w.read(lo, hi - lo, 0).astype(np.float64)
    i0 = np.floor(idx).astype(np.int64) - lo
    fr = idx - np.floor(idx)
    i0 = np.clip(i0, 0, len(x) - 2)
    return (x[i0] * (1 - fr) + x[i0 + 1] * fr).astype(np.float32)


def fake_aap(plan, render_dir):
    rng = np.random.default_rng(3)
    spf = TC.samples_per_frame(FPS)
    k = 0
    for it in plan['items']:
        if it['kind'] != 'source':
            continue
        w = WavInfo(it['path'])
        start = w.time_reference
        a = TC.frames_to_samples(it['abs_in'], FPS)
        b = TC.frames_to_samples(it['abs_out'], FPS)
        pre = int(rng.integers(0, 3000)) if k % 3 == 0 else 0     # some renders start before the piece
        if AAP2:                     # Auto-Align Post 2 in Resolve 21: 1 s handles where the file has them
            pre = min(SR, a - start)
            b = b + min(SR, start + w.nframes - b)
        a2 = a - pre
        if k == 2:
            b = b - 20 * SR          # AAP bug: render cut short
        y = ideal_aligned(it['path'], start, a2, b)
        if k == 4:
            y = -y                   # spectral mode flipped polarity on this piece
        stem = os.path.splitext(os.path.basename(it['path']))[0]
        if AAP2:                     # no offset in the name: Prep Station places it from the timeline plan
            out = os.path.join(render_dir, f'{stem}-AtAlgn-{k:04x}beef.wav')
        else:
            out = os.path.join(render_dir, f'{stem}_si0ch0off{a2 - start}-AtAlgn-{k:04x}beef.wav')
        ww = WavWriter(out, len(y), 1, SR, 24)
        ww.write_float(y)
        ww.close()
        k += 1
        print(f'  fake AAP render {os.path.basename(out)}  {(b - a2) / SR:.1f}s')


def main():
    p = Project()
    p.add_paths([DAY])
    job = default_job_name(p)
    out = default_out_dir(p, job)
    if os.path.exists(out):
        __import__('midwigsync.safety', fromlist=['x']).allow_output(out)
        shutil.rmtree(out)
    events = []

    def emit(kind, data):
        events.append((kind, data))
        if kind in ('log', 'step', 'error', 'aap_wait', 'done'):
            print(kind, data if kind != 'error' else data['trace'])

    j = Job(p, dict(job_name=job, out_dir=out, _fake_aap=fake_aap, poly_unaligned=True), emit)
    t = time.time()
    j.run()
    print(f'pipeline {time.time() - t:.1f}s')
    assert not any(k == 'error' for k, _ in events), 'pipeline error'
    # ---- check aligned ISOs against the ideal
    print('\nALIGNED ISO CHECK (vs perfect alignment)')
    for a in j.aligned:
        src = [s for s in j.audio_sources() if s['path'] == a['source']][0]
        w = WavInfo(a['aligned'])
        ws = WavInfo(a['source'])
        assert w.nframes == ws.nframes, 'length changed'
        assert w.time_reference == ws.time_reference, 'TC changed'
        assert w.description == ws.description, 'description changed'
        n = w.nframes
        errs = []
        for s0 in range(SR, n - 11 * SR, 30 * SR):
            x = w.read(s0, 10 * SR, 0)
            y = ideal_aligned(a['source'], src['start'], src['start'] + s0, src['start'] + s0 + 10 * SR)
            if np.abs(y).max() < 1e-3:
                continue
            # correlation and residual
            cc = float(np.dot(x, y) / np.sqrt(np.dot(x, x) * np.dot(y, y) + 1e-20))
            errs.append(cc)
        stats = a.get('tracks') or f"sections={a['sections']} aligned={a['aligned_s']:.0f}/{a['total_s']:.0f}s"
        print(f"  {os.path.basename(a['aligned']):40} {stats} "
              f"min_cc_vs_ideal={min(errs):.3f} flags={a['flags']}")
    print('\nVERIFY vs REF (lag samples):')
    for k, v in j.verify.items():
        print('  ', os.path.basename(k), [(l, round(c, 2)) for (_, l, c) in v])
    print('\nPOLY:')
    for x in j.polys:
        w = WavInfo(x['path'])
        print('  ', os.path.basename(x['path']), w.channels, 'ch', TC.samples_to_tc(w.time_reference, FPS), w.track_names(),
              round(w.nframes / SR, 1), 's')
    print('\nXML:', j.xmls)
    print('\nREPORT:', j.report_path)


if __name__ == '__main__':
    main()
