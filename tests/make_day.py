"""Simulate a shoot day: FX6 MXF cards with TC, a B-cam (one clip with a missed jam), an iPhone clip with
no TC, Zaxcom-style ISOs with a drifting clock offset vs camera (one file with a missed jam), a short
accidental lav clip and a Zoom WAV with no TC.  Writes truth.json with every file's real start."""
import json
import os
import struct
import subprocess
import sys
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync import bwf, tc as TC  # noqa: E402

SR = 48000
OUT = sys.argv[1] if len(sys.argv) > 1 else '/home/claude/testday'
FPS = TC.parse_rate('23.976')
T0 = TC.tc_to_samples('09:16:56:00', FPS)
SPAN = 560  # seconds of world audio
rng = np.random.default_rng(7)


def talker(f0, seed, n):
    r = np.random.default_rng(seed)
    env = np.zeros(n, np.float32)
    t = int(r.uniform(0.2, 1.0) * SR)
    while t < n:
        if r.random() < 0.5:     # this talker's turn
            end = t + int(r.uniform(1.0, 4.0) * SR)
            while t < min(end, n):
                L = int(r.uniform(0.08, 0.25) * SR)
                seg = np.hanning(L).astype(np.float32) * r.uniform(0.3, 1.0)
                env[t:t + L] = seg[:max(0, min(L, n - t))]
                t += L + int(r.uniform(0.03, 0.15) * SR)
        t += int(r.uniform(0.3, 2.5) * SR)
    # pitch contour
    k = n // 4800 + 2
    f = f0 * (1 + 0.12 * np.interp(np.arange(n), np.arange(k) * 4800, r.standard_normal(k)))
    ph = np.cumsum(2 * np.pi * f / SR)
    x = np.zeros(n, np.float32)
    for h in range(1, 14):
        x += (np.sin(h * ph) / h).astype(np.float32)
    x += 0.25 * r.standard_normal(n).astype(np.float32)
    return 0.25 * x * env


def delay(x, d_samples):
    d = int(round(d_samples))
    if d <= 0:
        return x
    return np.concatenate([np.zeros(d, np.float32), x[:-d]])


def reverb(x, seed, t60=0.35, mix=0.35):
    r = np.random.default_rng(seed)
    L = int(t60 * SR)
    ir = r.standard_normal(L).astype(np.float32) * np.exp(-6.9 * np.arange(L) / L).astype(np.float32)
    ir /= np.sqrt((ir ** 2).sum())
    n = 1 << int(np.ceil(np.log2(len(x) + L)))
    y = np.fft.irfft(np.fft.rfft(x, n) * np.fft.rfft(ir, n), n)[:len(x)].astype(np.float32)
    return x + mix * y


print('synthesizing world audio...')
N = (SPAN + 20) * SR
s1 = talker(125, 1, N)
s2 = talker(205, 2, N)
room = 0.002 * rng.standard_normal(N).astype(np.float32)
world = {
    'cam': reverb(delay(s1 + s2, 0.012 * SR), 11) * 0.6 + room * 3,
    'lav02': s1 + 0.12 * delay(s2, 0.004 * SR) + room,
    'lav03': s2 + 0.12 * delay(s1, 0.004 * SR) + room,
    'lav04': 0.8 * s1 + 0.1 * s2 + room,
    'boom06': reverb(delay(s1 + s2, 0.003 * SR), 12, mix=0.15) + room,
    'zoom': reverb(delay(s1 + s2, 0.02 * SR), 13, mix=0.5) * 0.5 + room * 4,
    'phone': reverb(delay(s1 + s2, 0.015 * SR), 14, mix=0.45) * 0.5 + room * 5,
}
del s1, s2


def zax_offset(t_abs):
    """Zaxcom clock vs camera, seconds (drifts 35-55 ms through the day)."""
    return 0.045 + 0.010 * np.sin(2 * np.pi * (t_abs - T0) / SR / 300.0)


def grab(name, start_abs, dur_s, warp=False):
    """Signal a device would record from TC position start_abs (samples) for dur_s."""
    n = int(dur_s * SR)
    tt = start_abs + np.arange(n)
    if warp:
        tt = tt + zax_offset(tt) * SR
    idx = tt - T0
    i0 = np.floor(idx).astype(np.int64)
    fr = (idx - i0).astype(np.float32)
    src = world[name]
    i0 = np.clip(i0, 0, len(src) - 2)
    return src[i0] * (1 - fr) + src[i0 + 1] * fr


truth = {}
os.makedirs(OUT, exist_ok=True)


def wav_bwf(path, x, start_abs, track, ubits='20261103', bext=True, channels=1):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tcs = TC.samples_to_tc(start_abs, FPS)
    if bext:
        desc = f'sTRK1={track}\r\ndTC={tcs}\r\ndUBITS=${ubits}\r\ndFRAMERATE=23.976ND\r\nsSPEED=023.976-ND\r\nzPROJECT=TEST\r\n'
        be = bwf.build_bext(desc, 'ZAXCOM', 'ZAX12345', '2026-11-03', tcs[:8].replace(':', ':'), start_abs,
                            b'A=PCM,F=48000,W=24,M=mono,T=ZAXCOM;\r\n')
        ix = bwf.build_ixml('TEST', '23.976', '24000/1001', False, start_abs, tracks=[track])
    else:
        be = ix = None
    w = bwf.WavWriter(path, len(x) if x.ndim == 1 else x.shape[0], channels, 48000, 24, bext=be, ixml=ix)
    w.write_float(x)
    w.close()


def iso(folder, idx, rel_s, dur_s, wname, jam_error_s=0.0, silent=False):
    start_true = T0 + int(round(rel_s * 23.976)) * 2002   # frame aligned
    x = grab(wname, start_true, dur_s, warp=True)
    if silent:
        x = 0.0005 * rng.standard_normal(len(x)).astype(np.float32)
    claimed = start_true + int(round(jam_error_s * 23.976)) * 2002
    hh = TC.samples_to_tc(claimed, FPS).replace(':', '')[:6]
    path = f'{OUT}/SOUND/{folder}/20261103_{hh}_{folder}_{idx:03}.WAV'
    wav_bwf(path, x, claimed, folder)
    truth[os.path.basename(path)] = dict(true_start=start_true, claimed=claimed, warp=True)


print('writing ISOs...')
iso('02LAV', 1, 0, 250, 'lav02')
iso('02LAV', 2, 255, 265, 'lav02')
iso('03LAV', 1, 5, 240, 'lav03')
iso('03LAV', 2, 252, 263, 'lav03', jam_error_s=93.0)
iso('04LAV', 1, 10, 3, 'lav04', silent=True)
iso('04LAV', 2, 15, 485, 'lav04')
iso('06BOOM', 1, 0, 530, 'boom06')

# Zoom: no bext at all
zs = T0 + int(60.37 * SR)
zx = grab('zoom', zs, 240)
zpath = f'{OUT}/SOUND/ZOOM_F3/ZOOM0001.WAV'
os.makedirs(os.path.dirname(zpath), exist_ok=True)
w = bwf.WavWriter(zpath, len(zx), 2, 48000, 24)
w.write_float(np.stack([zx, zx * 0.9], 1))
w.close()
truth['ZOOM0001.WAV'] = dict(true_start=zs, claimed=None)


def write_tmp_wav(path, x):
    w = bwf.WavWriter(path, len(x), 1, 48000, 24)
    w.write_float(x)
    w.close()


def cam_clip(kind, path, rel_s, dur_s, tc_err_s=0.0, notc=False):
    start_true = T0 + int(round(rel_s * 23.976)) * 2002
    claimed = start_true + int(round(tc_err_s * 23.976)) * 2002
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = '/tmp/mk'
    os.makedirs(tmp, exist_ok=True)
    cam = grab('cam' if kind != 'phone' else 'phone', start_true, dur_s)
    tcs = TC.samples_to_tc(claimed, FPS)
    v = ['-f', 'lavfi', '-i', f'testsrc2=s=320x180:r=24000/1001:d={dur_s}']
    if kind == 'mxf':
        boom = grab('boom06', start_true, dur_s) * 0.5
        chans = [cam, boom] + [np.zeros(len(cam), np.float32)] * 6
        ins = []
        for i, ch in enumerate(chans):
            write_tmp_wav(f'{tmp}/c{i}.wav', ch)
            ins += ['-i', f'{tmp}/c{i}.wav']
        maps = ['-map', '0:v'] + sum([['-map', f'{i + 1}:a'] for i in range(8)], [])
        cmd = ['ffmpeg', '-y', '-v', 'error'] + v + ins + maps + ['-c:v', 'mpeg2video', '-b:v', '2M', '-c:a', 'pcm_s24le',
                                                                  '-timecode', tcs, '-shortest', path]
    else:
        write_tmp_wav(f'{tmp}/c0.wav', cam)
        cmd = ['ffmpeg', '-y', '-v', 'error'] + v + ['-i', f'{tmp}/c0.wav', '-map', '0:v', '-map', '1:a', '-ac', '2',
                                                      '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p']
        if kind == 'phone':
            cmd += ['-c:a', 'aac', '-b:a', '192k', '-metadata', 'creation_time=2026-11-03T14:18:40Z']
        else:
            cmd += ['-c:a', 'pcm_s16le']
        if not notc:
            cmd += ['-timecode', tcs]
        cmd += ['-shortest', path]
    subprocess.run(cmd, check=True)
    truth[os.path.basename(path)] = dict(true_start=start_true, claimed=None if notc else claimed)


print('writing camera clips...')
cam_clip('mxf', f'{OUT}/FX6_A001/XDROOT/Clip/A001C001_261103AB.MXF', 20, 180)
cam_clip('mxf', f'{OUT}/FX6_A001/XDROOT/Clip/A001C002_261103AB.MXF', 260, 210)
os.makedirs(f'{OUT}/FX6_A001/XDROOT/Sub', exist_ok=True)
open(f'{OUT}/FX6_A001/XDROOT/Sub/A001C001S03.MP4', 'wb').write(b'proxy-should-be-skipped')
cam_clip('mov', f'{OUT}/C80_B001/CONTENTS/CLIPS001/B001C001.MOV', 40, 120)
cam_clip('mov', f'{OUT}/C80_B001/CONTENTS/CLIPS001/B001C002.MOV', 300, 130, tc_err_s=7.458)
cam_clip('phone', f'{OUT}/IPHONE/IMG_4471.MP4', 100.3, 90, notc=True)
json.dump(truth, open(f'{OUT}/truth.json', 'w'), indent=1)
print('done', OUT)
