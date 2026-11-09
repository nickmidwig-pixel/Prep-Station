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
    # 2.2: drag-select on the timeline, sortable columns, mute / solo boxes
    box = pg.locator('#tl').bounding_box()
    x0, y0 = box['x'] + 250, box['y'] + 55          # starts ON a clip: a drag from a clip must still draw the box
    pg.mouse.move(x0, y0); pg.mouse.down(); pg.mouse.move(x0 + 500, y0 + 120, steps=8); pg.mouse.up()
    pg.wait_for_timeout(200)
    sel = pg.inner_text('#selLine') if not pg.is_hidden('#selLine') else ''
    print('  drag-select ->', sel.split('⌫')[0].strip())
    check('clips selected' in sel, 'dragging a box on the timeline selects the clips it touches')
    check('slow motion' in sel.lower(), 'the selection offers Mark as slow motion for the cameras in it')
    pg.click('#clipTable th[data-sort="name"]'); pg.wait_for_timeout(150)
    check('on' in (pg.get_attribute('#clipTable th[data-sort="name"]', 'class') or ''), 'clicking File sorts by name (arrow on)')
    pg.click('#clipTable th[data-sort="name"]'); pg.wait_for_timeout(150)
    check('desc' in (pg.get_attribute('#clipTable th[data-sort="name"]', 'class') or ''), 'second click flips the order')
    pg.click('#clipTable th[data-sort="start"]'); pg.wait_for_timeout(150)
    pg.mouse.click(box['x'] + 150, box['y'] + 47)       # the M box of the first lane
    pg.wait_for_timeout(150)
    pg.screenshot(path=f'{OUT}/4_controls.png')
    # 2.3: fps column, draggable column widths, draggable panes, JKL shuttle (play stubbed -> badge stays hidden)
    check(pg.query_selector('#clipTable th[data-sort="fps"]') is not None, 'clip list has an fps column')
    fps_cells = [t.strip() for t in pg.eval_on_selector_all('#clipTable td.fps', 'els => els.map(e => e.textContent)')]
    check(any(t and t != '—' for t in fps_cells), f'camera rows show a frame rate {fps_cells[:4]}')
    check(pg.query_selector('.legend .sw[style*="D9534F"]') is None and 'Cameras' not in pg.inner_text('.legend'), 'legend has no camera swatches')
    check(pg.query_selector('#backBtn') is None, 'no back-5-seconds button')
    g = pg.query_selector('#clipTable th[data-sort="name"] .grip').bounding_box()
    w_before = pg.eval_on_selector('#clipCols col[data-col="name"]', 'e => e.style.width')
    pg.mouse.move(g['x'] + 4, g['y'] + 10); pg.mouse.down(); pg.mouse.move(g['x'] - 96, g['y'] + 10, steps=6); pg.mouse.up()
    w_after = pg.eval_on_selector('#clipCols col[data-col="name"]', 'e => e.style.width')
    check(w_before != w_after and int(w_after[:-2]) < int(w_before[:-2]), f'dragging the File grip narrows the column {w_before} -> {w_after}')
    check('on' in (pg.get_attribute('#clipTable th[data-sort="start"]', 'class') or ''), 'the grip drag did not re-sort')
    vb = pg.query_selector('#vsplit').bounding_box()
    h_before = pg.query_selector('#timeline').bounding_box()['height']
    pg.mouse.move(vb['x'] + vb['width'] / 2, vb['y'] + 4); pg.mouse.down(); pg.mouse.move(vb['x'] + vb['width'] / 2, vb['y'] + 84, steps=6); pg.mouse.up()
    h_after = pg.query_selector('#timeline').bounding_box()['height']
    check(h_after > h_before + 60, f'dragging the split grows the timeline {h_before:.0f} -> {h_after:.0f}')
    hb = pg.query_selector('#hsplit').bounding_box()
    r_before = pg.query_selector('#run').bounding_box()['width']
    pg.mouse.move(hb['x'] + 3, hb['y'] + 300); pg.mouse.down(); pg.mouse.move(hb['x'] - 117, hb['y'] + 300, steps=6); pg.mouse.up()
    r_after = pg.query_selector('#run').bounding_box()['width']
    check(r_after > r_before + 100, f'dragging the side split widens the panel {r_before:.0f} -> {r_after:.0f}')
    pg.keyboard.press('l'); pg.wait_for_timeout(200)
    check(pg.query_selector('#shuttle') is not None, 'shuttle badge present')
    pg.screenshot(path=f'{OUT}/5_resized.png')
    # 2.4: two worlds — mark the selection as slow motion and it moves under the SLOW MOTION divider, in list and timeline
    rows0 = pg.evaluate('window.MS.rows()')
    check(not any(r['type'] == 'divider' for r in rows0), 'no slow-motion world before anything is marked')
    pg.evaluate("document.querySelector('#selLine button, #selLine .linkbtn') && document.querySelector('#selLine button, #selLine .linkbtn').click()")
    pg.wait_for_timeout(400)
    rows1 = pg.evaluate('window.MS.rows()')
    check(any(r['type'] == 'divider' for r in rows1) and any(r['slow'] for r in rows1), f'marking adds the slow-motion world {[(r["type"], r["key"]) for r in rows1]}')
    check(pg.query_selector('#clipTable tr.world') is not None and pg.query_selector('#clipTable tr.grp.slow') is not None, 'clip list has the SLOW MOTION section with its camera group')
    check(not errs, f'no page errors {errs[:2]}')
    b.close()
print('UI SHOTS OK' if ok else 'UI SHOTS FAILED')
sys.exit(0 if ok else 1)
