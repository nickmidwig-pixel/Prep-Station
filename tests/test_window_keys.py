"""The window driven by the keyboard against the real Python side (pywebview's bridge replaced by Playwright):
⌘A / ⌫ / ⌘Z / ⇧⌘Z, ⌘G grouping, the camera letter menu, ⌘C, ⌘, and ⌘/ sheets, arrows, zoom keys and slider.
Needs Playwright + Chromium (skips cleanly without them). Original files are only read."""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app'))
sys.path.insert(0, HERE)
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print('WINDOW KEYS SKIPPED (no Playwright)')
    sys.exit(0)
import make_canon_raw
from midwigsync.gui import Api

make_canon_raw.build()
UI = 'file://' + os.path.join(HERE, '..', 'app', 'midwigsync', 'ui', 'index.html')
api = Api()
api.emit = lambda kind, data=None: None
api.p.add_paths(['/home/claude/canonday_plain']); api.p.check_short_clips(20); api._refresh_defaults()
copied = []
api.copy_text = lambda t: copied.append(t) or True
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


BRIDGE = """
window.pywebview = { api: new Proxy({}, { get: (_, k) => async (...a) => window.pycall(k, a) }) };
"""
with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={'width': 1440, 'height': 900})
    errs = []
    pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.expose_function('pycall', dispatch)
    pg.add_init_script(BRIDGE)
    pg.goto(UI)
    pg.wait_for_timeout(900)
    inc = lambda: [c['include'] for c in api.p.clips]
    lanes = lambda: [(l['label'], l['rolls']) for l in api.p.lanes() if l['kind'] == 'camera']
    k = lambda key, t=250: (pg.keyboard.press(key), pg.wait_for_timeout(t))

    print('select and leave out')
    k('Meta+a')
    check('4 clips selected' in pg.inner_text('#inspMany'), pg.inner_text('#inspMany'))
    k('Backspace')
    check(inc() == [False] * 4, f'⌫ left all out: {inc()}')
    k('Meta+z')
    check(inc() == [True] * 4, f'⌘Z: {inc()}')
    check(not pg.is_hidden('#toast') and 'Undid' in pg.inner_text('#toast'), pg.inner_text('#toast'))
    k('Meta+Shift+z')
    check(inc() == [False] * 4, f'⇧⌘Z: {inc()}')
    k('Meta+z')

    print('lanes')
    check(lanes() == [('A CAM', ['Card1', 'Card2']), ('C CAM', ['Card3', 'Card4'])], lanes())
    # the C CAM lane label on the timeline opens the letter menu (cameras stack like an edit: C above A, so first row)
    box = pg.locator('#tl').bounding_box()
    pg.mouse.click(box['x'] + 40, box['y'] + 26 + 3 + 17)
    pg.wait_for_timeout(200)
    check(not pg.is_hidden('#popMenu') and 'C CAM' in pg.inner_text('#popMenu'), 'letter menu opens on C CAM')
    pg.click('#popMenu [data-letter="B"]'); pg.wait_for_timeout(300)
    check(lanes() == [('A CAM', ['Card1', 'Card2']), ('B CAM', ['Card3', 'Card4'])], f'relettered: {lanes()}')
    k('Meta+z')
    check(lanes()[1][0] == 'C CAM', f'⌘Z puts it back: {lanes()}')
    k('Meta+1')
    check('4 clips selected' in pg.inner_text('#inspMany'), '⌘1 picks the camera clips')
    k('Meta+g')
    check(lanes() == [('A CAM', ['Card1', 'Card2', 'Card3', 'Card4'])], f'⌘G one camera: {lanes()}')
    k('Meta+z')
    check(len(lanes()) == 2, f'⌘Z splits them again: {lanes()}')

    print('one clip')
    k('Escape')
    pg.click('#clipRows tr[data-id] td.file'); pg.wait_for_timeout(250)
    check(not pg.is_hidden('#insp'), 'click shows the clip')
    k('Meta+c')
    check(copied and copied[-1] == '10:49:02:08', f'⌘C copied {copied[-1:]}')
    k('ArrowDown')
    check(pg.inner_text('#iName').startswith('A002C001'), f'↓ next clip: {pg.inner_text("#iName")}')
    ph0 = pg.inner_text('#phTc')
    k('ArrowRight')
    check(pg.inner_text('#phTc') != ph0, f'→ one frame: {ph0} -> {pg.inner_text("#phTc")}')
    k('t')
    check(pg.evaluate('document.activeElement.id') == 'iTcIn', 'T types a timecode')
    pg.keyboard.type('11:20:01:00'); k('Enter', 400)
    c2 = next(c for c in api.p.clips if c['name'].startswith('A002'))
    check(c2.get('manual') and c2['start'] != c2['orig_start'], 'timecode set from the keyboard')
    pg.click('#tl'); k('Meta+z', 400)
    check(not c2.get('manual') and c2['start'] == c2['orig_start'], '⌘Z takes the typed timecode back')

    print('view')
    k('Shift+z')
    z0 = pg.inner_text('#zoomPct')
    k('+'); k('+')
    z1 = pg.inner_text('#zoomPct')
    check(z0 == '100%' and z1 == '400%', f'zoom {z0} -> {z1}')
    pg.fill('#zoomSlider', '0'); pg.dispatch_event('#zoomSlider', 'input'); pg.wait_for_timeout(200)
    check(pg.inner_text('#zoomPct') == '100%', f'slider to the left fits the day: {pg.inner_text("#zoomPct")}')

    print('sheets')
    k('Meta+Comma')
    check(not pg.is_hidden('#settingsSheet'), '⌘, opens Settings')
    pg.click('#settingsSheet input[data-set="transcribe"]'); pg.wait_for_timeout(300)
    check(api.settings['transcribe'] is True, 'a setting changes from the sheet')
    k('Escape')
    check(pg.is_hidden('#settingsSheet'), 'Esc closes it')
    check(pg.get_attribute('#steps li[data-key="transcribe"]', 'aria-checked') == 'true', 'the step list shows it on')
    pg.click('#steps li[data-key="transcribe"]'); pg.wait_for_timeout(300)
    check(api.settings['transcribe'] is False and pg.get_attribute('#steps li[data-key="transcribe"]', 'aria-checked') == 'false', 'clicking the step turns it off')
    pg.click('#steps li[data-key="aap_mode"]'); pg.wait_for_timeout(300)
    check(api.settings['aap_mode'] == 'skip', f"clicking Auto-Align turns it off ({api.settings['aap_mode']})")
    pg.click('#steps li[data-key="aap_mode"]'); pg.wait_for_timeout(300)
    check(api.settings['aap_mode'] == 'resolve', 'and back on')
    before = bool(api.settings.get('export_otio'))
    pg.click('#steps label.pick:has(input[data-set="export_otio"])'); pg.wait_for_timeout(300)
    check(bool(api.settings.get('export_otio')) != before, f"timeline chip switches OTIO ({before} -> {api.settings.get('export_otio')})")
    pg.click('#steps label.pick:has(input[data-set="export_otio"])'); pg.wait_for_timeout(300)
    k('Meta+Slash')
    check(not pg.is_hidden('#keysSheet') and 'These cards are one camera' in pg.inner_text('#keysSheet'), '⌘/ lists the shortcuts')
    k('Escape')

    print('remove and bring back')
    k('Meta+a'); k('Meta+Backspace', 400)
    check(len(api.p.clips) == 0, '⌘⌫ removed them from the list')
    k('Meta+z', 400)
    check(len(api.p.clips) == 4 and len(lanes()) == 2, '⌘Z brought them back')
    check(not errs, f'page errors: {errs}')
    b.close()
print('WINDOW KEYS GOOD' if ok else 'WINDOW KEYS FAILED')
sys.exit(0 if ok else 1)
