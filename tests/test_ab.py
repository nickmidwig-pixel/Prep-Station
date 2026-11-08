"""Hear the difference: after a run, ab_play builds a before/after preview (camera mic + boom from the mixer's
original file, then from the aligned file) at the playhead. Runs the mixer day through the Api (Auto-Align
skipped), then asks for the preview and checks the file and the schedule."""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app'))
from midwigsync.gui import Api
from midwigsync import safety
from midwigsync.brand import CACHE

DAY = '/home/claude/mixerday'
api = Api()
events = []
api.emit = lambda kind, data=None: events.append((kind, data))
api.p.add_paths([DAY]); api.p.check_short_clips(20); api._refresh_defaults()
api.settings.update(dict(aap_mode='skip', review_tc=False, transcript=False, export_aaf=False, export_ale=False))
out = os.path.join(DAY, '20261104_SYNCED')
safety.allow_output(out)
assert api.run(), 'run did not start'
for _ in range(1500):
    if any(k in ('done', 'error') for k, _ in events):
        break
    time.sleep(0.2)
assert any(k == 'done' for k, _ in events), [e for e in events if e[0] == 'error']
ok = True


def check(c, w):
    global ok
    ok &= bool(c)
    print(f"  {'ok ' if c else 'BAD'} {w}")


snd = [c for c in api.p.clips if c['kind'] == 'audio' and c['include']]
at = snd[0]['start'] + 48000 * 20
# Auto-Align was skipped here, so stand in for its result: the aligned twin of each take is the take itself
api.job.aligned = [dict(source=c.get('stamped_path') or c['path'], aligned=c['path'], start=c['start'], roll=c['roll'])
                   for c in snd]
r = api.ab_play(at)
print('  ab_play ->', {k: v for k, v in r.items() if k != 'sched'})
ab = os.path.join(CACHE, 'preview', 'ab.wav')
check(os.path.exists(ab) and os.path.getsize(ab) > 48000 * 2 * 2 * 15, 'before/after preview written (~17 s stereo)')
check(r.get('ok') or 'Mac' in (r.get('why') or ''), 'ab_play reports ok (or Mac-only playback off the Mac)')
if r.get('sched'):
    check([l for _, l in r['sched']] == ['BEFORE', 'AFTER', 'BEFORE', 'AFTER'], 'schedule: before, after, before, after')
    check('boom' in r['what'].lower() or 'mic' in r['what'].lower(), f"what: {r['what']}")
# nothing aligned yet -> a plain answer, no crash
api2 = Api(); api2.emit = lambda *a: None
api2.p.add_paths([DAY]); api2.p.check_short_clips(20); api2._refresh_defaults()
r2 = api2.ab_play(at)
check(not r2.get('ok') and 'Sync the day first' in r2.get('why', ''), 'before a run it says to sync first')
print('AB OK' if ok else 'AB FAILED')
sys.exit(0 if ok else 1)
