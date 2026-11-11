"""Click clips in the timeline: the row is visible in the list, and the playhead sits inside the clicked clip."""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app'))
from playwright.sync_api import sync_playwright
from midwigsync.gui import Api
api = Api(); api.emit = lambda k, d=None: None
api.p.add_paths(['/home/claude/testday']); api.p.check_short_clips(20); api._refresh_defaults()
def dispatch(name, args):
    fn = getattr(api, name, None)
    if not callable(fn) or name.startswith('_') or name in ('choose_folders', 'choose_files', 'choose_output', 'run', 'play', 'frame', 'open_path', 'reveal'): return None
    return json.loads(json.dumps(fn(*args), default=str))
with sync_playwright() as pw:
    b = pw.chromium.launch(); pg = b.new_page(viewport={'width': 1390, 'height': 900})
    errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.expose_function('pycall', dispatch)
    pg.add_init_script("window.pywebview = { api: new Proxy({}, { get: (_, k) => async (...a) => window.pycall(k, a) }) };")
    pg.goto('file://' + os.path.join(HERE, '..', 'app', 'midwigsync', 'ui', 'index.html')); pg.wait_for_timeout(1500)
    tl = pg.query_selector('#tl').bounding_box()
    n = 0
    for row in range(1, 8):                                   # one click per lane, at a few x positions
        for fx in (0.3, 0.55, 0.8):
            pg.mouse.click(tl['x'] + tl['width'] * fx, tl['y'] + 26 + 38 * row - 19); pg.wait_for_timeout(150)
            st = pg.evaluate("""() => { const s = window.MS.state(); if (s.sel == null) return null;
                const c = s.project.clips.find(x => x.id === s.sel); const r = document.querySelector(`tr[data-id="${s.sel}"]`);
                if (!r) return {row: false};
                let box = r.parentElement; while (box && !(box.scrollHeight > box.clientHeight + 1 && /(auto|scroll)/.test(getComputedStyle(box).overflowY))) box = box.parentElement;
                const br = box.getBoundingClientRect(), rr = r.getBoundingClientRect(), hh = box.querySelector('thead').offsetHeight;
                return {row: true, vis: rr.top >= br.top + hh - 1 && rr.bottom <= br.bottom + 1, inside: s.ph >= c.start && s.ph < c.start + c.length}; }""")
            if st is None: continue
            n += 1
            assert st['row'] and st['vis'] and st['inside'], (row, fx, st, pg.evaluate("(() => { const r = document.querySelector('tr.sel, tr[aria-selected=true]'); const o=[]; let b = document.querySelector('table').parentElement; while (b && b !== document.body) { o.push([b.className, b.scrollHeight, b.clientHeight, getComputedStyle(b).overflowY, b.scrollTop]); b = b.parentElement; } return o; })()"))
    assert n >= 5, n
    assert not errs, errs
    b.close()
print('CLICK CLIP GOOD', n)
