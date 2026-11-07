"""Slow motion never lands on the sync timeline: C500-style 59.94 time base (with sound), Sony S&Q (XML says
captureFps 119.88), a silent clip whose TC runs into the next, a real-time silent clip that must stay, and a
29.97 clip on a 23.976 day. Plus a mark / unmark by hand. Then a job run: separate XML, main XML without them."""
import os, shutil, subprocess, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync.scan import Project
from midwigsync.pipeline import Job
from midwigsync import convert, safety, tc as TC

D = '/home/claude/slowday'
FF = convert.ffmpeg_exe()
def mov(path, rate, secs, tc, audio=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = [FF, '-y', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc=size=160x90:rate={rate}']
    if audio:
        cmd += ['-f', 'lavfi', '-i', 'anoisesrc=sample_rate=48000:amplitude=0.1']
    cmd += ['-t', str(secs), '-timecode', tc, '-c:v', 'libx264', '-preset', 'ultrafast']
    cmd += (['-c:a', 'pcm_s24le'] if audio else ['-an']) + [path]
    subprocess.run(cmd, check=True)

if not os.path.exists(D + '/.made'):
    shutil.rmtree(D, ignore_errors=True)
    mov(f'{D}/A_FX6/A001C001.MOV', '24000/1001', 8, '10:00:00:00')
    mov(f'{D}/A_FX6/A001C002.MOV', '24000/1001', 8, '10:01:00:00')
    mov(f'{D}/B_C500/B001C001.MOV', '24000/1001', 8, '10:00:02:00')
    mov(f'{D}/B_C500/B001C002.MOV', '60000/1001', 6, '10:00:30:00')          # time base switched to 59.94, has sound
    mov(f'{D}/C_FX3/C0001.MP4', '24000/1001', 20, '10:00:10:00', audio=False)  # S&Q: Sony XML says 119.88
    open(f'{D}/C_FX3/C0001M01.XML', 'w').write('<?xml version="1.0"?><NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.00">'
        '<VideoFormat><VideoFrame videoCodec="AVC_3840_2160_HP@L51" captureFps="119.88p" formatFps="23.98p"/></VideoFormat></NonRealTimeMeta>')
    mov(f'{D}/C_FX3/C0002.MP4', '24000/1001', 30, '10:00:40:00', audio=False)  # silent, 30 s of TC ...
    mov(f'{D}/C_FX3/C0003.MP4', '24000/1001', 6, '10:00:50:00', audio=False)   # ... but the next starts 10 s later
    mov(f'{D}/C_FX3/C0004.MP4', '24000/1001', 6, '10:02:00:00', audio=False)   # silent, real time: stays
    mov(f'{D}/D_GOPRO/GX010001.MP4', '30000/1001', 6, '10:00:20:00')           # 29.97 on a 23.976 day
    open(D + '/.made', 'w').write('1')

p = Project(); p.add_paths([D]); p.analyze()
want = {'A001C001.MOV': None, 'A001C002.MOV': None, 'B001C001.MOV': None, 'B001C002.MOV': 'timebase',
        'C0001.MP4': 'in_camera', 'C0002.MP4': 'likely', 'C0003.MP4': None, 'C0004.MP4': 'likely' if False else None,
        'GX010001.MP4': 'rate'}
ok = True
print('frame rate:', p.fps_info['label'], p.fps_info['status'], '|', p.fps_info['reason'])
ok &= p.fps_info['label'] == '23.976' and p.fps_info['status'] == 'ok'
for c in sorted(p.clips, key=lambda c: c['name']):
    got = (c.get('slowmo') or {}).get('kind')
    good = got == want[c['name']]
    ok &= good
    print(f"  {'ok ' if good else 'BAD'} {c['roll']:8s} {c['name']:14s} {TC.rate_label(c['fps']):7s} audio={bool(c.get('audio_streams'))!s:5s} -> {got}  {(c.get('slowmo') or {}).get('why', '')}")
# by hand: unmark the 'likely' one, mark a normal one
c2 = next(c for c in p.clips if c['name'] == 'C0002.MP4'); c2['slowmo_user'] = False
c4 = next(c for c in p.clips if c['name'] == 'C0004.MP4'); c4['slowmo_user'] = True
p.analyze()
ok &= c2['slowmo'] is None and (c4['slowmo'] or {}).get('kind') == 'marked'
print('  by hand: C0002 unmarked ->', c2['slowmo'], '| C0004 marked ->', (c4['slowmo'] or {}).get('kind'))
c2['slowmo_user'] = None; c4['slowmo_user'] = None; p.analyze()

out = D + '/SLOWDAY_SYNCED'
safety.allow_output(out); shutil.rmtree(out, ignore_errors=True)
s = dict(job_name='SLOWDAY', out_dir=out, aap_mode='skip', reference=dict(roll='A_FX6', channel=0), synced_clips=False)
errs = []
j = Job(p, s, lambda k, d: errs.append(d) if k == 'error' else None)
j.run()
ok &= not errs
main = open(f'{out}/EDITORIAL/SLOWDAY_SYNC_ISO.xml').read() if os.path.exists(f'{out}/EDITORIAL/SLOWDAY_SYNC_ISO.xml') else ''
slow = open(f'{out}/EDITORIAL/SLOWDAY_SLOW_MOTION.xml').read()
slow_names = ['B001C002.MOV', 'C0001.MP4', 'C0002.MP4', 'GX010001.MP4']
in_main = [n for n in slow_names if n in main]
in_slow = [n for n in slow_names if n in slow]
print('  sync XML has slow clips:', in_main or 'none', '| normal clips in sync XML:', all(n in main for n in ('A001C001.MOV', 'B001C001.MOV', 'C0003.MP4', 'C0004.MP4')))
print('  slow-motion XML has:', in_slow, '| markers:', slow.count('<marker>'))
ok &= not in_main and in_slow == slow_names and slow.count('<marker>') == 4
import re
starts = {m.group(1): int(m.group(2)) for m in re.finditer(r'<clipitem id="[^"]+"><name>([^<]+)</name>.*?<start>(\d+)</start>', slow)}
order = [starts[n] for n in ['C0001.MP4', 'GX010001.MP4', 'B001C002.MOV', 'C0002.MP4']]
print('  shot order kept:', order == sorted(order))
ok &= order == sorted(order)
rep = open(j.report_path).read()
ok &= 'SLOW MOTION + OTHER FRAME RATES' in rep and 'Frame rate: 23.976' in rep
print('  report section:', 'SLOW MOTION + OTHER FRAME RATES' in rep)
# the B-cam's 59.94 sound was never used to check timecode
used = [c['name'] for c in p.clips if (c.get('sync') or {}).get('against') and c['name'] in slow_names]
print('  slow clips touched by the timecode check:', used or 'none')
ok &= not used and not any(c.get('sync') for c in p.clips if c['name'] in slow_names)
print('SLOWMO GOOD' if ok else 'SLOWMO FAILED')
