"""Drive the Sound Radix Auto-Align Post 2 panel through macOS Accessibility.

The panel is not part of Resolve's window: Resolve starts it as its own Electron app (bundle id
com.github.Electron, window "Auto-Align Post"). Its Render button stays disabled until a reference track is
chosen and at least one source track is ticked, so pressing Render alone does nothing. This module:

  1. finds the Electron process whose window is titled "Auto-Align Post" (not any Electron app),
  2. picks the reference track in the "Reference Track" menu,
  3. ticks every source track (the list only shows ~6 rows; Tab walks focus down so the rest appear),
  4. sets Time / Phase if asked, checks "N selected", presses Render and confirms it started.

All Accessibility calls go through a small backend object so the logic runs in tests against a fake panel.
Nothing here touches files.
"""
import re
import time

KEY_TAB = 48


class PanelError(RuntimeError):
    pass


# ------------------------------------------------------------------ real macOS backend
class MacBackend:
    def __init__(self):
        import ApplicationServices as AS  # pyobjc-framework-ApplicationServices
        self.AS = AS

    def attr(self, el, name):
        try:
            err, val = self.AS.AXUIElementCopyAttributeValue(el, name, None)
            return val if err == 0 else None
        except Exception:  # noqa: BLE001
            return None

    def children(self, el):
        return list(self.attr(el, 'AXChildren') or [])

    def press(self, el):
        try:
            return self.AS.AXUIElementPerformAction(el, 'AXPress') == 0
        except Exception:  # noqa: BLE001
            return False

    def focus(self, el):
        try:
            return self.AS.AXUIElementSetAttributeValue(el, 'AXFocused', True) == 0
        except Exception:  # noqa: BLE001
            return False

    def apps(self):
        """[(handle, bundle_id, name, pid)] of running apps that could be the panel."""
        from AppKit import NSWorkspace
        out = []
        for a in NSWorkspace.sharedWorkspace().runningApplications():
            bid = str(a.bundleIdentifier() or '')
            nm = str(a.localizedName() or '')
            if 'electron' in bid.lower() or 'auto-align' in nm.lower() or 'autoalign' in bid.lower().replace('-', ''):
                out.append((a, bid, nm, int(a.processIdentifier())))
        return out

    def app_element(self, handle):
        el = self.AS.AXUIElementCreateApplication(handle.processIdentifier())
        try:
            self.AS.AXUIElementSetAttributeValue(el, 'AXManualAccessibility', True)  # Electron builds its AX tree
        except Exception:  # noqa: BLE001
            pass
        return el

    def activate(self, handle):
        try:
            handle.activateWithOptions_(2)      # NSApplicationActivateIgnoringOtherApps
        except Exception:  # noqa: BLE001
            pass

    def key(self, pid, keycode):
        try:
            import Quartz
            for down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
                Quartz.CGEventPostToPid(pid, ev)
                time.sleep(0.03)
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def sleep(s):
        time.sleep(s)


# ------------------------------------------------------------------ the panel
def _s(v):
    if v is None or hasattr(v, 'pointerValue'):
        return ''
    return str(v).strip()


class Panel:
    def __init__(self, backend, log=print):
        self.b = backend
        self.log = log
        self.app = None
        self.pid = None
        self.handle = None
        self.window = None

    # -- finding it
    def find(self, wait_s=40):
        t0 = time.time()
        while True:
            for handle, bid, nm, pid in self.b.apps():
                app = self.b.app_element(handle)
                for w in (self.b.attr(app, 'AXWindows') or []):
                    if 'auto-align' in _s(self.b.attr(w, 'AXTitle')).lower():
                        self.app, self.pid, self.handle, self.window = app, pid, handle, w
                        return True
            if time.time() - t0 >= wait_s:
                return False
            self.b.sleep(1.0)

    def close(self):
        """Close the panel's window (its close button). A panel left open from another project can show that
        project's tracks; reopening it from Resolve's menu loads the current timeline."""
        btn = self.b.attr(self.window, 'AXCloseButton')
        return bool(btn is not None and self.b.press(btn))

    # -- reading it
    def nodes(self):
        """Every element of the window in reading order: dict(el, role, label, enabled, value)."""
        out = []

        def walk(el, depth):
            role = _s(self.b.attr(el, 'AXRole'))
            label = ' '.join(x for x in (_s(self.b.attr(el, k)) for k in ('AXTitle', 'AXDescription')) if x)
            val = self.b.attr(el, 'AXValue')
            if not label and role == 'AXStaticText':
                label = _s(val)
            en = self.b.attr(el, 'AXEnabled')
            out.append(dict(el=el, role=role, label=label, enabled=en is not False and en != 0, value=val))
            if depth < 60:
                for ch in self.b.children(el):
                    walk(ch, depth + 1)
        walk(self.window, 0)
        return out

    def rows(self, nodes=None):
        """Source-track rows: [dict(el, label, checked, enabled)] in list order. A row is a checkbox and the
        first piece of text after it."""
        nodes = nodes if nodes is not None else self.nodes()
        rows = []
        for i, n in enumerate(nodes):
            if n['role'] != 'AXCheckBox':
                continue
            label = n['label']
            if not label:
                for m in nodes[i + 1:i + 6]:
                    if m['role'] == 'AXCheckBox':
                        break
                    if m['role'] == 'AXStaticText' and m['label']:
                        label = m['label']
                        break
            v = n['value']
            rows.append(dict(el=n['el'], label=label, checked=bool(v) and v not in ('0', 0), enabled=n['enabled']))
        return rows

    @staticmethod
    def _after(nodes, text_re, role='AXButton'):
        """The first `role` element after the static text matching text_re."""
        for i, n in enumerate(nodes):
            if n['role'] == 'AXStaticText' and re.match(text_re, n['label'], re.I):
                for m in nodes[i + 1:i + 12]:
                    if m['role'] == role:
                        return m
        return None

    def selected_count(self, nodes=None):
        for n in (nodes if nodes is not None else self.nodes()):
            m = re.match(r'^(\d+)\s+selected$', n['label'], re.I)
            if m:
                return int(m.group(1))
        return None

    def render_button(self, nodes=None):
        for n in (nodes if nodes is not None else self.nodes()):
            if n['role'] == 'AXButton' and re.match(r'^(render|align)\b', n['label'], re.I):
                return n
        return None

    def rendering(self, nodes=None):
        for n in (nodes if nodes is not None else self.nodes()):
            if n['role'] == 'AXStaticText' and re.match(r'^(rendering|aligning|processing)\b', n['label'], re.I):
                return True
        return False

    # -- the choices
    def choose(self, label_re, want, exact=True):
        """Open the menu next to the text `label_re` and pick `want`. True when it already shows `want` or was
        set; False when the menu or the option isn't there."""
        nodes = self.nodes()
        dd = self._after(nodes, label_re)
        if dd is None:
            return False

        def same(a):
            return a == want if exact else a.lower() == want.lower()
        if same(dd['label']):
            return True
        self.b.press(dd['el'])
        self.b.sleep(0.5)
        nodes = self.nodes()
        start = next((i for i, n in enumerate(nodes) if n['el'] is dd['el'] or n['el'] == dd['el']), 0)
        for n in nodes[start + 1:]:
            if n['role'] in ('AXButton', 'AXMenuItem', 'AXStaticText', 'AXRadioButton') and same(n['label']):
                self.b.press(n['el'])
                self.b.sleep(0.5)
                dd2 = self._after(self.nodes(), label_re)
                return dd2 is not None and same(dd2['label'])
        self.b.press(dd['el'])          # close the menu again
        self.b.sleep(0.3)
        return False

    def tick_sources(self, names, max_rounds=40):
        """Tick every enabled row whose label is one of `names`; untick enabled rows that are not (e.g. an empty
        default track). Walks down the list with Tab so rows below the fold appear. Returns the count ticked."""
        names = set(names)
        want = None
        seen_tail, stuck = None, 0
        for _ in range(max_rounds):
            rows = self.rows()
            for r in rows:
                if not r['enabled']:
                    continue
                should = r['label'] in names
                if should != r['checked']:
                    self.b.press(r['el'])
                    self.b.sleep(0.08)
            nodes = self.nodes()
            n_sel = self.selected_count(nodes)
            want = len(names)
            if n_sel is not None and n_sel >= want:
                return n_sel
            rows = self.rows(nodes)
            if not rows:
                return n_sel or 0
            tail = tuple(r['label'] for r in rows[-3:])
            if tail == seen_tail:
                stuck += 1
                if stuck >= 2:
                    return n_sel if n_sel is not None else sum(1 for r in rows if r['checked'])
            else:
                stuck = 0
            seen_tail = tail
            # walk focus down past the last row so the list scrolls and draws the next rows
            self.b.activate(self.handle)
            self.b.focus(rows[-1]['el'])
            for _k in range(3):
                self.b.key(self.pid, KEY_TAB)
            self.b.sleep(0.3)
        return self.selected_count() or 0

    def press_render(self, wait_s=8):
        nodes = self.nodes()
        btn = self.render_button(nodes)
        if btn is None:
            raise PanelError('the Render button is not in the Auto-Align Post panel')
        if not btn['enabled']:
            raise PanelError('Render is greyed out (no reference or no source tracks chosen)')
        self.b.press(btn['el'])
        t0 = time.time()
        while time.time() - t0 < wait_s:
            self.b.sleep(0.5)
            nodes = self.nodes()
            if self.rendering(nodes):
                return True
            b2 = self.render_button(nodes)
            if b2 is None or not b2['enabled']:
                return True
        return False


def drive(ref_name, source_names, backend=None, log=print, wait_s=40, time_mode=None, phase_mode=None):
    """Set up Auto-Align Post for our timeline and press Render. Returns (ok, message)."""
    b = backend or MacBackend()
    p = Panel(b, log)
    if not p.find(wait_s):
        return False, 'the Auto-Align Post window did not appear'
    if time_mode and not p.choose(r'^time$', time_mode, exact=False):
        log(f'  Auto-Align Post: could not set Time to {time_mode}; left as it was')
    if phase_mode and not p.choose(r'^phase$', phase_mode, exact=False):
        log(f'  Auto-Align Post: could not set Phase to {phase_mode}; left as it was')
    if not p.choose(r'^reference', ref_name):
        return False, f'could not choose "{ref_name}" as the Reference Track'
    n = p.tick_sources(source_names)
    if n < len(set(source_names)):
        return False, f'only {n} of {len(set(source_names))} source tracks could be ticked'
    try:
        if not p.press_render():
            return False, 'Render was pressed but Auto-Align did not start'
    except PanelError as e:
        return False, str(e)
    log(f'  Auto-Align Post: reference {ref_name}, {n} source tracks, Render pressed')
    return True, 'rendering'
