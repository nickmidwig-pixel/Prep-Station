"""Camera copies with new timecode: an exact copy of the original bytes with only the timecode number changed
(MOV/MP4 tmcd, MXF timecode components, BRAW). Original untouched; copy differs in a handful of bytes only."""
import hashlib, os, shutil, sys, glob
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync.scan import Project
from midwigsync import stamp as ST, safety, media, tc as TC

out = '/tmp/camcopy'
shutil.rmtree(out, ignore_errors=True)
files = [glob.glob('/home/claude/slowday/A_FX6/A001C001.MOV')[0],
         glob.glob('/home/claude/testday/**/*.MXF', recursive=True)[0],
         glob.glob('/home/claude/testday_x/**/*.braw', recursive=True)[0],
         glob.glob('/home/claude/slowday/C_FX3/C0003.MP4')[0]]
p = Project(); p.add_paths(files); p.analyze()
safety.allow_output(out)
ok = True
for c in p.clips:
    h0 = hashlib.sha256(open(c['path'], 'rb').read()).hexdigest()
    new = c['orig_start'] + TC.frames_to_samples(37 * 24 + 5, c['fps'])     # 37 s 5 frames later
    path, how = ST.copy_with_tc(c, os.path.join(out, c['roll']), new)
    got = media.probe_video(path)
    a, b = open(c['path'], 'rb').read(), open(path, 'rb').read()
    diff = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b)) if len(a) == len(b) else -1
    same_orig = hashlib.sha256(open(c['path'], 'rb').read()).hexdigest() == h0
    good = abs(got['start'] - new) <= TC.samples_per_frame(c['fps']) and same_orig
    ok &= good
    print(f"  {'ok ' if good else 'BAD'} {c['name']:30s} {TC.samples_to_tc(c['orig_start'], c['fps'])} -> "
          f"{TC.samples_to_tc(got['start'], c['fps'])}  ({how}; bytes differing: {diff if diff >= 0 else 'rewrapped'}; original unchanged: {same_orig})")
print('CAMERA COPY GOOD' if ok else 'CAMERA COPY FAILED')
