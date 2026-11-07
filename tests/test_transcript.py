"""Transcript plumbing with a stand-in speech engine (timing real, words not): stretches, listening source,
speakers from lav track names, TC at turns and every minute, Word/TXT/CSV written. Mixer day: Jane + Bob lavs."""
import os, re, shutil, sys
os.environ['PREP_FAKE_ASR'] = '1'
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync.scan import Project
from midwigsync import transcribe as TR, safety, tc as TC

p = Project(); p.add_paths(['/home/claude/mixerday/SOUND_ROLL_01']); p.check_short_clips(20); p.analyze()
groups = TR.plan(p.clips)
print(f'{len(groups)} stretches; listening to:', sorted({g["listen_what"] for g in groups}),
      '| speakers:', sorted({x['label'] for g in groups for x in g['speakers']}))
out = '/tmp/transcript_test'; shutil.rmtree(out, ignore_errors=True); safety.allow_output(out)
paths = TR.transcribe_day(p, out, 'MIXERDAY', p.fps, p.df, log=print)
txt = open(next(x for x in paths if x.endswith('.txt'))).read()
lines = [l for l in txt.splitlines() if re.match(r'^\d\d:\d\d:\d\d:\d\d ', l)]
spk = sorted(set(re.findall(r'^\S+\s+([A-Z][A-Z ]+):', '\n'.join(lines), re.M)))
mins = re.findall(r'\[(\d\d:\d\d:00:00)\]', txt)
print(f'{len(lines)} paragraphs, speakers in text: {spk}, minute stamps: {len(mins)}')
print('\n'.join(txt.splitlines()[:14]))
from docx import Document
d = Document(next(x for x in paths if x.endswith('.docx')))
print('docx paragraphs:', len(d.paragraphs), '| csv rows:', sum(1 for _ in open(next(x for x in paths if x.endswith('.csv')))) - 1)
ok = (len(groups) == 6 and {g['listen_what'] for g in groups} == {'mix'} and spk == ['BOB', 'JANE']
      and len(lines) > 10 and len(mins) >= 3 and len(paths) == 3)

# ---- a Sound Devices 633 take named the way the real 2026-11-06 day was: a per-person "Best Mic" mix, the boom
# and its -6 dB safety, three lavs with the person's name. Inputs 1-5 + 7 recorded, so CHANNEL_INDEX skips 6 and
# the bext sTRK list is mangled; the iXML interleave order is the truth.
import numpy as np
import xml.etree.ElementTree as ET
from midwigsync import bwf
d633 = '/tmp/day633/SOUND'
shutil.rmtree('/tmp/day633', ignore_errors=True); os.makedirs(d633)
SR = 48000; n = 40 * SR
rng = np.random.default_rng(6)
t = np.arange(n) / SR
def talk(seed, gaps):
    r = np.random.default_rng(seed); x = np.zeros(n, np.float32)
    for a, b in gaps:
        seg = r.standard_normal(int((b - a) * SR)).astype(np.float32) * 0.2 * (1 + np.sin(2 * np.pi * 4 * t[:int((b - a) * SR)]))
        x[int(a * SR):int(a * SR) + len(seg)] += seg
    return x
alex, ellie, clara = talk(1, [(1, 6), (16, 20)]), talk(2, [(7, 11), (21, 26)]), talk(3, [(12, 15), (28, 34)])
boom = 0.5 * (alex + ellie + clara) + 0.003 * rng.standard_normal(n).astype(np.float32)
names = ['Ale Best Mic', 'BOOM ', 'BOOM -6db', 'Lav1 Alex', 'Lav 2 Ellie', 'Lav 5 Clara']
x = np.stack([boom, boom, boom * 0.5, alex, ellie, clara], 1)
start = TC.tc_to_samples('10:49:34:22', TC.parse_rate('23.976'))
desc = 'sTRK1=Ale Best Mic\r\nsTRK2=BOOM \r\nsTRK3=BOOM -6db\r\nsTRK4=Lav1 Alex\r\nsTRK5=Lav 2 Ellie\r\nsTRK7=Lav 5 Clara\r\n' \
       'dTC=10:49:34:22\r\ndUBITS=11062624\r\ndFRAMERATE=23.976ND\r\n'
be = bwf.build_bext(desc, 'Sound Devices', '633', '2026-11-06', '10:49:34', start, 'A=PCM,F=48000,W=24,M=6\r\n')
ix = ET.fromstring(bwf.build_ixml('UDP', '23.976', '24000/1001', False, start, SR, 24, names, scene='Conversat', take='1')[38:])
for tr, ci in zip(ix.iter('TRACK'), (1, 2, 3, 4, 5, 7)):
    tr.find('CHANNEL_INDEX').text = str(ci)
ix = b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(ix, encoding='utf-8')
w = bwf.WavWriter(os.path.join(d633, 'ConversatT01.WAV'), n, 6, SR, 24, bwf.FMT_PCM, be, ix)
w.write_float(x.astype(np.float32)); w.close()
q = Project(); q.add_paths([d633]); q.analyze()
c = q.clips[0]
roles = Project.clip_roles(c)
g633 = TR.plan(q.clips)
spk633 = sorted({x['label'] for g in g633 for x in g['speakers']})
print('633 track names:', [c['track_names'].get(i + 1) for i in range(6)])
print('633 roles:', roles, '| listening to:', sorted({g['listen_what'] for g in g633}), '| speakers:', spk633)
out2 = '/tmp/transcript_633'; shutil.rmtree(out2, ignore_errors=True); safety.allow_output(out2)
p2 = TR.transcribe_day(q, out2, '20261106', q.fps, q.df, log=lambda *a: None)
t2 = open(next(x for x in p2 if x.endswith('.txt'))).read()
spk2 = sorted(set(re.findall(r'^\S+\s+([A-Z][A-Z ]+):', t2, re.M)))
print('633 speakers in text:', spk2)
ok &= (roles == ['mix', 'boom', 'safety', 'lav', 'lav', 'lav'] and spk633 == ['ALEX', 'CLARA', 'ELLIE']
       and c['track_names'].get(6) == 'Lav 5 Clara' and set(spk2) <= {'ALEX', 'CLARA', 'ELLIE'} and len(spk2) >= 2)
print('TRANSCRIPT GOOD' if ok else 'TRANSCRIPT FAILED')
