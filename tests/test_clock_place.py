"""A camera clip with no timecode whose sound matches nothing (here: no sound at all) is placed at the time of day
its own clock wrote, flagged as approximate, offered for a timecode copy like any other fix; one whose creation
date has no local offset is left unplaced. Originals untouched."""
import os, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync import convert, tc as TC
from midwigsync.pipeline import Job
from midwigsync.scan import Project
FF = convert.ffmpeg_exe()
D = tempfile.mkdtemp(prefix='clockplace_')
try:
    day = os.path.join(D, '20261110')
    def mov(path, tc=None, created=None, audio=True):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cmd = [FF, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=160x90:rate=24000/1001']
        if audio: cmd += ['-f', 'lavfi', '-i', 'anoisesrc=sample_rate=48000:amplitude=0.1']
        cmd += ['-t', '8']
        if tc: cmd += ['-timecode', tc]
        if created: cmd += ['-metadata', f'com.apple.quicktime.creationdate={created}', '-movflags', 'use_metadata_tags']
        cmd += ['-c:v', 'libx264', '-preset', 'ultrafast'] + (['-c:a', 'pcm_s16le'] if audio else ['-an']) + [path]
        subprocess.run(cmd, check=True)
    mov(f'{day}/A_CAM/A001C001.MOV', tc='09:00:00:00')
    mov(f'{day}/C_CAM_IPHONE/IMG_0001.MOV', created='2026-11-10T09:17:30-0500', audio=False)
    mov(f'{day}/C_CAM_IPHONE/IMG_0002.MOV', created='2026-11-10T14:17:30Z', audio=False)
    p = Project(); p.add_paths([day]); p.check_short_clips(20)
    by = {c['name']: c for c in p.clips}
    a, b = by['IMG_0001.MOV'], by['IMG_0002.MOV']
    assert a.get('created') and a['start'] is None, ('creation date not read', a.get('created'))
    want = TC.tc_to_samples('09:17:30:00', p.fps, False)
    assert p._clock_place(a) == want and p._clock_place(b) is None
    names = sorted(os.listdir(f'{day}/C_CAM_IPHONE'))
    ev = []
    j = Job(p, dict(job_name='20261110', out_dir=os.path.join(D, 'out'), aap_mode='off', review_tc=False, transcribe=False),
            lambda k, d: ev.append((k, d)))
    j.run()
    err = [d for k, d in ev if k == 'error']
    assert not err, err
    by = {c['name']: c for c in j.p.clips}
    a, b = by['IMG_0001.MOV'], by['IMG_0002.MOV']
    assert a['start'] == want and 'sync_clock' in a['flags'], (a['start'], a['flags'])
    assert b['start'] is None, 'a clip with no local clock time was placed anyway'
    assert any(f['name'] == 'IMG_0001.MOV' for f in j.camera_fixes()), 'not offered for a timecode copy'
    assert sorted(os.listdir(f'{day}/C_CAM_IPHONE')) == names
    print('CLOCK PLACE GOOD')
finally:
    shutil.rmtree(D, ignore_errors=True)
