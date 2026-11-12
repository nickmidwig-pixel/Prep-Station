"""A copy that was cut short (the file is smaller than its own header says) is flagged at load, left out on its
own, can't be ticked back in, and never reaches the listening step; whole files are not flagged."""
import os, shutil, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync.scan import Project
from midwigsync import convert, media, refpick

FF = convert.ffmpeg_exe()


def mov(path, secs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run([FF, '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc=s=320x180:r=24000/1001:d={secs}',
                    '-f', 'lavfi', '-i', f'anoisesrc=d={secs}:r=48000', '-c:v', 'mjpeg', '-c:a', 'pcm_s16le',
                    '-timecode', '09:00:00:00', '-movflags', '+faststart', path], check=True)


D = tempfile.mkdtemp(prefix='incomplete_')
try:
    cam = os.path.join(D, '20261111', 'B CAM')
    whole, cut = f'{cam}/A001_C001.mov', f'{cam}/A001_C002.mov'
    mov(whole, 6); mov(cut, 6)
    full = os.path.getsize(cut)
    with open(cut, 'r+b') as f:
        f.truncate(full * 2 // 3)                     # the copy stopped two thirds of the way through
    wav = f'{D}/20261111/SOUND/x.wav'
    os.makedirs(os.path.dirname(wav))
    subprocess.run([FF, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'anoisesrc=d=3:r=48000', wav], check=True)
    wfull = os.path.getsize(wav)
    assert media.missing_bytes(whole) == 0 and media.missing_bytes(wav) == 0
    assert media.missing_bytes(cut) == full - full * 2 // 3, media.missing_bytes(cut)
    with open(wav, 'r+b') as f:
        f.truncate(wfull // 2)
    assert media.missing_bytes(wav) == wfull - wfull // 2
    assert media.missing_bytes(os.path.join(D, 'nope.mov')) == 0

    before = sorted(os.listdir(cam))
    p = Project()
    p.add_paths([os.path.join(D, '20261111')])
    by = {c['name']: c for c in p.clips}
    assert 'incomplete' in by['A001_C002.mov']['flags'] and not by['A001_C002.mov']['include']
    assert 'incomplete' not in by['A001_C001.mov']['flags'] and by['A001_C001.mov']['include']
    j = [c for c in p.to_json()['clips'] if c['name'] == 'A001_C002.mov'][0]
    assert j['missing_bytes'] > 0 and 'incomplete' in j['flags']

    heard = []
    t = time.time()
    refpick.pick_camera_reference(p, log=lambda *a: None, progress=lambda i, n, m: heard.append(m))
    assert time.time() - t < 60
    assert not any('C002' in m for m in heard), heard          # never listened to
    assert sorted(os.listdir(cam)) == before and os.path.getsize(cut) == full * 2 // 3   # nothing touched
    print('incomplete files OK:', heard)
finally:
    shutil.rmtree(D, ignore_errors=True)
