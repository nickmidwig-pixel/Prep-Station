"""Two shoot days dropped together (camera day + mixer day): each runs as its own job and folder, through the window's API."""
import os, sys, time, shutil
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync.gui import Api

a = Api()
ev = []
a.window = None
a.emit = lambda k, d=None: ev.append((k, d))
a.p.add_paths(['/home/claude/testday', '/home/claude/mixerday/SOUND_ROLL_01'])
a.p.check_short_clips(20)
a._refresh_defaults()
a.settings.update(aap_mode='skip', synced_clips=False)
st = a._state()
print('days', [d['label'] for d in st['project']['days']])
print('names', st['day_names'])
for v in st['day_names'].values():
    shutil.rmtree(v['out_dir'], ignore_errors=True)
assert a.run('all')
a.job_thread.join(1200)
done = [d for k, d in ev if k == 'done']
dayev = [d for k, d in ev if k == 'day']
errs = [d for k, d in ev if k == 'error']
print('day events', [(d['i'], d['label']) for d in dayev])
print('errors', errs)
assert not errs and len(dayev) == 2 and len(done) == 1 and len(done[0]['all']) == 2, (len(done), done and done[-1].keys())
for d in done[0]['all']:
    out = d['out']
    files = sorted(os.listdir(out))
    print(os.path.basename(out), files)
    assert any(f.endswith('_REPORT.txt') for f in files) and 'READ ME.txt' in files and 'EDITORIAL' in files
outs = [os.path.basename(d['out']) for d in done[0]['all']]
assert outs == ['20261103_testday_SYNCED', '20261104_mixerday_SYNCED'], outs
print('TWO DAYS GOOD')
