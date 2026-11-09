"""Shoot-day grouping on a real night shape (the 20250208 RODEO backup): A cam (C500 II) with its clock reset to
2019 and holding more than a quarter of the dated clips, B cam (C80) with CONTENTS + CRM + XFVC folders, Zaxcom
ISOs stamped with the evening's date past midnight. Everything must land in ONE day, A and B as two cameras."""
import datetime as dt
import os, shutil, subprocess, sys
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync.scan import Project
from midwigsync import bwf, convert, tc as TC

D = '/home/claude/dayshape'
FF = convert.ffmpeg_exe()
FPS = TC.parse_rate('23.976')


def mov(path, rate, secs, tc, created):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run([FF, '-y', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc=size=160x90:rate={rate}',
                    '-f', 'lavfi', '-i', 'anoisesrc=sample_rate=48000:amplitude=0.1', '-t', str(secs),
                    '-timecode', tc, '-c:v', 'libx264', '-preset', 'ultrafast', '-metadata', f'creation_time={created}',
                    '-c:a', 'pcm_s24le', path], check=True)


def wav(path, tc, secs, ubits):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    start = TC.tc_to_samples(tc, FPS)
    desc = f'sTRK1=LAV\r\ndTC={tc}\r\ndUBITS=${ubits}\r\ndFRAMERATE=23.976ND\r\n'
    be = bwf.build_bext(desc, 'ZAXCOM', 'ZAX1', '0000-00-00', tc[:8], start, b'A=PCM,F=48000,W=24,M=1,T=ZAXFILE\r\n')
    w = bwf.WavWriter(path, 48000 * secs, 1, 48000, 24, bext=be, ixml=None)
    w.write_float(0.05 * np.random.default_rng(1).standard_normal(48000 * secs).astype(np.float32))
    w.close()


if not os.path.exists(D + '/.made'):
    shutil.rmtree(D, ignore_errors=True)
    # A cam: 6 clips, clock says 27 Apr 2019, timecode is the real evening
    for i, tc in enumerate(['20:32:26:11', '20:40:36:01', '20:55:16:14', '21:26:01:18', '22:08:48:15', '22:16:25:10']):
        mov(f'{D}/ACAM_C500II/A01/CRM/REEL_001/A001C00{i+1}_190427XX_CANON.MOV', '24000/1001', 3, tc,
            f'2019-04-27T1{2+i//3}:1{i}:00.000000Z')
    # B cam: three folders of one card, clock right, after midnight
    mov(f'{D}/BCAM_C80/B01/CONTENTS/CLIPS001/B_0001C001A250208_232926IE_CANON.MOV', '24000/1001', 3, '23:28:46:00', '2025-02-09T04:29:51.000000Z')
    mov(f'{D}/BCAM_C80/B01/CRM/REEL_0001/B_0001C005X250209_001653ZD_CANON.MOV', '24000/1001', 3, '00:16:13:00', '2025-02-09T05:16:53.000000Z')
    mov(f'{D}/BCAM_C80/B01/XFVC/REEL_0001/B_0001C017H250209_011313RI_CANON.MOV', '24000/1001', 3, '01:12:02:03', '2025-02-09T06:13:13.000000Z')
    # sound: Zaxcom, evening files and one past midnight still stamped 20250208
    wav(f'{D}/SOUND/01LAV/20250208_201748_01LAV_001.WAV', '20:17:48:00', 4, '20250208')
    wav(f'{D}/SOUND/01LAV/20250208_225017_01LAV_003.WAV', '22:50:17:00', 4, '20250208')
    wav(f'{D}/SOUND/03_LAV/20250208_002102_03_LAV_006.WAV', '00:21:02:00', 4, '20250208')
    open(D + '/.made', 'w').close()

p = Project()
p.add_paths([D])
days = p.days()
print([(d['key'], d['n'], d['rolls']) for d in days])
assert len(days) == 1 and days[0]['key'] == '2025-02-08', days
assert all(c['day'] == '2025-02-08' for c in p.clips)
a = [c for c in p.clips if c['roll'] == 'A01']
assert len(a) == 6 and all(c.get('date_note') == 'camera date says 2019-04-27' for c in a)
b = [c for c in p.clips if c['roll'] == 'B01']
assert len(b) == 3, [c['roll'] for c in p.clips]
night = next(c for c in p.clips if '002102' in c['name'])
assert night.get('date_note') == 'recorded after midnight' and night.get('after_midnight')
cams = [l for l in p.lanes() if l['kind'] == 'camera']
assert [l['letter'] for l in cams] == ['A', 'B'], cams

# a real two-day load must NOT be merged: camera-only day right after a sound day
E = '/home/claude/dayshape2'
if not os.path.exists(E + '/.made'):
    shutil.rmtree(E, ignore_errors=True)
    shutil.copytree(D + '/SOUND', E + '/SOUND')
    shutil.copytree(D + '/BCAM_C80', E + '/BCAM_C80')
    for i, tc in enumerate(['20:30:00:00', '21:00:00:00', '21:30:00:00']):
        mov(f'{E}/CCAM/C01/CONTENTS/CLIPS001/C001C00{i+1}.MOV', '24000/1001', 3, tc, f'2025-02-10T0{1+i}:30:00.000000Z')
    open(E + '/.made', 'w').close()
q = Project()
q.add_paths([E])
print([(d['key'], d['n'], d['rolls']) for d in q.days()])
assert [d['key'] for d in q.days()] == ['2025-02-08', '2025-02-09'], q.days()
print('DAYS GOOD')
