"""Canon Cinema RAW Light (.CRM): timecode from the tmcd box (ffmpeg can't see it), body serial and model from
Canon's uuid box, camera letters from the card folder (UDP_261106_C80_B_001) or the clip name (C_0001C002),
one lane per body across cards, and a lossless timecode copy. Originals untouched.

Real clips: set REAL_CANON to a folder holding A005C004*.CRM and C_0001C002*.CRM (read-only) to check
them too: 17:17:58:07 / 10:56:37:05 and serials 582022100067 / 012919000260. Checked on the real drive on
2026-11-06 with the same reader: both match (C500 Mark II firmware 1.1.3.1, C80 firmware 1.0.6.1; Canon's uuid
box holds EXIF-style CMT1-CMT4 blocks, the body serial is in CMT2)."""
import glob, hashlib, os, shutil, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app'))
sys.path.insert(0, HERE)
import make_canon_raw
from midwigsync import media, safety, stamp as ST, tc as TC
from midwigsync.scan import Project

make_canon_raw.build()
ok = True


def check(cond, what):
    global ok
    ok &= bool(cond)
    print(f"  {'ok ' if cond else 'BAD'} {what}")


def sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 22), b''):
            h.update(b)
    return h.hexdigest()


want = {'A001C001': ('10:49:02:08', '582022100067', 'C500 II'), 'A002C001': ('11:20:00:00', '582022100067', 'C500 II'),
        'C_0001C001': ('10:49:13:18', '012919000260', 'C80'), 'C_0002C002': ('10:56:37:05', '012919000260', 'C80')}
files = sorted(glob.glob('/home/claude/canonday/*/CRM/*/*.CRM'))
hashes = {f: sha(f) for f in files + glob.glob('/home/claude/canonday_plain/*/CRM/*/*.CRM')}
print('probe')
for f in files:
    i = media.probe(f)
    tc, serial, cam = next(v for k, v in want.items() if os.path.basename(f).startswith(k))
    got = TC.samples_to_tc(i['start'], i['fps']) if i['start'] is not None else None
    check(got == tc and i['tc_source'] == 'qt-tmcd' and i.get('serial') == serial and i['camera'] == cam
          and TC.rate_label(i['fps']) == '23.976' and i['nframes'] == 96,
          f"{os.path.basename(f):30s} {got} {i.get('serial')} {i['camera']} {TC.rate_label(i['fps'])} {i['nframes']} frames")

print('lanes, letters in the card folders')
p = Project(); p.add_paths(['/home/claude/canonday']); p.analyze()
lanes = [(l['label'], l['sub'], l['rolls']) for l in p.lanes() if l['kind'] == 'camera']
check(lanes == [('A CAM', 'C500 II · 2 cards', ['UDP_261106_C500_A_001', 'UDP_261106_C500_A_002']),
                ('B CAM', 'C80 · 2 cards', ['UDP_261106_C80_B_001', 'UDP_261106_C80_B_002'])], lanes)
check(p.fps_info['label'] == '23.976' and p.fps_info['status'] == 'ok', f"frame rate {p.fps_info['label']} {p.fps_info['status']}")

print('lanes, no letters in the folders (grouped by body serial, letters from the clip names)')
q = Project(); q.add_paths(['/home/claude/canonday_plain']); q.analyze()
lanes = [(l['label'], l['sub'], l['rolls']) for l in q.lanes() if l['kind'] == 'camera']
check(lanes == [('A CAM', 'C500 II · 2 cards', ['Card1', 'Card2']), ('C CAM', 'C80 · 2 cards', ['Card3', 'Card4'])], lanes)

print('timecode copy')
out = '/tmp/canon_copy'
shutil.rmtree(out, ignore_errors=True)
safety.allow_output(out)
c = next(c for c in p.clips if c['name'].startswith('C_0002'))
new = c['orig_start'] + TC.frames_to_samples(39 * 24, c['fps'])
path, how = ST.copy_with_tc(c, out, new)
a, b = open(c['path'], 'rb').read(), open(path, 'rb').read()
diff = sum(x != y for x, y in zip(a, b)) if len(a) == len(b) else -1
got = media.probe(path)
check(TC.samples_to_tc(got['start'], got['fps']) == '10:57:16:05' and 0 < diff <= 4 and got.get('serial') == '012919000260',
      f"{c['name']} -> {TC.samples_to_tc(got['start'], got['fps'])} ({how}; {diff} bytes differ)")

print('camera pictures and fast audio')
import numpy as np
c0 = p.clips[0]
pv, th = media.embedded_preview(c0['path']), media.embedded_preview(c0['path'], 'thmb')
check(pv and th and pv[:3] == th[:3] == b'\xff\xd8\xff', f'preview {len(pv or b"")} bytes, thumbnail {len(th or b"")} bytes, both JPEG')
for ch in range(media.total_audio_channels(c0)):
    os.environ['PREP_SLOW_AUDIO'] = '1'; slow = np.concatenate([b for _, b in media.iter_audio(c0, ch)])
    os.environ['PREP_SLOW_AUDIO'] = '0'; fast = np.concatenate([b for _, b in media.iter_audio(c0, ch)])
    check(np.array_equal(slow, fast), f'ch{ch + 1}: audio read from the sample table = audio read through ffmpeg ({len(fast)} samples)')
from midwigsync.gui import Api
api = Api(); api.p = p
c0['raw_picture'] = True                     # as on real Canon RAW: ffmpeg can't decode the picture
fr = api.frame(c0['start'] + 1000, c0['id'])
check(fr['ok'] and fr.get('preview') and fr['img'].startswith('data:image/jpeg'), f"viewer shows the camera preview: {fr.get('label')}")
check(fr.get('raw') and fr.get('cid') == c0['id'], 'viewer offers to make a RAW clip playable')
# a viewing copy (stand-in for Resolve's render) -> the picture anywhere in the clip, from the copy
from midwigsync.brand import CACHE
from midwigsync.convert import ffmpeg_exe
import subprocess
vk = api._view_key(c0)
vdir = os.path.join(CACHE, 'rawview')
os.makedirs(vdir, exist_ok=True)
vp = os.path.join(vdir, vk + '.mov')
subprocess.run([ffmpeg_exe(), '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=24000/1001',
                '-t', '4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', vp], check=True)
api.__dict__.pop('_frames', None)
fr2 = api.frame(c0['start'] + 2 * 48000, c0['id'])
check(fr2['ok'] and 'viewing copy' in fr2['label'] and not fr2.get('preview') and fr2['img'].startswith('data:image/jpeg'),
      f"with a viewing copy: {fr2.get('label')}")
r = api.raw_copies([c0['id']])
check(r.get('ok') and r.get('n') == 0, 'no second copy of a clip that has one')
os.remove(vp)
api.__dict__.pop('_frames', None)
th = api.thumbs([c['id'] for c in p.clips])
check(len(th) == 4 and all(v and v.startswith('data:image/jpeg') for v in th.values()), f'{len(th)} thumbnails')
c0['raw_picture'] = False

print('reference camera = every card of that body')
from midwigsync.pipeline import Job
from midwigsync import refpick
jb = Job(p, dict(job_name='REFBODY', out_dir='/home/claude/canonday_REFBODY_SYNCED', aap_mode='skip',
                 reference=dict(roll='UDP_261106_C80_B_001', channel=0)), lambda k, d: None)
rr = jb.ref()
check(sorted(rr['rolls']) == ['UDP_261106_C80_B_001', 'UDP_261106_C80_B_002'],
      f"B CAM card 1 picked -> reference reads {len(rr['rolls'])} cards: {jb.ref_label(rr)}")
from midwigsync.wavesync import SyncAnalyzer
an = SyncAnalyzer(jb.p, ref=rr)
b2 = next(c for c in jb.p.clips if c['roll'] == 'UDP_261106_C80_B_002')
a1 = next(c for c in jb.p.clips if c['roll'] == 'UDP_261106_C500_A_001')
check(an._ch(b2) == 0 and an._ch(a1) == 0, 'the second card is listened to on the reference channel')
pk = refpick.pick_camera_reference(jb.p, log=lambda m: None)
check(pk and pk['roll'] in {c['roll'] for c in jb.p.clips}, f"automatic pick names a real card: {pk and pk['roll']}")

real = os.environ.get('REAL_CANON')
if real:
    print('real clips (read only)')
    for stem, tc, serial in (('A005C004', '17:17:58:07', '582022100067'), ('C_0001C002', '10:56:37:05', '012919000260')):
        found = glob.glob(os.path.join(real, '**', stem + '*.CRM'), recursive=True)
        if not found:
            check(False, f'{stem}: not found under {real}')
            continue
        info = media.qt_info(found[0])
        q = media.qt_timecode(info)
        check(q and q[0] == tc and info.get('serial') == serial,
              f"{stem}: {q and q[0]} {info.get('serial')} {info.get('model')!r} {info.get('video_frames')} frames")

print('originals')
check(all(sha(f) == h for f, h in hashes.items()), f'{len(hashes)} originals unchanged')
print('CANON RAW GOOD' if ok else 'CANON RAW FAILED')
sys.exit(0 if ok else 1)
