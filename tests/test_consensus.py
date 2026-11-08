"""Fix the fewest sources: whose timecode gets corrected when clocks disagree.
Four days from make_consensus_day.py: the recorder off (cameras agree), a GoPro off, the reference camera off
(the other two agree), and a day where everything agrees. Checks which files move, that they land within a
frame of the truth, that nothing else moves, and that the decision is explained."""
import json, os, shutil, subprocess, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app'))
from midwigsync.scan import Project
from midwigsync.pipeline import Job
from midwigsync import safety, tc as TC

EXPECT = dict(audio_off={'CONS_T01.WAV', 'CONS_T02.WAV'}, gopro={'GX010042.MP4'},
              ref_off={'A001C001_261107AB.MXF', 'A002C001_261107AB.MXF'}, all_good=set())
ok = True
for scen, want in EXPECT.items():
    day = f'/home/claude/consday_{scen}'
    if not os.path.exists(os.path.join(day, 'truth.json')):
        subprocess.run([sys.executable, os.path.join(HERE, 'make_consensus_day.py'), day, scen], check=True,
                       capture_output=True)
    truth = json.load(open(os.path.join(day, 'truth.json')))
    p = Project(); p.add_paths([day]); p.check_short_clips(20); p.analyze()
    out = f'{day}_SYNCED'
    safety.allow_output(out); shutil.rmtree(out, ignore_errors=True)
    ev = []
    j = Job(p, dict(job_name=f'CONS_{scen}', out_dir=out, aap_mode='skip', review_tc=True, transcript=False),
            lambda k, d: ev.append((k, d)))
    t = threading.Thread(target=j.run); t.start()
    for _ in range(900):
        if any(k in ('review_tc', 'done', 'error') for k, _ in ev) or not t.is_alive():
            break
        time.sleep(0.2)
    rev = next((d for k, d in ev if k == 'review_tc'), None)
    sr = j.sync_report or {}
    moved = {f['name'] for f in (rev or {}).get('fixes', [])}
    j.decide('stop'); t.join(300)
    good = moved == want
    for c in p.clips:
        if c['name'] in want:
            err = abs(c['start'] - truth[c['name']]['true_start'])
            good &= err <= TC.samples_per_frame(p.fps)
            if err > TC.samples_per_frame(p.fps):
                print(f'   {c["name"]} landed {err / 48000:+.3f} s from the truth')
    labels = {s['label'] for s in (sr.get('sources') or {}).get('off', [])}
    dec = sr.get('decision') or ''
    good &= ('→ correct' in dec) == bool(want)
    print(f'{scen:10s} {"OK " if good else "BAD"} moved={sorted(moved)}  off sources={sorted(labels)}')
    print('           ', dec)
    ok &= good
print('CONSENSUS OK' if ok else 'CONSENSUS FAILED')
sys.exit(0 if ok else 1)
