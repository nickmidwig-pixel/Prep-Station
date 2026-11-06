"""A sound mixer's delivery (Sound Devices style): one polywav per take, 6 tracks
   1 Mix-L, 2 Mix-R (live mix of the unaligned mics), 3 Boom, 4 Jane (lav), 5 Bob (lav), 6 Spare (empty)
with bext TC + user bits + iXML track names. Take T004 has the boom switched off (lav-only scene).
Mics sit at different distances: lavs ~0.5 ms from their talker, boom ~3 ms, plus wireless latency on the lavs.
Writes <OUT>/SOUND_ROLL_01/T00x.WAV and truth_mixer.json (each track's delay vs the talkers)."""
import json
import os
import sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app'))
from midwigsync import bwf, tc as TC  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else '/home/claude/mixerday'
SR = 48000
FPS = TC.parse_rate('23.976')
rng = np.random.default_rng(21)

src = open(os.path.join(HERE, 'make_day.py')).read()
g = {'__name__': 'world', '__file__': os.path.join(HERE, 'make_day.py')}
exec(compile(src[:src.index('world = {')], 'make_day_talker', 'exec'), g)
talker, delay, reverb = g['talker'], g['delay'], g['reverb']

N = 520 * SR
s1, s2 = talker(130, 31, N), talker(210, 32, N)
room = 0.002 * rng.standard_normal(N).astype(np.float32)
LAT = 0.0021                     # digital wireless latency on the lavs (s)
mics = {
    'Boom': reverb(delay(s1 + s2, 0.0030 * SR), 41, mix=0.12) + room,
    'Jane': delay(s1 + 0.15 * delay(s2, 0.0035 * SR), LAT * SR) + room,
    'Bob': delay(s2 + 0.15 * delay(s1, 0.0035 * SR), LAT * SR) + room,
}
T0 = TC.tc_to_samples('14:02:10:00', FPS)
takes = [(0, 70), (85, 60), (160, 95), (270, 80), (365, 60), (440, 70)]   # (start s, length s)
os.makedirs(os.path.join(OUT, 'SOUND_ROLL_01'), exist_ok=True)
truth = dict(delays_ms=dict(Boom=3.0, Jane=LAT * 1000, Bob=LAT * 1000), takes={})
names = ['Mix-L', 'Mix-R', 'Boom', 'Jane', 'Bob', 'Spare']
for k, (a_s, len_s) in enumerate(takes):
    a, n = int(a_s * SR), int(len_s * SR)
    boom = mics['Boom'][a:a + n] * (0.0 if k == 3 else 1.0)
    jane, bob = mics['Jane'][a:a + n], mics['Bob'][a:a + n]
    mix = 0.5 * boom + 0.5 * jane + 0.5 * bob
    x = np.stack([mix, mix, boom, jane, bob, np.zeros(n, np.float32)], 1).astype(np.float32)
    start = T0 + a
    tcs = TC.samples_to_tc(start, FPS)
    name = f'T{k + 1:03d}.WAV'
    desc = ''.join(f'sTRK{i + 1}={nm}\r\n' for i, nm in enumerate(names)) + \
        f'dTC={tcs}\r\ndUBITS=$20261104\r\ndFRAMERATE=23.976ND\r\ndSCENE=12\r\ndTAKE={k + 1}\r\n'
    be = bwf.build_bext(desc, 'SOUND DEVICES', '888', '2026-11-04', tcs[:8], start, 'A=PCM,F=48000,W=24,M=6\r\n')
    ix = bwf.build_ixml('MIXERDAY', '23.976', '24000/1001', False, start, SR, 24, names, scene='12', take=str(k + 1))
    w = bwf.WavWriter(os.path.join(OUT, 'SOUND_ROLL_01', name), n, 6, SR, 24, bwf.FMT_PCM, be, ix)
    w.write_float(x)
    w.close()
    truth['takes'][name] = dict(start=start, boom_off=(k == 3))
json.dump(truth, open(os.path.join(OUT, 'truth_mixer.json'), 'w'), indent=1)
print('mixer day written to', OUT)
