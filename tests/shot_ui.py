"""Screenshots of the window on a test day (for a visual check of UI changes) and a smoke test of the 2.0
controls: Sync-to sentence + chooser, polywav track rows. Needs Playwright; skips cleanly without it."""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app'))
sys.path.insert(0, HERE)
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print('UI SHOTS SKIPPED (no Playwright)')
    sys.exit(0)
from midwigsync.gui import Api

DAY = sys.argv[1] if len(sys.argv) > 1 else '/home/claude/testday'
OUT = sys.argv[2] if len(sys.argv) > 2 else '/tmp/ui'
os.makedirs(OUT, exist_ok=True)
UI = 'file://' + os.path.join(HERE, '..', 'app', 'midwigsync', 'ui', 'index.html')
api = Api()
api.emit = lambda kind, data=None: None
api.p.add_paths([DAY]); api.p.check_short_clips(20); api._refresh_defaults()
ok = True


def check(cond, what):
    global ok
    ok &= bool(cond)
    print(f"  {'ok ' if cond else 'BAD'} {what}")


def dispatch(name, args):
    fn = getattr(api, name, None)
    if not callable(fn) or name.startswith('_'):
        return None
    if name in ('choose_folders', 'choose_files', 'choose_output', 'run', 'play', 'frame', 'open_path', 'reveal'):
        return None
    return json.loads(json.dumps(fn(*args), default=str))


BRIDGE = "window.pywebview = { api: new Proxy({}, { get: (_, k) => async (...a) => window.pycall(k, a) }) };"
with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={'width': 1440, 'height': 900})
    errs = []
    pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.expose_function('pycall', dispatch)
    pg.add_init_script(BRIDGE)
    pg.goto(UI)
    pg.wait_for_timeout(1200)
    pg.screenshot(path=f'{OUT}/1_day.png')
    txt = pg.inner_text('#syncText')
    print('  sync line:', txt)
    check('syncs to' in txt, 'the Sync-to line reads as a sentence')
    has_cam = any(c['kind'] == 'video' for c in api.p.clips)
    check(pg.is_hidden('#syncChange') != has_cam, 'Change link shown only when there is a camera to choose')
    if not has_cam:
        pg.screenshot(path=f'{OUT}/2_chooser.png')
    else:
      pg.click('#syncChange'); pg.wait_for_timeout(200)
      opts = pg.locator('#syncChooser [data-sync]').all_inner_texts()
      print('  choices:', [o.replace('\n', ' ') for o in opts])
      check(any('Sound only' in o for o in opts) and len(opts) >= 2, 'chooser: camera bodies + Sound only, nothing else')
      pg.screenshot(path=f'{OUT}/2_chooser.png')
      pg.click('#syncChooser [data-sync="audio"]'); pg.wait_for_timeout(400)
      check('Sound only' in pg.inner_text('#syncText'), f"after choosing Sound only: {pg.inner_text('#syncText')}")
      check(api.settings.get('mode') == 'audio', 'setting = audio')
      pg.click('#syncChange'); pg.wait_for_timeout(200)
      cams = [e for e in pg.locator('#syncChooser [data-sync]').all() if e.get_attribute('data-sync') not in ('audio', 'auto')]
      cams[0].click(); pg.wait_for_timeout(400)
      r = api.settings.get('reference') or {}
      check(api.settings.get('mode') == 'camera' and r.get('listen') and not r.get('auto'), f'camera chosen: app still listens for the channel ({r})')
      pg.click('#syncChange'); pg.wait_for_timeout(200)
      pg.click('#syncChooser [data-sync="auto"]'); pg.wait_for_timeout(400)
      check(api.settings.get('mode') == 'auto', 'back to auto')
    # polywav rows
    btn = pg.locator('.trkbtn').first
    check(btn.count() == 1, 'multitrack sound files show one summary button')
    print('  tracks summary:', btn.inner_text().replace('\n', ' '))
    btn.click(); pg.wait_for_timeout(200)
    check(pg.locator('tr.trkrow').count() == 1, 'click opens the track list')
    pg.screenshot(path=f'{OUT}/3_tracks.png')
    check(not errs, f'no page errors {errs[:2]}')
    b.close()
print('UI SHOTS OK' if ok else 'UI SHOTS FAILED')
sys.exit(0 if ok else 1)
