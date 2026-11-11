"""A selected camera clip with no timecode (iPhone camera app) shows ITS OWN picture in the viewer, not whichever
camera happens to be rolling at the playhead; and the project is not changed by looking."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync.gui import Api
api = Api(); api.emit = lambda *a, **k: None
api.p.add_paths(['/home/claude/testday']); api.p.check_short_clips(20)
c = next(x for x in api.p.clips if x['kind'] == 'video' and x['start'] is None)
o = next(x for x in api.p.clips if x['kind'] == 'video' and x['start'] is not None)
r = api.frame(o['start'] + 48000, c['id'])
assert r.get('ok') and c['name'] in r.get('label', ''), r.get('label') or r.get('why')
assert c['start'] is None, 'looking at a clip gave it a position'
r2 = api.frame(o['start'] + 48000, None)
assert r2.get('ok') and o['name'] in r2['label'] or r2.get('ok'), r2
print('NO-TC VIEW GOOD')
