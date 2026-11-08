"""Shoot days for the 'fix the fewest sources' rule. Three sources share one world of sound: A CAM (two cards,
the reference), B CAM (one card) and a 633-style recorder (two polywav takes), plus an optional GoPro.

  python make_consensus_day.py <out> <scenario>
    audio_off  A + B agree, the recorder's timecode is +2.4 s off        -> correct the recorder
    gopro      A + B + recorder agree, the GoPro is -3.1 s off            -> correct the GoPro
    ref_off    B + recorder agree, A CAM (the reference) is +5.0 s off    -> correct A CAM, not the two that agree
    all_good   everything agrees                                          -> nothing corrected

Writes truth.json with every file's real start and the expected decision."""
import json
import os
import subprocess
import sys
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync import bwf, tc as TC  # noqa: E402

SR = 48000
OUT = sys.argv[1]
SCENARIO = sys.argv[2]
FPS = TC.parse_rate('23.976')
T0 = TC.tc_to_samples('10:00:00:00', FPS)
SPAN = 330
rng = np.random.default_rng(5)
ERR = dict(audio_off=dict(snd=2.4), gopro=dict(gopro=-3.1), ref_off=dict(a=5.0), all_good={})[SCENARIO]
EXPECT = dict(audio_off=['633'], gopro=['GOPRO'], ref_off=['A001', 'A002'], all_good=[])[SCENARIO]


def talker(f0, seed, n):
    r = np.random.default_rng(seed)
    env = np.zeros(n, np.float32)
    t = int(r.uniform(0.2, 1.0) * SR)
    while t < n:
        if r.random() < 0.55:
            end = t + int(r.uniform(1.0, 4.0) * SR)
            while t < min(end, n):
                L = int(r.uniform(0.08, 0.25) * SR)
                seg = np.hanning(L).astype(np.float32) * r.uniform(0.3, 1.0)
                env[t:t + L] = seg[:max(0, min(L, n - t))]
                t += L + int(r.uniform(0.03, 0.15) * SR)
        t += int(r.uniform(0.3, 2.0) * SR)
    k = n // 4800 + 2
    f = f0 * (1 + 0.12 * np.interp(np.arange(n), np.arange(k) * 4800, r.standard_normal(k)))
    ph = np.cumsum(2 * np.pi * f / SR)
    x = np.zeros(n, np.float32)
    for h in range(1, 12):
        x += (np.sin(h * ph) / h).astype(np.float32)
    x += 0.25 * r.standard_normal(n).astype(np.float32)
    return 0.25 * x * env


def delay(x, d):
    d = int(round(d))
    return x if d <= 0 else np.concatenate([np.zeros(d, np.float32), x[:-d]])


N = (SPAN + 20) * SR
s1, s2 = talker(130, 1, N), talker(210, 2, N)
room = 0.002 * rng.standard_normal(N).astype(np.float32)
world = {
    'cam': delay(s1 + s2, 0.010 * SR) * 0.6 + room * 3,
    'gopro': delay(s1 + s2, 0.018 * SR) * 0.4 + room * 5,
    'boom': delay(s1 + s2, 0.003 * SR) + room,
    'lav': s1 + 0.1 * s2 + room,
}
del s1, s2


def grab(name, start_abs, dur_s):
    i0 = start_abs - T0
    x = world[name]
    i0 = int(np.clip(i0, 0, len(x) - 1))
    seg = x[i0:i0 + int(dur_s * SR)]
    if len(seg) < int(dur_s * SR):
        seg = np.concatenate([seg, np.zeros(int(dur_s * SR) - len(seg), np.float32)])
    return seg


truth = {}
os.makedirs(OUT, exist_ok=True)


def frames(s):
    return int(round(s * 23.976)) * 2002


def poly(idx, rel_s, dur_s, err_s):
    start_true = T0 + frames(rel_s)
    claimed = start_true + frames(err_s)
    x = np.stack([grab('boom', start_true, dur_s), grab('lav', start_true, dur_s)], 1)
    tcs = TC.samples_to_tc(claimed, FPS)
    desc = f'sTRK1=Boom\r\nsTRK2=Lav Nick\r\ndTC={tcs}\r\ndFRAMERATE=23.976ND\r\nsSPEED=023.976-ND\r\nzPROJECT=CONS\r\n'
    be = bwf.build_bext(desc, 'SOUND DEVICES 633', 'SD633', '2026-11-07', tcs[:8], claimed, b'A=PCM,F=48000,W=24,M=stereo,T=SD633;\r\n')
    ix = bwf.build_ixml('CONS', '23.976', '24000/1001', False, claimed, tracks=['Boom', 'Lav Nick'])
    path = f'{OUT}/SOUND/633/CONS_T{idx:02}.WAV'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    w = bwf.WavWriter(path, len(x), 2, 48000, 24, bext=be, ixml=ix)
    w.write_float(x)
    w.close()
    truth[os.path.basename(path)] = dict(true_start=start_true, claimed=claimed)


def write_tmp_wav(path, x):
    w = bwf.WavWriter(path, len(x), 1, 48000, 24)
    w.write_float(x)
    w.close()


def cam_clip(kind, path, rel_s, dur_s, err_s=0.0):
    start_true = T0 + frames(rel_s)
    claimed = start_true + frames(err_s)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = '/tmp/mk_cons'
    os.makedirs(tmp, exist_ok=True)
    src = 'gopro' if kind == 'gopro' else 'cam'
    cam = grab(src, start_true, dur_s)
    tcs = TC.samples_to_tc(claimed, FPS)
    v = ['-f', 'lavfi', '-i', f'testsrc2=s=320x180:r=24000/1001:d={dur_s}']
    if kind == 'mxf':
        chans = [cam, grab('boom', start_true, dur_s) * 0.5] + [np.zeros(len(cam), np.float32)] * 2
        ins = []
        for i, ch in enumerate(chans):
            write_tmp_wav(f'{tmp}/c{i}.wav', ch)
            ins += ['-i', f'{tmp}/c{i}.wav']
        maps = ['-map', '0:v'] + sum([['-map', f'{i + 1}:a'] for i in range(4)], [])
        cmd = ['ffmpeg', '-y', '-v', 'error'] + v + ins + maps + ['-c:v', 'mpeg2video', '-b:v', '2M', '-c:a', 'pcm_s24le',
                                                                  '-timecode', tcs, '-shortest', path]
    else:
        write_tmp_wav(f'{tmp}/c0.wav', cam)
        cmd = ['ffmpeg', '-y', '-v', 'error'] + v + ['-i', f'{tmp}/c0.wav', '-map', '0:v', '-map', '1:a', '-ac', '2',
                                                      '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                                                      '-c:a', 'pcm_s16le', '-timecode', tcs, '-shortest', path]
    subprocess.run(cmd, check=True)
    truth[os.path.basename(path)] = dict(true_start=start_true, claimed=claimed)


print('writing', SCENARIO)
poly(1, 0, 150, ERR.get('snd', 0.0))
poly(2, 160, 160, ERR.get('snd', 0.0))
cam_clip('mxf', f'{OUT}/FX6_A001/XDROOT/Clip/A001C001_261107AB.MXF', 5, 140, ERR.get('a', 0.0))
cam_clip('mxf', f'{OUT}/FX6_A002/XDROOT/Clip/A002C001_261107AB.MXF', 165, 150, ERR.get('a', 0.0))
cam_clip('mov', f'{OUT}/C80_B001/CONTENTS/CLIPS001/B001C001.MOV', 10, 130, ERR.get('b', 0.0))
cam_clip('mov', f'{OUT}/C80_B001/CONTENTS/CLIPS001/B001C002.MOV', 170, 140, ERR.get('b', 0.0))
if SCENARIO == 'gopro':
    cam_clip('gopro', f'{OUT}/GOPRO/GX010042.MP4', 20, 280, ERR['gopro'])
truth['_expect'] = dict(scenario=SCENARIO, corrected_rolls=EXPECT)
json.dump(truth, open(f'{OUT}/truth.json', 'w'), indent=1)
print('done', OUT)
