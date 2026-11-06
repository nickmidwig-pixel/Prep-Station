"""Full run on testday_x (make_extras.py): new formats + one clip placed by hand."""
import json, os, shutil, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.path.insert(0, os.path.dirname(__file__))
from midwigsync import tc as TC
from midwigsync.scan import Project
from midwigsync.manual import ClipTools, parse_tc_input
from midwigsync.pipeline import Job, default_job_name, default_out_dir
from midwigsync.bwf import WavInfo
import run_pipeline as RP

DAY = sys.argv[1] if len(sys.argv) > 1 else '/home/claude/testday_x'
truth = json.load(open(os.path.join(DAY, 'truth.json')))
truth.update(json.load(open(os.path.join(DAY, 'truth_extras.json'))))
p = Project()
p.add_paths([DAY])
job = default_job_name(p)
out = default_out_dir(p, job)
print('job', job, 'out', out)
if os.path.exists(out):
    shutil.rmtree(out)
zoom = next(c for c in p.clips if c['name'] == 'ZOOM0001.WAV')
typed = TC.samples_to_tc(truth['ZOOM0001.WAV']['true_start'], p.fps)
ClipTools(p).set_tc(zoom, parse_tc_input(typed, p.fps), 'typed')
print('ZOOM typed in as', typed)
ev = []
j = Job(p, dict(job_name=job, out_dir=out, _fake_aap=RP.fake_aap, poly_unaligned=True),
        lambda k, d: (ev.append((k, d)), print(k, d if k != 'error' else d['trace'])) if k in ('error', 'done') else ev.append((k, d)))
t = time.time(); j.run(); print(f'pipeline {time.time() - t:.1f}s')
assert not any(k == 'error' for k, _ in ev), 'pipeline error'
print('\nPLACEMENT vs truth (ms)')
for c in sorted(p.clips, key=lambda c: c['name']):
    if c['name'] not in truth or c['start'] is None:
        print(f"  {c['name']:34} not placed flags={c['flags']}")
        continue
    err = (c['start'] - truth[c['name']]['true_start']) / 48
    print(f"  {c['name']:34} {TC.samples_to_tc(c['start'], p.fps)} err={err:8.1f} ms  src={c.get('tc_source')} flags={c['flags']} "
          f"sync={ {k: v for k, v in (c.get('sync') or {}).items() if k in ('method', 'score', 'against')} }")
print('\nSTAMPED', [s['out'] for s in j.stamped])
print('ALIGNED', [os.path.basename(a['aligned']) for a in j.aligned])
print('POLY', [(os.path.basename(x['path']), x['channels']) for x in j.polys])
x = open(os.path.join(out, j.xmls[0])).read()
for n in ('A002_C001.braw', 'A002_C002.braw', '0001AB.MXF', 'GX010042.MP4', 'GX020043.MP4', 'MIX_0001', 'Voice 001', 'ZOOM0001'):
    print('  XML has', n, n in x)
