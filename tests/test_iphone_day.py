"""An iPhone day (20261110 shape): a C80 at 23.976, an iPhone in the Blackmagic app at 24.000 with timecode,
and an iPhone camera app with no timecode whose files were copied twice ("IMG_8602.MOV" + "IMG_8602 2.MOV").
24.000 on a 23.976 day is real time, not slow motion; the " 2" copies are duplicates, left out, never deleted."""
import os, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync.scan import Project
from midwigsync import convert

FF = convert.ffmpeg_exe()


def mov(path, rate, secs, tc=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = [FF, '-y', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc=size=160x90:rate={rate}',
           '-f', 'lavfi', '-i', 'anoisesrc=sample_rate=48000:amplitude=0.1', '-t', str(secs)]
    if tc:
        cmd += ['-timecode', tc]
    cmd += ['-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', path]
    subprocess.run(cmd, check=True)


D = tempfile.mkdtemp(prefix='iphoneday_')
try:
    day = os.path.join(D, '20261110')
    mov(f'{day}/A_CAM/A_0002C001X261110_0859492N_CANON.MOV', '24000/1001', 6, '07:58:29:04')
    mov(f'{day}/B_CAM_IPHONE/A001_11100852_C020.mov', '24', 6, '08:52:01:00')
    mov(f'{day}/C_CAM_IPHONE/IMG_8602.MOV', '24', 7)
    shutil.copy(f'{day}/C_CAM_IPHONE/IMG_8602.MOV', f'{day}/C_CAM_IPHONE/IMG_8602 2.MOV')
    before = sorted(os.listdir(f'{day}/C_CAM_IPHONE'))
    p = Project()
    p.add_paths([day])
    p.check_short_clips(20)
    by = {c['name']: c for c in p.clips}
    b = by['A001_11100852_C020.mov']
    assert not b.get('slowmo'), ('24 on a 23.976 day went to slow motion', b.get('slowmo'))
    assert 'rate_near' in b['flags'], b['flags']
    one, two = by['IMG_8602.MOV'], by['IMG_8602 2.MOV']
    assert not one.get('slowmo') and not two.get('slowmo')
    assert two.get('duplicate_of') and not two['include'], ('copy not caught', two.get('duplicate_of'), two['include'])
    assert not one.get('duplicate_of') and one['include'], 'kept the copy instead of the plain name'
    assert sorted(os.listdir(f'{day}/C_CAM_IPHONE')) == before
    print('IPHONE DAY GOOD')
finally:
    shutil.rmtree(D, ignore_errors=True)
