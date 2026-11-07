"""Try to change original media every way we can think of; every attempt must be refused and nothing may change."""
import hashlib, os, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
import numpy as np
from midwigsync.scan import Project
from midwigsync import safety, convert
from midwigsync.bwf import patch_time_reference

root = tempfile.mkdtemp(prefix='guard_')
day = os.path.join(root, 'DAY1'); snd = os.path.join(day, 'SOUND'); os.makedirs(snd)
src = shutil.copy('/home/claude/mixerday/SOUND_ROLL_01/T001.WAV', os.path.join(snd, 'T001.WAV'))
alias_hard = os.path.join(root, 'hardlink_made_before.wav'); os.link(src, alias_hard)   # made before the app ran
alias_soft = os.path.join(root, 'symlink.wav'); os.symlink(src, alias_soft)
def fp(p):
    st = os.stat(p)
    return hashlib.sha256(open(p, 'rb').read()).hexdigest(), st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode, st.st_nlink
before = fp(src)

p = Project(); p.add_paths([day])
out = os.path.join(day, 'DAY1_SYNCED'); safety.allow_output(out); os.makedirs(out, exist_ok=True)
ff = convert.ffmpeg_exe()
attempts = [
    ('open for writing', lambda: open(src, 'wb')),
    ('open read+write', lambda: open(src, 'r+b')),
    ('append', lambda: open(src, 'ab')),
    ('os.open O_RDWR', lambda: os.open(src, os.O_RDWR)),
    ('delete', lambda: os.remove(src)),
    ('rename', lambda: os.rename(src, src + '.x')),
    ('replace over it', lambda: (open(os.path.join(out, 't.wav'), 'wb').close(), os.replace(os.path.join(out, 't.wav'), src))),
    ('change dates', lambda: os.utime(src, (0, 0))),
    ('chmod', lambda: os.chmod(src, 0o444)),
    ('truncate', lambda: os.truncate(src, 10)),
    ('hard link to it', lambda: os.link(src, os.path.join(out, 'l.wav'))),
    ('write via old hard link', lambda: open(alias_hard, 'r+b')),
    ('write via symlink', lambda: open(alias_soft, 'wb')),
    ('patch BWF header in place', lambda: patch_time_reference(src, 0)),
    ('shutil.copyfile over it', lambda: shutil.copyfile(os.path.join(out, 'l2.wav') if open(os.path.join(out, 'l2.wav'), 'wb').close() is None else '', src)),
    ('ffmpeg output onto it', lambda: subprocess.run([ff, '-y', '-f', 'lavfi', '-i', 'anullsrc', '-t', '1', src], capture_output=True)),
    ('new file inside the shoot folder (not output)', lambda: open(os.path.join(snd, 'stray.txt'), 'w')),
    ('delete the sound folder', lambda: shutil.rmtree(snd)),
    ('move the sound folder', lambda: shutil.move(snd, os.path.join(root, 'moved'))),
]
ok = True
for name, fn in attempts:
    try:
        fn()
        print(f'  NOT BLOCKED: {name}'); ok = False
    except PermissionError as e:
        print(f'  blocked: {name}')
# allowed: writing in the output folder, cloning a copy
open(os.path.join(out, 'fine.txt'), 'w').write('ok')
safety.clone_or_copy(src, os.path.join(out, 'copy.wav'))
open(os.path.join(out, 'copy.wav'), 'r+b').write(b'XXXX')          # writing the copy can't reach the original
after = fp(src)
print('original unchanged:', before == after, '| stray files in SOUND:', sorted(os.listdir(snd)))
ok = ok and before == after and sorted(os.listdir(snd)) == ['T001.WAV']
print('refused and logged:', len(safety.summary()['blocked']))
print('GUARD GOOD' if ok else 'GUARD FAILED')
