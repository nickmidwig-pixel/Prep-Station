"""Audio-only run on make_mixer_day.py's delivery, with a stand-in for Auto-Align that really measures each
piece's delay against the reference piece and shifts it (like AAP's static mode). Checks:
  - mode is audio only, references: Boom for takes with a boom, a lav for the boom-less take
  - aligned files keep their names, track layout, iXML names, bext TC/user bits, length
  - Mix-L/R, Spare, and the reference track itself are bit-identical to the originals
  - the aligned lavs sit at ~0 lag against the boom (they were 0.9 ms early)."""
import json
import os
import shutil
import sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app'))
from midwigsync import tc as TC  # noqa: E402
from midwigsync.bwf import WavInfo, WavWriter  # noqa: E402
from midwigsync.scan import Project  # noqa: E402
from midwigsync.pipeline import Job, default_job_name, default_out_dir  # noqa: E402

DAY = sys.argv[1] if len(sys.argv) > 1 else '/home/claude/mixerday'
SR = 48000


def lag_of(ref, x, R=SR // 20):
    """L such that x(t) ~ ref(t - L)  (x late by L)."""
    n = 1 << int(np.ceil(np.log2(len(ref) + len(x))))
    c = np.fft.irfft(np.fft.rfft(x, n) * np.conj(np.fft.rfft(ref, n)), n)
    c = np.concatenate([c[-R:], c[:R + 1]])
    return int(np.argmax(c)) - R


def fake_aap(plan, render_dir):
    refs = [i for i in plan['items'] if i['kind'] == 'ref']
    for k, it in enumerate(i for i in plan['items'] if i['kind'] == 'source'):
        r = next(r for r in refs if r['abs_in'] <= it['abs_in'] and it['abs_out'] <= r['abs_out'])
        ws, wr = WavInfo(it['path']), WavInfo(r['path'])
        fps = TC.parse_rate(plan['fps'])
        a = TC.frames_to_samples(it['abs_in'], fps)
        b = TC.frames_to_samples(it['abs_out'], fps)
        s_start = ws.time_reference
        r_start = wr.time_reference
        mid = (a + b) // 2
        W = 20 * SR
        x = ws.read(mid - W // 2 - s_start, W, 0).astype(np.float64)
        y = wr.read(mid - W // 2 - r_start, W, 0).astype(np.float64)
        L = lag_of(y, x)                      # source late by L vs the reference (negative = early)
        out = ws.read(a - s_start + L, b - a, 0)
        stem = os.path.splitext(os.path.basename(it['path']))[0]
        p = os.path.join(render_dir, f'{stem}_si0ch0off{a - s_start}-AtAlgn-{k:04x}cafe.wav')
        w = WavWriter(p, len(out), 1, SR, 24)
        w.write_float(out)
        w.close()


def main():
    truth = json.load(open(os.path.join(DAY, 'truth_mixer.json')))
    p = Project()
    p.add_paths([DAY])
    job = default_job_name(p)
    out = default_out_dir(p, job)
    if os.path.exists(out):
        shutil.rmtree(out)
    ev = []
    j = Job(p, dict(job_name=job, out_dir=out, _fake_aap=fake_aap), lambda k, d: ev.append((k, d)))
    j.run()
    errs = [d for k, d in ev if k == 'error']
    if errs:
        print(errs[0]['trace'])
        raise SystemExit('pipeline error')
    print('job', job, '| mode', j.mode(), '| names', j._names, '| days', [d['label'] for d in p.days()])
    for line in j.log_lines:
        if 'reference ' in line or 'tracks,' in line or 'polywav' in line.lower():
            print('  ', line[10:])
    ok = True
    for name, t in truth['takes'].items():
        a = next(x for x in j.aligned if os.path.basename(x['source']) == name)
        src, aln = WavInfo(a['source']), WavInfo(a['aligned'])
        same_name = os.path.basename(a['aligned']) == name
        meta = (aln.nframes == src.nframes and aln.channels == src.channels and aln.time_reference == src.time_reference
                and aln.track_names() == src.track_names() and aln.description == src.description)
        X, Y = src.read(0, src.nframes), aln.read(0, aln.nframes)
        exact = {nm: bool(np.array_equal(X[:, i], Y[:, i])) for i, nm in src.track_names().items() for i in [i - 1]}
        boom = Y[:, 2].astype(np.float64)
        lags = {}
        for i, nm in ((3, 'Jane'), (4, 'Bob')):
            lags[nm] = None if t['boom_off'] else lag_of(boom[SR:21 * SR], Y[SR:21 * SR, i].astype(np.float64))
        before = {nm: None if t['boom_off'] else lag_of(X[SR:21 * SR, 2].astype(np.float64), X[SR:21 * SR, i].astype(np.float64))
                  for i, nm in ((3, 'Jane'), (4, 'Bob'))}
        good = same_name and meta and exact['Mix-L'] and exact['Mix-R'] and exact['Spare'] and \
            (t['boom_off'] or (exact['Boom'] and all(abs(v) <= 2 for v in lags.values())))
        ok &= good
        print(f"  {name}: {'OK ' if good else 'BAD'} name={same_name} meta={meta} untouched="
              f"{[k for k, v in exact.items() if v]} lag vs boom before={before} after={lags}")
    print('ALL GOOD' if ok else 'PROBLEMS')
    print('polywavs:', [os.path.basename(x['path']) for x in j.polys], '| xmls:', j.xmls)


if __name__ == '__main__':
    main()
