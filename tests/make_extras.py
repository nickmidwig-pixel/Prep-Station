"""Extra formats on top of the simulated day (tests/make_day.py must have been run first).

Writes <OUT> = a folder that links the original day and adds:
  GOPRO/DCIM/100GOPRO/GX010042.MP4 + GX020042.MP4   no TC metadata, LTC (Tentacle) on ch 2, AAC audio
  GOPRO/DCIM/100GOPRO/GX010043.MP4 + GX020043.MP4   TC on chapter 1 only (chapter 2 must follow it)
  BRAW_C/A002_C001.braw, A002_C002.braw              RAW-style MOV (unknown picture codec) + tmcd; C002 off by 3 s
  P2CARD/CONTENTS/VIDEO/0001AB.MXF + AUDIO/0001AB00..01.MXF   OP-Atom, audio in separate files
  SOUND/07MIX96/MIX_0001.WAV                          96 kHz 32-bit float BWF with TC
  MEMO/Voice 001.m4a                                   44.1 kHz AAC, no TC
Truth for each goes into <OUT>/truth_extras.json.
"""
import json
import os
import subprocess
import sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app'))
from midwigsync import bwf, ltc, tc as TC  # noqa: E402
from midwigsync.stamp import ffmpeg_exe  # noqa: E402

DAY = sys.argv[1] if len(sys.argv) > 1 else '/home/claude/testday'
OUT = sys.argv[2] if len(sys.argv) > 2 else '/home/claude/testday_x'
FF = ffmpeg_exe()

# rebuild the same world audio as make_day (same seeds), without writing its files
src = open(os.path.join(HERE, 'make_day.py')).read()
src = src[:src.index('truth = {}')]
g = {'__name__': 'world', '__file__': os.path.join(HERE, 'make_day.py')}
sys.argv = [sys.argv[0], DAY]
exec(compile(src, 'make_day_world', 'exec'), g)
grab, SR, FPS, T0 = g['grab'], g['SR'], g['FPS'], g['T0']

os.makedirs(OUT, exist_ok=True)
for n in os.listdir(DAY):
    p = os.path.join(DAY, n)
    if os.path.isdir(p) and not n.endswith(('_MIDWIG', '_SYNCED')) and n != 'SOUND':
        if not os.path.exists(os.path.join(OUT, n)):
            os.symlink(p, os.path.join(OUT, n))
os.makedirs(os.path.join(OUT, 'SOUND'), exist_ok=True)
for n in os.listdir(os.path.join(DAY, 'SOUND')):
    p = os.path.join(DAY, 'SOUND', n)
    if os.path.isdir(p) and not n.endswith(('_MIDWIG', '_SYNCED')) and not os.path.exists(os.path.join(OUT, 'SOUND', n)):
        os.symlink(p, os.path.join(OUT, 'SOUND', n))
truth = {}


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-800:])


def write_raw_f32(path, x):
    np.asarray(x, '<f4').tofile(path)


def at(tc_s):
    return T0 + int(tc_s * SR)


# ---------------------------------------------------------------- GoPro chapters with LTC on ch 2
gp = os.path.join(OUT, 'GOPRO', 'DCIM', '100GOPRO')
os.makedirs(gp, exist_ok=True)
start = at(40.0) + 517          # not on a frame edge
dur = 45.0
for ch_i in (1, 2):
    s = start + int((ch_i - 1) * dur * SR)
    scratch = grab('phone', s, dur) * 0.8
    f0 = TC.samples_to_frames(s, FPS, 'ceil')
    lead = (TC.frames_to_samples(f0, FPS) - s) / SR
    code = ltc.encode(f0, int(dur * 24) + 2, FPS, SR, lead_s=lead)[:len(scratch)] * 0.6
    code = np.pad(code, (0, len(scratch) - len(code)))
    st = np.stack([scratch, code], 1).astype('<f4')
    raw = '/tmp/gp.f32'
    st.tofile(raw)
    name = f'GX0{ch_i}0042.MP4'
    run([FF, '-y', '-v', 'error', '-f', 'lavfi', '-i', f'color=c=gray:s=64x36:r=24000/1001:d={dur}',
         '-f', 'f32le', '-ar', str(SR), '-ac', '2', '-i', raw, '-c:v', 'libx264', '-preset', 'ultrafast',
         '-c:a', 'aac', '-b:a', '192k', '-shortest', '-metadata', 'model=HERO12 Black', os.path.join(gp, name)])
    truth[name] = dict(true_start=s)
# second take: TC metadata on chapter 1 only, no LTC
start2 = at(150.0)
for ch_i in (1, 2):
    s = start2 + int((ch_i - 1) * 30 * SR)
    x = grab('phone', s, 30.0) * 0.8
    x.astype('<f4').tofile('/tmp/gp2.f32')
    name = f'GX0{ch_i}0043.MP4'
    extra = ['-timecode', TC.samples_to_tc(s, FPS)] if ch_i == 1 else []
    run([FF, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=gray:s=64x36:r=24000/1001:d=30',
         '-f', 'f32le', '-ar', str(SR), '-ac', '1', '-i', '/tmp/gp2.f32', '-c:v', 'libx264', '-preset', 'ultrafast',
         '-c:a', 'pcm_s16le', '-shortest'] + extra + [os.path.join(gp, name.replace('.MP4', '.mov'))])
    os.replace(os.path.join(gp, name.replace('.MP4', '.mov')), os.path.join(gp, name))
    truth[name] = dict(true_start=s)

# ---------------------------------------------------------------- Blackmagic-RAW-like files
bc = os.path.join(OUT, 'BRAW_C')
os.makedirs(bc, exist_ok=True)
for k, (t_s, err_s) in enumerate([(60.0, 0.0), (250.0, 3.0)]):
    s = at(t_s)
    n_fr = 24 * 40
    s = TC.frames_to_samples(TC.samples_to_frames(s, FPS), FPS)
    x = grab('cam', s, n_fr * 1001 / 24000.0)
    np.stack([x, x * 0.7], 1).astype('<f4').tofile('/tmp/br.f32')
    tmp = '/tmp/br.mov'
    claimed = s + int(err_s * SR)
    run([FF, '-y', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc=s=64x36:r=24000/1001', '-f', 'f32le', '-ar', str(SR),
         '-ac', '2', '-i', '/tmp/br.f32', '-frames:v', str(n_fr), '-c:v', 'rawvideo', '-pix_fmt', 'uyvy422',
         '-c:a', 'pcm_s24le', '-timecode', TC.samples_to_tc(claimed, FPS), '-shortest', tmp])
    b = bytearray(open(tmp, 'rb').read())
    i = b.find(b'stsd')
    j = b.find(b'2vuy', i)
    b[j:j + 4] = b'brvo'
    name = f'A002_C00{k + 1}.braw'
    open(os.path.join(bc, name), 'wb').write(b)
    truth[name] = dict(true_start=s, claimed=claimed)

# ---------------------------------------------------------------- Panasonic P2 (OP-Atom)
p2 = os.path.join(OUT, 'P2CARD', 'CONTENTS')
for d in ('VIDEO', 'AUDIO', 'CLIP'):
    os.makedirs(os.path.join(p2, d), exist_ok=True)
s = TC.frames_to_samples(TC.samples_to_frames(at(330.0), FPS), FPS)
x = grab('cam', s, 12.0)
x.astype('<f4').tofile('/tmp/p2.f32')
tcs = TC.samples_to_tc(s, FPS)
run([FF, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=s=1920x1080:r=24000/1001', '-t', '12', '-c:v', 'dnxhd',
     '-b:v', '36M', '-pix_fmt', 'yuv422p', '-timecode', tcs, '-f', 'mxf_opatom', os.path.join(p2, 'VIDEO', '0001AB.MXF')])
for ch in (0, 1):
    run([FF, '-y', '-v', 'error', '-f', 'f32le', '-ar', str(SR), '-ac', '1', '-i', '/tmp/p2.f32', '-c:a', 'pcm_s24le',
         '-timecode', tcs, '-f', 'mxf_opatom', os.path.join(p2, 'AUDIO', f'0001AB0{ch}.MXF')])
truth['0001AB.MXF'] = dict(true_start=s)

# ---------------------------------------------------------------- 96 kHz 32-bit float recorder with TC
mix = os.path.join(OUT, 'SOUND', '07MIX96')
os.makedirs(mix, exist_ok=True)
s = at(20.0)
x48 = grab('boom06', s, 120.0, warp=True)
x96 = np.interp(np.arange(len(x48) * 2) / 2.0, np.arange(len(x48)), x48).astype(np.float32)
tr96 = int(round(s * 2))
desc = f'sTRK1=MIX\r\ndTC={TC.samples_to_tc(s, FPS)}\r\ndFRAMERATE=23.976ND\r\n'
be = bwf.build_bext(desc, 'Sound Devices', 'MIXPRE', '2026-11-03', '09:17:16', tr96, 'A=FLOAT,F=96000,W=32\r\n')
ix = bwf.build_ixml('TEST', '23.976', '24000/1001', False, tr96, 96000, 32, ['MIX'])
w = bwf.WavWriter(os.path.join(mix, 'MIX_0001.WAV'), len(x96), 1, 96000, 32, bwf.FMT_FLOAT, be, ix)
w.write_float(x96)
w.close()
truth['MIX_0001.WAV'] = dict(true_start=s, warp=True)

# ---------------------------------------------------------------- 44.1 kHz voice memo, no TC
memo = os.path.join(OUT, 'MEMO')
os.makedirs(memo, exist_ok=True)
s = at(200.0) + 1234
x = grab('phone', s, 60.0)
x.astype('<f4').tofile('/tmp/memo.f32')
run([FF, '-y', '-v', 'error', '-f', 'f32le', '-ar', str(SR), '-ac', '1', '-i', '/tmp/memo.f32', '-ar', '44100',
     '-c:a', 'aac', '-b:a', '128k', os.path.join(memo, 'Voice 001.m4a')])
truth['Voice 001.m4a'] = dict(true_start=s)

json.dump(truth, open(os.path.join(OUT, 'truth_extras.json'), 'w'), indent=1)
print('extras written to', OUT)
