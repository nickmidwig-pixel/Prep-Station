"""Walk the whole window like a user, through every state, and screenshot each one into /tmp/walk/<size>/.
A real run (fake Auto-Align panel) drives the progress, confirm-fixes, Auto-Align card, transcript and done states.
Usage: python3 tests/walk.py [day] [WxH ...]      e.g. python3 tests/walk.py /home/claude/testday 1440x900 1280x800
Prints every page error and every check; screenshots are for looking at."""
import json, os, shutil, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app')); sys.path.insert(0, HERE)
from playwright.sync_api import sync_playwright
from midwigsync import gui, safety
import run_pipeline as rp

DAY = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1][0].isdigit() else '/home/claude/testday'
SIZES = [a for a in sys.argv[1:] if a[0].isdigit()] or ['1440x900']
UI = 'file://' + os.path.join(HERE, '..', 'app', 'midwigsync', 'ui', 'index.html')
BRIDGE = "window.pywebview = { api: new Proxy({}, { get: (_, k) => async (...a) => window.pycall(k, a) }) };"
BLOCK = ('choose_folders', 'choose_files', 'choose_output', 'play', 'open_path', 'reveal', 'open_in_app', 'copy_text')


def walk(size):
    W, H = (int(x) for x in size.split('x'))
    out = f'/tmp/walk/{size}'
    shutil.rmtree(out, ignore_errors=True); os.makedirs(out)
    api = gui.Api()
    q = []
    lock = threading.Lock()
    api.emit = lambda kind, data=None: (lock.acquire(), q.append((kind, data)), lock.release())
    n = [0]

    def dispatch(name, args):
        if not hasattr(api, name) or name.startswith('_') or name in BLOCK:
            return None
        return json.loads(json.dumps(getattr(api, name)(*args), default=str))

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={'width': W, 'height': H})
        errs = []
        pg.on('pageerror', lambda e: errs.append(str(e)))
        pg.on('console', lambda m: errs.append('console: ' + m.text) if m.type == 'error' else None)
        pg.expose_function('pycall', dispatch)
        pg.add_init_script(BRIDGE)
        pg.goto(UI); pg.wait_for_timeout(800)

        def pump(ms=300):
            end = time.time() + ms / 1000
            while time.time() < end:
                with lock:
                    batch, q[:] = q[:], []
                for k, d in batch:
                    pg.evaluate('([k, d]) => window.MS.event(k, d)', [k, json.loads(json.dumps(d, default=str))])
                pg.wait_for_timeout(40)

        def shot(name, **kw):
            n[0] += 1
            pg.screenshot(path=f'{out}/{n[0]:02d}_{name}.png', **kw)

        shot('empty')
        # load the day the way a drop does
        api.add_paths([DAY])
        for _ in range(200):
            pump(100)
            if not api.scanning:
                break
        pump(400)
        shot('loaded')
        # select a camera clip, a sound clip, a slow-motion clip
        tl = pg.query_selector('#tl').bounding_box()
        rows = pg.evaluate('window.MS.rows()')
        pg.evaluate("document.querySelector('#clipRows tr[data-id]').click()"); pump(500)
        shot('camera_selected')
        snd = pg.evaluate("[...document.querySelectorAll('#clipRows tr[data-id]')].map(t => t.innerText).findIndex(t => /\\.wav/i.test(t))")
        if snd >= 0:
            pg.evaluate(f"document.querySelectorAll('#clipRows tr[data-id]')[{snd}].click()"); pump(500)
            shot('sound_selected')
        slow = pg.evaluate("(() => { const r = [...document.querySelectorAll('#clipTable tr.grp.slow ~ tr[data-id]')]; return r.length })()")
        if slow:
            pg.evaluate("document.querySelector('#clipTable tr.grp.slow ~ tr[data-id]').click()"); pump(500)
            shot('slow_selected')
        # hover a clip on the timeline
        for yy in range(int(tl['y']) + 40, int(tl['y'] + tl['height']) - 5, 10):
            pg.mouse.move(tl['x'] + tl['width'] * 0.45, yy); pg.wait_for_timeout(40)
            if not pg.eval_on_selector('#tip', 'e => e.hidden'):
                shot('hover'); break
        pg.mouse.move(5, 5)
        # right-click menu
        tr = pg.query_selector('#clipRows tr[data-id]')
        if tr:
            bb = tr.bounding_box(); pg.mouse.click(bb['x'] + 200, bb['y'] + bb['height'] / 2, button='right'); pump(200)
            shot('context_menu'); pg.keyboard.press('Escape'); pump(150)
        # sheets
        pg.keyboard.press('Meta+,'); pump(300); shot('settings'); pg.keyboard.press('Escape'); pump(200)
        pg.keyboard.press('Meta+/'); pump(300); shot('shortcuts'); pg.keyboard.press('Escape'); pump(200)
        pg.keyboard.press('Escape'); pump(200)
        # a real run with the fake Auto-Align panel
        api.settings['_fake_aap'] = rp.fake_aap
        api.settings['transcribe'] = False
        pg.click('#goBtn'); pump(800)
        shot('running')
        t0 = time.time(); seen = set()
        while time.time() - t0 < 600:
            pump(300)
            if api.job and getattr(api.job, 'user_decide', None) is not None and 'review' not in seen and \
                    pg.evaluate("!!document.querySelector('#review:not([hidden]), .review:not([hidden])')"):
                seen.add('review'); shot('confirm_fixes')
                pg.click('#revWrite')
            if 'aap' not in seen and pg.evaluate("!document.getElementById('aapCard').hidden"):
                seen.add('aap'); pump(500); shot('autoalign_card')
            if not (api.job_thread and api.job_thread.is_alive()):
                pump(1500); break
        shot('done')
        pg.evaluate("window.scrollTo(0,0)")
        print(f'{size}: {n[0]} screenshots in {out}; page errors: {errs}')
        b.close()


for s in SIZES:
    walk(s)
