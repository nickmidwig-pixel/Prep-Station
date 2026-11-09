"""Screenshot of the window on any day folder (visual check only): python3 tests/shot_day.py <day> <out.png>
Prints the timeline rows the page built (type / key) so a test can assert on the two-world layout."""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app'))
from playwright.sync_api import sync_playwright
from midwigsync.gui import Api

DAY, OUT = sys.argv[1], sys.argv[2]
UI = 'file://' + os.path.join(HERE, '..', 'app', 'midwigsync', 'ui', 'index.html')
api = Api()
api.emit = lambda kind, data=None: None
api.p.add_paths([DAY]); api.p.check_short_clips(20); api._refresh_defaults()


def dispatch(name, args):
    fn = getattr(api, name, None)
    if not callable(fn) or name.startswith('_') or name in ('choose_folders', 'choose_files', 'choose_output', 'run', 'play', 'frame', 'open_path', 'reveal'):
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
    pg.wait_for_timeout(1500)
    rows = pg.evaluate("window.MS && window.MS.rows ? window.MS.rows() : null")
    print('ROWS', json.dumps(rows))
    pg.screenshot(path=OUT)
    if pg.query_selector('tr.world'):
        pg.evaluate("document.querySelector('tr.world').scrollIntoView({block:'start'})"); pg.wait_for_timeout(200)
        pg.screenshot(path=OUT.replace('.png', '_table.png'), clip={'x': 0, 'y': 700, 'width': 1100, 'height': 200})
    print('ERRORS', errs)
    b.close()
