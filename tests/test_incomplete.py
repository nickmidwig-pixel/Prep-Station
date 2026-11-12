"""A copy that was cut short (the file is smaller than its own header says) is flagged at load, kept in the day
(what reads is still synced) and called out; whole files are not flagged. Listening jumps straight to the spot it
needs (same samples as reading from the top), and a read that stops answering is abandoned after the time limit."""
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
    assert 'incomplete' in by['A001_C002.mov']['flags'] and by['A001_C002.mov']['include']     # stays in
    assert 'incomplete' not in by['A001_C001.mov']['flags'] and by['A001_C001.mov']['include']
    j = [c for c in p.to_json()['clips'] if c['name'] == 'A001_C002.mov'][0]
    assert j['missing_bytes'] > 0 and 'incomplete' in j['flags']

    # jumping to a spot gives exactly the samples that reading from the top gives: PCM in MOV, AAC in MP4, WAV
    import numpy as np
    aac = f'{D}/aac/x.mp4'
    os.makedirs(os.path.dirname(aac))
    subprocess.run([FF, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=s=160x90:r=24:d=20', '-f', 'lavfi', '-i',
                    'aevalsrc=0.3*sin(2*PI*(200+30*t)*t)+0.2*sin(2*PI*1234*t)*sin(3*t):d=20:s=48000', '-c:v', 'mpeg4', '-c:a', 'aac',
                    '-b:a', '192k', aac], check=True)   # tones, not noise: the AAC decoder invents its own noise, so noise never repeats
    wv = f'{D}/aac/y.wav'
    subprocess.run([FF, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'anoisesrc=d=20:r=48000:seed=5', wv], check=True)
    q = Project(); q.add_paths([os.path.dirname(aac)])
    for it in [by['A001_C001.mov']] + q.clips:
        whole_a = np.concatenate([b for _, b in media.iter_audio(it, 0)])
        for st, n in ((0, 5000), (48000 * 2 + 17, 30000), (len(whole_a) - 9000, 9000), (48000 * 4, 48000 * 3)):
            if st + n > len(whole_a):
                continue
            part = np.concatenate([b for _, b in media.iter_audio(it, 0, max_samples=n, start=st)] or [np.zeros(0)])
            assert len(part) == n, (it['name'], st, n, len(part))
            err = float(np.abs(part - whole_a[st:st + n]).max())
            assert err < (1e-6 if 'aac' not in str(it.get('audio_streams')) else 2e-2), (it['name'], st, err)

    # a read that stops answering is given up on, the clip is marked, and later reads of it don't wait again
    real = refpick._read0
    refpick.READ_LIMIT = 1.0
    refpick._read0 = lambda *a: time.sleep(30)
    c1 = by['A001_C001.mov']
    t = time.time()
    x = refpick._read(c1, 0, c1['start'], 4800)
    assert time.time() - t < 5 and not x.any() and 'read_trouble' in c1['flags'] and c1['include']
    t = time.time(); refpick._read(c1, 0, c1['start'], 4800); assert time.time() - t < 0.5
    refpick._read0 = real
    c1['flags'].remove('read_trouble'); c1.pop('read_trouble')
    refpick.READ_LIMIT = 60

    heard = []
    t = time.time()
    refpick.pick_camera_reference(p, log=lambda *a: None, progress=lambda i, n, m: heard.append(m))
    assert time.time() - t < 60
    assert sorted(os.listdir(cam)) == before and os.path.getsize(cut) == full * 2 // 3   # nothing touched
    print('incomplete files OK:', heard)
finally:
    shutil.rmtree(D, ignore_errors=True)
