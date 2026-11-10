"""Auto-Align renders left beside the sound (2.5.2 did that) are never read as source files."""
import os, shutil, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app'))
from midwigsync import media
src = '/home/claude/testday'
wavs = [os.path.join(d, f) for d, _, fs in os.walk(src) for f in fs if f.lower().endswith('.wav') and '_SYNCED' not in d]
tmp = tempfile.mkdtemp(prefix='renders_')
try:
    lav = os.path.join(tmp, '20261103', 'SOUND', '01LAV'); os.makedirs(lav)
    shutil.copy(wavs[0], os.path.join(lav, 'T01.WAV'))
    for i in range(3):
        shutil.copy(wavs[0], os.path.join(lav, f'T01_si0ch0off{i}-AtAlgn-00{i}beef.wav'))
    found, _ = media.find_media([tmp])
    names = sorted(os.path.basename(p) for p in found)
    assert names == ['T01.WAV'], names
    print('IGNORE RENDERS GOOD')
finally:
    shutil.rmtree(tmp, ignore_errors=True)
