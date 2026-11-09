"""The stop after the timecode check: the job waits, shows the fixes, and only writes after you answer.
'write' -> SOUND_TC_CORRECTED with every sound file (fixed ones re-stamped, the rest exact copies);
'skip'  -> the files' own timecode is kept. Also: camera copies offered after the run, then written on request."""
import hashlib, os, shutil, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync.scan import Project
from midwigsync.pipeline import Job, default_job_name, default_out_dir
from midwigsync import safety, media, tc as TC
from midwigsync.bwf import WavInfo

def run(choice):
    p = Project(); p.add_paths(['/home/claude/testday']); p.check_short_clips(20); p.analyze()
    out = f'/home/claude/testday/REVIEW_{choice}_SYNCED'
    safety.allow_output(out); shutil.rmtree(out, ignore_errors=True)
    ev = []
    j = Job(p, dict(job_name=f'REVIEW_{choice}', out_dir=out, aap_mode='skip', review_tc=True), lambda k, d: ev.append((k, d)))
    t = threading.Thread(target=j.run); t.start()
    for _ in range(600):
        if any(k == 'review_tc' for k, _ in ev):
            break
        time.sleep(0.2)
    rev = next(d for k, d in ev if k == 'review_tc')
    written_before = os.path.exists(os.path.join(out, 'SOUND_TC_CORRECTED'))
    j.decide(choice); t.join(600)
    errs = [d for k, d in ev if k == 'error']
    return p, j, out, rev, written_before, errs, ev

ok = True
p, j, out, rev, early, errs, ev = run('write')
print('fixes shown:', [(f['name'], f['old'], '->', f['new']) for f in rev['fixes']])
print('anything written before the answer:', early, '| errors:', [e['message'] for e in errs])
sc = os.path.join(out, 'SOUND_TC_CORRECTED')
files = sorted(os.path.relpath(os.path.join(d, f), sc) for d, _, fs in os.walk(sc) for f in fs)
aud = [c for c in p.clips if c['kind'] == 'audio' and c['include'] and c['start'] is not None]
print(f'SOUND_TC_CORRECTED: {len(files)} files for {len(aud)} sound files')
same = 0
for c in aud:
    path = os.path.join(sc, j._mirror_dir(c), c['name'])      # mirrors the dropped folder: SOUND/02LAV/...
    fixed = any(f['name'] == c['name'] for f in rev['fixes'])
    w0, w1 = WavInfo(c['path']), WavInfo(path)
    if not fixed:
        same += hashlib.sha256(open(c['path'], 'rb').read()).digest() == hashlib.sha256(open(path, 'rb').read()).digest()
    else:
        good = (w1.channels == w0.channels and w1.bits == w0.bits and w1.sr == w0.sr and (w1.track_names() == w0.track_names() or not w0.track_names())
                and abs(w1.time_reference - TC.wrap(c['stamped_start'], p.fps)) <= 1)
        print(f'  fixed {c["name"]}: same format/names {good}, new TC {TC.samples_to_tc(c["stamped_start"], p.fps)}')
        ok &= good
n_fixed_audio = sum(1 for f in rev['fixes'] if f['kind'] == 'audio')
print(f'  unchanged copies identical to the originals: {same} of {len(aud) - n_fixed_audio}')
ok &= not early and not errs and len(files) == len(aud) and same == len(aud) - n_fixed_audio
done = next(d for k, d in ev if k == 'done')
print('camera fixes offered after the run:', [(x['name'], x['new']) for x in done['camera_fixes']])
made = j.rewrite_cameras()
xml = open(os.path.join(out, 'TIMELINES', 'REVIEW_write_SYNC_UNALIGNED.xml')).read()
print('camera copies:', [os.path.relpath(m, out) for m in made], '| XML points at them:', all(os.path.basename(m) in xml and 'CAMERA_TC_CORRECTED' in xml for m in made))
for m in made:
    c = next(c for c in p.clips if c.get('stamped_path') == m)
    got = media.probe_video(m)['start']
    print(f'   {os.path.basename(m)} TC {TC.samples_to_tc(got, c["fps"])} (wanted {TC.samples_to_tc(c["start"], c["fps"])})')
    ok &= abs(got - c['start']) <= TC.samples_per_frame(c['fps'])
ok &= len(made) == len(done['camera_fixes']) and len(made) > 0

p2, j2, out2, rev2, early2, errs2, ev2 = run('skip')
kept = all(c['start'] == c['orig_start'] for c in p2.clips if any(f['name'] == c['name'] for f in rev2['fixes']) and c['orig_start'] is not None)
print("'skip': files keep their own TC:", kept, '| SOUND_TC_CORRECTED written:', os.path.exists(os.path.join(out2, 'SOUND_TC_CORRECTED')))
ok &= kept and not errs2
for o in (out, out2):
    shutil.rmtree(o, ignore_errors=True)
print('REVIEW GOOD' if ok else 'REVIEW FAILED')
