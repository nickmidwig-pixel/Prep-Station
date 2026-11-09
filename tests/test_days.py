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


def mov(path, rate, secs, tc, created, audio=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = [FF, '-y', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc=size=160x90:rate={rate}']
    if audio:
        cmd += ['-f', 'lavfi', '-i', 'anoisesrc=sample_rate=48000:amplitude=0.1']
    cmd += ['-t', str(secs), '-timecode', tc, '-c:v', 'libx264', '-preset', 'ultrafast', '-metadata', f'creation_time={created}']
    cmd += (['-c:a', 'pcm_s24le'] if audio else ['-an']) + [path]
    subprocess.run(cmd, check=True)


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
    # Canon S&F in XF-AVC: no sound, plays 23.976, name says the take started 00:20:00 and the (end) stamp says
    # it was over 6 s later, yet the file runs 20 s -> shot faster than it plays
    mov(f'{D}/BCAM_C80/B01/CONTENTS/CLIPS001/B_0001C040A250209_002000SF_CANON.MOV', '24000/1001', 20, '00:20:00:00', '2025-02-09T05:20:06.000000Z', audio=False)
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
assert len(b) == 4, [c['roll'] for c in p.clips]
sf = next(c for c in b if 'C040' in c['name'])
assert sf.get('slowmo') and sf['slowmo']['kind'] == 'clock', sf.get('slowmo')
assert all(not c.get('slowmo') for c in b if 'C040' not in c['name']), [(c['name'], c.get('slowmo')) for c in b]
night = next(c for c in p.clips if '002102' in c['name'])
assert night.get('date_note') == 'recorded after midnight' and night.get('after_midnight')
cams = [l for l in p.lanes() if l['kind'] == 'camera']
assert [l['letter'] for l in cams] == ['A', 'B'], cams

# two real days dropped as separate folders must NOT be merged: camera-only day right after a sound day
E = '/home/claude/dayshape2'
if not os.path.exists(E + '/.made'):
    shutil.rmtree(E, ignore_errors=True)
    shutil.copytree(D + '/SOUND', E + '/SOUND')
    shutil.copytree(D + '/BCAM_C80', E + '/BCAM_C80')
    for i, tc in enumerate(['20:30:00:00', '21:00:00:00', '21:30:00:00']):
        mov(f'{E}/CCAM/C01/CONTENTS/CLIPS001/C001C00{i+1}.MOV', '24000/1001', 3, tc, f'2025-02-10T0{1+i}:30:00.000000Z')
    open(E + '/.made', 'w').close()
q = Project()
q.add_paths([E + '/SOUND', E + '/BCAM_C80', E + '/CCAM'])
print([(d['key'], d['n'], d['rolls']) for d in q.days()])
assert [d['key'] for d in q.days()] == ['2025-02-08', '2025-02-09'], q.days()

# ... but the same material dropped as ONE undated folder is one day (the whole day went into one folder)
q1 = Project()
q1.add_paths([E])
print([(d['key'], d['n'], d['rolls']) for d in q1.days()])
assert [d['key'] for d in q1.days()] == ['2025-02-08'], q1.days()
assert all(c.get('date_note') == 'camera date says 2025-02-09' for c in q1.clips if c['roll'] == 'C01')

# the dropped folder's name is the master date: every clock in it is overruled, even the sound recorder's
F = '/home/claude/dayshape_20250301'
if not os.path.exists(F + '/.made'):
    shutil.rmtree(F, ignore_errors=True)
    shutil.copytree(D + '/SOUND', F + '/SOUND')
    shutil.copytree(D + '/ACAM_C500II', F + '/ACAM_C500II')
    open(F + '/.made', 'w').close()
r = Project()
r.add_paths([F])
print([(d['key'], d['n'], d['rolls']) for d in r.days()])
assert [d['key'] for d in r.days()] == ['2025-03-01'], r.days()
assert all(c.get('day_forced') == 'dayshape_20250301' for c in r.clips)
notes = {c['roll']: c.get('date_note') for c in r.clips}
assert notes['A01'] == 'camera date says 2019-04-27' and notes['01LAV'] == 'recorder date says 2025-02-08', notes
# a dated folder one level under an undated drop counts too, and two of them stay two days
G = '/home/claude/dayshape_parent'
if not os.path.exists(G + '/.made'):
    shutil.rmtree(G, ignore_errors=True)
    shutil.copytree(D + '/SOUND', G + '/20250208/SOUND')
    shutil.copytree(E + '/CCAM', G + '/20250210/CCAM')
    open(G + '/.made', 'w').close()
g = Project()
g.add_paths([G])
print([(d['key'], d['n'], d['rolls']) for d in g.days()])
assert [d['key'] for d in g.days()] == ['2025-02-08', '2025-02-10'], g.days()
print('DAYS GOOD')

# unlabelled bodies are lettered by footage: a C80 with most of the day is B next to a labelled A, a phone with a
# few clips is C — whichever of them the reference happens to be
H = '/home/claude/dayshape_letters'
if not os.path.exists(H + '/.made'):
    shutil.rmtree(H, ignore_errors=True)
    for i in range(3):
        mov(f'{H}/ACAM/A01/CONTENTS/CLIPS001/A001C00{i+1}.MOV', '24000/1001', 3, f'20:{i:02d}:00:00', f'2025-02-09T01:{i:02d}:00.000000Z')
    for i in range(6):
        mov(f'{H}/C80 card/CONTENTS/CLIPS001/CLIP_{i:04d}.MOV', '24000/1001', 20, f'20:{10+i:02d}:00:00', f'2025-02-09T01:{10+i:02d}:00.000000Z')
    for i in range(2):
        mov(f'{H}/iphone/IMG_447{i}.MP4', '24000/1001', 3, f'20:{20+i:02d}:00:00', f'2025-02-09T01:{20+i:02d}:00.000000Z')
    open(H + '/.made', 'w').close()
h = Project()
h.add_paths([H])
cams = {l['letter']: l['rolls'] for l in h.lanes() if l['kind'] == 'camera'}
print(cams)
assert cams['A'] == ['A01'] and cams['B'] == ['C80 card'] and cams['C'] == ['iphone'], cams
print('LETTERS GOOD')
