"""The alignment track: an onboard mic, never the mixer's feed. Builds a copy of the camera day where FX6 ch1 carries
the recorder's mix (a wireless hop) and ch3 carries the onboard mic: ch3 must win."""
import os, shutil, subprocess, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
import numpy as np
from midwigsync.scan import Project
from midwigsync import refpick, convert, media, safety, tc as TC

def unlock():   # building test media, not syncing: the read-only registry doesn't apply
    for x in (safety._files, safety._inodes, safety._roots):
        x.clear()
from midwigsync.bwf import WavInfo

SRC, D = '/home/claude/testday', '/home/claude/hopday'
FF = convert.ffmpeg_exe()
if not os.path.exists(D + '/.made'):
    shutil.rmtree(D, ignore_errors=True)
    shutil.copytree(SRC, D, ignore=shutil.ignore_patterns('*_SYNCED', '_SYNC_WORK', 'truth*.json'))
    p = Project(); p.add_paths([D + '/SOUND'])
    snd = [c for c in p.clips if c['kind'] == 'audio' and c['start'] is not None]
    cams = [os.path.join(dp, f) for dp, _, fs in os.walk(D) for f in fs if f.endswith('.MXF') and 'FX6' in dp]
    for path in cams:
        q = Project(); q.add_paths([path]); c = q.clips[0]
        n = int(c['length'])
        # the "mix": sum of every recorder file over the clip's span, at no delay
        mix = np.zeros(n, np.float32)
        for s in snd:
            a, b = max(c['start'], s['start']), min(c['start'] + n, s['start'] + s['length'])
            if b <= a:
                continue
            w = WavInfo(s['path'])
            x = w.read(int(a - s['start']), int(b - a))
            x = x.mean(1) if x.ndim == 2 else x
            mix[a - c['start']:b - c['start']] += x[:b - a]
        mix /= max(1e-6, np.abs(mix).max()) / 0.5
        onboard = np.concatenate([blk for _, blk in media.iter_audio(c, 0)])[:n]
        unlock()
        tmp = path + '.tmp.mov'
        raw = os.path.join(D, 'mix.f32'); mix.tofile(raw)
        raw2 = os.path.join(D, 'mic.f32'); onboard.astype(np.float32).tofile(raw2)
        subprocess.run([FF, '-y', '-v', 'error', '-i', path, '-f', 'f32le', '-ar', '48000', '-ac', '1', '-i', raw,
                        '-f', 'f32le', '-ar', '48000', '-ac', '1', '-i', raw2,
                        '-filter_complex', '[1:a][2:a][2:a]amerge=inputs=3[a]', '-map', '0:v', '-map', '[a]',
                        '-c:v', 'copy', '-c:a', 'pcm_s24le', '-timecode', c['tc_string'], tmp], check=True)
        os.remove(path); os.rename(tmp, path[:-4] + '.MOV')
        for x in (raw, raw2):
            os.remove(x)
    open(D + '/.made', 'w').write('1')
    unlock()
p = Project(); p.add_paths([D]); p.check_short_clips(20); p.analyze()
r = refpick.pick_camera_reference(p)
print('->', r['roll'], 'ch', r['channel'] + 1, '|', r['why'])
ok = r['roll'] == 'FX6_A001' and r['channel'] != 0
print('REFPICK GOOD' if ok else 'REFPICK FAILED')
