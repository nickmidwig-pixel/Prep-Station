"""A phone clip with dropped frames measures 23.75 fps, but it was SET to 24 and its timecode counts at 24:
timecode converts at 24 (not the measured rate), the length stays the real length, and two clips shot 27 s
apart sit 27 s apart, in order, with no slow-motion flag."""
import os, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync import convert, media, tc as TC
from midwigsync.scan import Project
FF = convert.ffmpeg_exe()
D = tempfile.mkdtemp(prefix='wander_')
try:
    b = os.path.join(D, '20261110', 'B_CAM'); os.makedirs(b)
    base = [FF, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=160x90:rate=24', '-f', 'lavfi', '-i',
            'anoisesrc=sample_rate=48000:amplitude=0.1', '-t', '20']
    enc = ['-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac']
    subprocess.run(base + ['-timecode', '09:04:10:00'] + enc + [f'{b}/A001_11100904_C024.mov'], check=True)
    subprocess.run(base + ['-vf', "select='not(eq(mod(n\\,97)\\,5))'", '-fps_mode', 'vfr', '-timecode', '09:04:37:00'] + enc
                   + [f'{b}/A001_11100904_C025.mov'], check=True)
    i = media.probe(f'{b}/A001_11100904_C025.mov')
    assert float(i['fps']) == 24.0 and 23.5 < float(i['fps_measured']) < 23.95, (i['fps'], i.get('fps_measured'))
    assert TC.samples_to_tc(i['start'], i['fps']) == '09:04:37:00'
    assert abs(i['length'] / 48000 - 20.0) < 0.2, i['length'] / 48000
    p = Project(); p.add_paths([os.path.join(D, '20261110')]); p.check_short_clips(20)
    by = {c['name']: c for c in p.clips}
    a, c = by['A001_11100904_C024.mov'], by['A001_11100904_C025.mov']
    assert abs((c['start'] - a['start']) / 48000 - 27.0) < 0.05, (c['start'] - a['start']) / 48000
    assert not c.get('slowmo') and 'tc_overlap' not in c['flags'] and 'tc_overlap' not in a['flags'], (c.get('slowmo'), c['flags'])
    print('WANDER RATE GOOD')
finally:
    shutil.rmtree(D, ignore_errors=True)
