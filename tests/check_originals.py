"""Fingerprint every original file (contents hash, size, modified + changed dates, inode, link count, permissions),
run every test pipeline, fingerprint again. Any difference, or any new file outside the app's own output folders,
is a failure."""
import hashlib, os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
ROOTS = ['/home/claude/testday', '/home/claude/testday_x', '/home/claude/mixerday', '/home/claude/slowday', '/home/claude/hopday']
OUTS = ('_SYNCED', '_MIDWIG', '_SYNC_WORK')

def walk():
    seen = {}
    for r in ROOTS:
        for d, dirs, files in os.walk(r, followlinks=True):
            dirs[:] = [x for x in dirs if not x.endswith(OUTS)]
            for f in files:
                p = os.path.join(d, f)
                rp = os.path.realpath(p)
                if rp in seen or any(part.endswith(OUTS) for part in rp.split(os.sep)):
                    continue
                st = os.stat(rp)
                h = hashlib.sha256()
                with open(rp, 'rb') as fh:
                    for b in iter(lambda: fh.read(1 << 22), b''):
                        h.update(b)
                seen[rp] = (h.hexdigest(), st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_nlink, st.st_mode)
    return seen

before = walk()
print(f'{len(before)} original files fingerprinted')
runs = [['run_pipeline.py'], ['run_extras.py'], ['run_mixer_day.py'], ['run_two_days.py'], ['test_guard.py'],
        ['test_framerate.py'], ['test_slowmo.py'], ['test_refpick.py'], ['test_camera_copy.py'], ['test_transcript.py'],
        ['test_review.py']]
for r in runs:
    t = time.time()
    p = subprocess.run([sys.executable, os.path.join(HERE, r[0])], capture_output=True, text=True)
    tail = (p.stdout.strip().splitlines() or [''])[-1]
    print(f'  {r[0]:20s} exit {p.returncode}  {time.time() - t:5.0f}s  {tail[:90]}')
    if p.returncode:
        print(p.stdout[-3000:], p.stderr[-3000:])
after = walk()
changed = [k for k in before if k in after and before[k] != after[k]]
missing = [k for k in before if k not in after]
new = [k for k in after if k not in before]
fields = ('contents', 'size', 'modified', 'changed', 'inode', 'links', 'permissions')
for k in changed:
    print('CHANGED', k, [f for f, a, b in zip(fields, before[k], after[k]) if a != b])
for k in missing:
    print('MISSING', k)
for k in new:
    print('NEW FILE OUTSIDE OUTPUT', k)
print('ORIGINALS UNTOUCHED' if not (changed or missing or new) else 'ORIGINALS CHANGED')
