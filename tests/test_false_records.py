"""False records: a Zaxcom set to record on boot rolls for a few seconds before its timecode bites.
Such a file (short, no timecode or 00:00:00:00) is left out on its own, never deleted or renamed;
a longer file without timecode is not; and a false record you tick to use stays used after a rescan."""
import os, shutil, sys, tempfile
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync.scan import Project
from midwigsync import bwf, tc as TC

FPS = TC.parse_rate('23.976')
rng = np.random.default_rng(3)


def wav(path, secs, tc=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    be = None
    if tc is not None:
        start = TC.tc_to_samples(tc, FPS) if tc else 0
        desc = (f'sTRK1=LAV\r\ndTC={tc}\r\ndFRAMERATE=23.976ND\r\n' if tc else 'sTRK1=LAV\r\n')
        be = bwf.build_bext(desc, 'ZAXCOM', 'ZAX1', '2026-11-11', (tc or '00:00:00')[:8], start,
                            b'A=PCM,F=48000,W=24,M=1,T=ZAXFILE\r\n')
    w = bwf.WavWriter(path, 48000 * secs, 1, 48000, 24, bext=be, ixml=None)
    w.write_float(0.1 * rng.standard_normal(48000 * secs).astype(np.float32))   # not silent: a real false record has sound
    w.close()


D = tempfile.mkdtemp(prefix='falserec_')
try:
    day = os.path.join(D, '20261111', 'SOUND', '01LAV')
    wav(f'{day}/20261111_090000_01LAV_001.WAV', 60, '09:00:00:00')    # a real take
    wav(f'{day}/20261111_085958_01LAV_000.WAV', 4, '')               # rolled on boot, timecode not bitten: TC 0
    wav(f'{day}/NOTC_SHORT.WAV', 6)                                  # no bext at all, 6 s
    wav(f'{day}/NOTC_LONG.WAV', 45)                                  # no timecode but 45 s: a real recording, kept
    names = sorted(os.listdir(day))
    p = Project()
    p.add_paths([os.path.join(D, '20261111')])
    p.check_short_clips(20)
    by = {c['name']: c for c in p.clips}
    fr = lambda n: 'false_record' in by[n]['flags'] and not by[n]['include']
    assert fr('20261111_085958_01LAV_000.WAV'), by['20261111_085958_01LAV_000.WAV']['flags']
    assert fr('NOTC_SHORT.WAV'), by['NOTC_SHORT.WAV']['flags']
    assert not fr('NOTC_LONG.WAV') and by['NOTC_LONG.WAV']['include'], by['NOTC_LONG.WAV']['flags']
    assert not fr('20261111_090000_01LAV_001.WAV') and by['20261111_090000_01LAV_001.WAV']['include']
    # you tick one back on: it stays used through the next check
    c = by['NOTC_SHORT.WAV']; c['include'] = True; c['user_include'] = True
    p.check_short_clips(20)
    assert c['include'], 'your choice was overridden'
    # nothing on disk was deleted, renamed or added
    assert sorted(os.listdir(day)) == names, os.listdir(day)
    print('FALSE RECORDS GOOD')
finally:
    shutil.rmtree(D, ignore_errors=True)
