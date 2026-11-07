"""Auto-Align Post 2 (Resolve 21) names renders <src>-AtAlgn-<hash>.wav with no offset. Prep Station must place
each one from the Resolve timeline plan: right piece, with or without 1 s handles, a cut-short render settled by
the waveform, two pieces of the same length told apart by the waveform. Also: the render-name pattern still reads
the older <src>_si0ch0off<N>-AtAlgn names."""
import os
import shutil
import sys
import tempfile
from fractions import Fraction

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync import tc as TC  # noqa: E402
from midwigsync.assemble import Assembler, plan_hints  # noqa: E402
from midwigsync.bwf import WavWriter  # noqa: E402
from midwigsync.resolve_aap import RENDER_RE  # noqa: E402

SR = 48000
FPS = TC.parse_rate('23.976')
SPF = TC.samples_per_frame(FPS)


def wav(path, x):
    w = WavWriter(path, len(x), 1, SR, 24)
    w.write_float(x.astype(np.float32))
    w.close()


def main():
    m = RENDER_RE.match('GeorgiaT02__01_BOOM-AtAlgn-bdb73.wav')
    assert m and m['src'] == 'GeorgiaT02__01_BOOM' and m['off'] is None
    m = RENDER_RE.match('X__01_LAV_si0ch0off-1200-AtAlgn-beef.wav')
    assert m and m['src'] == 'X__01_LAV' and m['off'] == '-1200'
    m = RENDER_RE.match('ZOOM0001__02_ZOOM_F3_2-AtAlgn-000bbeef.wav')
    assert m['src'] == 'ZOOM0001__02_ZOOM_F3_2'

    d = tempfile.mkdtemp()
    try:
        rng = np.random.default_rng(7)
        n_src = 120 * SR
        # speech-like: noise bursts with gaps
        env = np.repeat(rng.random(n_src // 4800) > 0.4, 4800).astype(np.float32)
        src = (rng.standard_normal(n_src) * 0.2 * env).astype(np.float32)
        sp = os.path.join(d, 'TAKE__01_BOOM.WAV')
        wav(sp, src)
        start = TC.tc_to_samples('11:00:00:00', FPS)
        # pieces (frames from file start): 10-40 s, 50-80 s (same length as the first), 100-119.9 s, 85-95 s
        def fr(sec):
            return int(sec * SR / SPF)
        pieces = [(fr(10), fr(40)), (fr(50), fr(80)), (fr(100), fr(119.9)), (fr(85), fr(95))]
        plan = dict(fps='23.976', items=[dict(kind='source', name='TAKE__01_BOOM.WAV', path=sp,
                                              src_in=Fraction(a), src_out=Fraction(b)) for a, b in pieces])
        hints = plan_hints(plan)
        truth = {}
        files = []
        for k, (a, b) in enumerate(pieces):
            A, B = int(round(a * SPF)), int(round(b * SPF))
            if k == 0:
                A2, B2 = A - SR, B + SR             # 1 s handles both sides
            elif k == 1:
                A2, B2 = A - SR, B - 7 * SR          # cut short: no length matches, waveform decides
            elif k == 2:
                A2, B2 = A - SR, min(n_src, B + SR)  # handle only as far as the file goes
            else:
                A2, B2 = A - SR // 2, B + SR // 2    # Handles set to 0.5 s
            y = src[A2:B2] * (0.9 if k != 1 else -0.8)        # gain, and a polarity flip on one
            y = np.concatenate([np.zeros(12), y[:-12]])      # Auto-Align moved it 12 samples: must be KEPT
            p = os.path.join(d, f'TAKE__01_BOOM-AtAlgn-{k:04x}cafe.wav')
            wav(p, y)
            truth[p] = A2
            files.append(p)
        logs = []
        asm = Assembler([dict(path=sp, name='TAKE__01_BOOM.WAV', start=start, roll='BOOM')], files,
                        os.path.join(d, 'out'), log=logs.append, hints=hints)
        assert len(asm.rends) == 4, (len(asm.rends), logs)
        for r in asm.rends:
            got = r['start'] - start
            print(f"  {os.path.basename(r['path'])}: offset {got} (truth {truth[r['path']]}) via {r['placed_by']}")
            assert got == truth[r['path']], (got, truth[r['path']], r['placed_by'])
        # a render that fits nowhere is left out, not guessed
        junk = os.path.join(d, 'TAKE__01_BOOM-AtAlgn-dead.wav')
        wav(junk, rng.standard_normal(15 * SR).astype(np.float32) * 0.1)
        logs = []
        asm = Assembler([dict(path=sp, name='TAKE__01_BOOM.WAV', start=start, roll='BOOM')], [junk],
                        os.path.join(d, 'out'), log=logs.append, hints=hints)
        assert not asm.rends and any('left out' in x for x in logs), logs
        print('  unrelated render: left out')
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print('AAP2 NAMES OK')


if __name__ == '__main__':
    main()
