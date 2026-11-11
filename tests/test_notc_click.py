"""Click (and double-click) a no-timecode camera clip in the file list: the viewer shows that clip's picture."""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app'))
from playwright.sync_api import sync_playwright
from midwigsync.gui import Api
api = Api(); api.emit = lambda k, d=None: None
api.p.add_paths(['/home/claude/testday']); api.p.check_short_clips(20); api._refresh_defaults()
c = next(x for x in api.p.clips if x['kind'] == 'video' and x['start'] is None)
def dispatch(name, args):
    fn = getattr(api, name, None)
    if not callable(fn) or name.startswith('_') or name in ('choose_folders', 'choose_files', 'choose_output', 'run', 'play', 'open_path', 'reveal'): return None
    return json.loads(json.dumps(fn(*args), default=str))
with sync_playwright() as pw:
    b = pw.chromium.launch(); pg = b.new_page(viewport={'width': 1390, 'height': 900})
    errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.expose_function('pycall', dispatch)
    pg.add_init_script("window.pywebview = { api: new Proxy({}, { get: (_, k) => async (...a) => window.pycall(k, a) }) };")
    pg.goto('file://' + os.path.join(HERE, '..', 'app', 'midwigsync', 'ui', 'index.html')); pg.wait_for_timeout(1500)
    ok = True
    for how in ('click', 'dblclick'):
        pg.keyboard.press('Escape'); pg.wait_for_timeout(200)
        row = pg.locator(f'tr[data-id="{c["id"]}"] td').nth(1)
        row.scroll_into_view_if_needed()
        getattr(row, how)(); pg.wait_for_timeout(2500)
        note = pg.inner_text('#frameNote'); on = pg.evaluate("document.getElementById('frame').classList.contains('on')")
        good = on and c['name'] in note
        print(f"  {'ok ' if good else 'BAD'} {how}: viewer says {note!r}")
        ok = ok and good
    ok = ok and not errs
    print('NO-TC CLICK GOOD' if ok else f'NO-TC CLICK FAILED {errs}'); b.close()
sys.exit(0 if ok else 1)
