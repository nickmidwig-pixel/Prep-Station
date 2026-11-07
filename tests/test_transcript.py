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
print('TRANSCRIPT GOOD' if ok else 'TRANSCRIPT FAILED')
