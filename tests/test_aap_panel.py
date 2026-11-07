"""Auto-Align Post panel driver against a fake panel that behaves like the real one (2026-11-06 night):
own Electron app, Reference defaults to "Audio 1", no sources ticked, Render greyed out until both are chosen,
source list shows 6 rows and Tab walks focus down to draw the next ones. Another Electron app runs alongside."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync import aap_panel as AP  # noqa: E402


class El:
    def __init__(self, role, title='', value=None, enabled=True, children=None, on_press=None):
        self.role, self.title, self.value, self.enabled = role, title, value, enabled
        self.kids = children or []
        self.on_press = on_press

    def __repr__(self):
        return f'<{self.role} {self.title!r}>'


class FakePanel:
    VISIBLE = 6

    def __init__(self, tracks, empty_default=True, virtual=True, ref_default=None):
        self.tracks = (['Audio 1'] if empty_default else []) + tracks
        self.ref = ref_default or self.tracks[0]
        self.checked = {i: False for i in range(len(self.tracks))}
        self.menu_open = False
        self.top = 0
        self.focus = None
        self.virtual = virtual
        self.rendering = False
        self.presses = []
        self.time, self.phase = 'DYNAMIC', 'SPECTRAL'

    # what the window holds right now
    def window(self):
        kids = [El('AXStaticText', value='If you are using Audio Layers, please flatten them'),
                El('AXStaticText', value='Time'), El('AXButton', self.time),
                El('AXStaticText', value='Phase'), El('AXButton', self.phase),
                El('AXStaticText', value='Reference Track'),
                El('AXButton', self.ref, on_press=self._toggle_menu)]
        if self.menu_open:
            for t in self.tracks:
                kids.append(El('AXButton', t, on_press=lambda t=t: self._pick(t)))
        kids.append(El('AXStaticText', value='Source Tracks'))
        n = sum(self.checked.values())
        if n:
            kids.append(El('AXStaticText', value=f'{n} selected'))
        rng = range(len(self.tracks)) if not self.virtual else range(self.top, min(len(self.tracks), self.top + self.VISIBLE))
        for i in rng:
            t = self.tracks[i]
            cb = El('AXCheckBox', value=1 if self.checked[i] else 0, enabled=(t != self.ref),
                    on_press=lambda i=i: self._tick(i))
            cb.idx = i
            kids += [cb, El('AXImage'), El('AXStaticText', value=t)]
        can = any(self.checked.values()) and not self.rendering
        if self.rendering:
            kids.append(El('AXStaticText', value="Rendering... Please don't edit the timeline"))
        kids.append(El('AXButton', 'Render' if not self.rendering else '', enabled=can, on_press=self._render))
        return El('AXWindow', 'Auto-Align Post', children=[El('AXWebArea', children=kids)])

    def _toggle_menu(self):
        self.menu_open = not self.menu_open

    def _pick(self, t):
        self.ref, self.menu_open = t, False
        i = self.tracks.index(t)
        self.checked[i] = False

    def _tick(self, i):
        if self.tracks[i] == self.ref:
            return
        self.checked[i] = not self.checked[i]

    def _render(self):
        if any(self.checked.values()):
            self.rendering = True

    def tab(self):
        if self.focus is None:
            return
        nxt = self.focus + 1
        if nxt < len(self.tracks):
            self.focus = nxt
            if nxt >= self.top + self.VISIBLE:
                self.top = nxt - self.VISIBLE + 1
        else:
            self.focus = None       # on to the Handles field


class FakeBackend:
    def __init__(self, panel):
        self.p = panel
        self.other = El('AXApplication', children=[El('AXWindow', 'Some other Electron app')])

    def apps(self):
        return [('other', 'com.github.Electron', 'Electron', 11), ('aap', 'com.github.Electron', 'Electron', 22)]

    def app_element(self, handle):
        if handle == 'other':
            return self.other
        return El('AXApplication', children=[self.p.window()])

    def attr(self, el, name):
        if name == 'AXWindows':
            return [k for k in el.kids if k.role == 'AXWindow']
        if name == 'AXRole':
            return el.role
        if name == 'AXTitle':
            return el.title
        if name == 'AXDescription':
            return ''
        if name == 'AXValue':
            return el.value
        if name == 'AXEnabled':
            return el.enabled
        return None

    def children(self, el):
        if el.role == 'AXWindow' and el.title == 'Auto-Align Post':
            return self.p.window().kids      # always the live state
        return el.kids

    def press(self, el):
        self.p.presses.append(el.title or el.role)
        if not el.enabled:
            return False
        if el.on_press:
            el.on_press()
        return True

    def focus(self, el):
        if getattr(el, 'idx', None) is not None:
            self.p.focus = el.idx
            return True
        return False

    def activate(self, handle):
        pass

    def key(self, pid, keycode):
        assert pid == 22, 'keys must go to the Auto-Align Post process, not another Electron app'
        if keycode == AP.KEY_TAB:
            self.p.tab()
        return True

    def sleep(self, s):
        pass


TRACKS = ['REF', '11-6-UDP BOOM', '11-6-UDP LAV', '11-6-UDP BOOM -6db', '11-6-UDP LAV -6db', '11-6-UDP BOOM1',
          '11-6-UDP BOOM2', '11-6-UDP LAV1', '11-6-UDP LAV2', '11-6-UDP Boom', '11-6-UDP Lav1 Alex',
          '11-6-UDP Lav2 Ellie', '11-6-UDP Lav3 Emma', '11-6-UDP Lav4 Bailey', '11-6-UDP Lav 5 Clara']
SOURCES = TRACKS[1:]


def check(name, panel, expect_ok=True):
    b = FakeBackend(panel)
    ok, msg = AP.drive('REF', SOURCES, backend=b, log=lambda m: None, wait_s=0, time_mode='Dynamic', phase_mode='Spectral')
    assert ok == expect_ok, (name, ok, msg)
    if expect_ok:
        assert panel.rendering, name
        assert panel.ref == 'REF', name
        got = sorted(t for i, t in enumerate(panel.tracks) if panel.checked[i])
        assert got == sorted(SOURCES), (name, got)
        if 'Audio 1' in panel.tracks:
            assert not panel.checked[panel.tracks.index('Audio 1')], name
    print(f'  {name}: ok={ok} ({msg})')
    return ok, msg


def main():
    print('Auto-Align Post panel driver')
    # tonight's case: empty Audio 1 above REF, list drawn 6 rows at a time
    check('empty Audio 1 + virtual list', FakePanel(TRACKS, empty_default=True, virtual=True))
    # 1.6 timelines: REF is A1
    check('REF on A1, virtual list', FakePanel(TRACKS, empty_default=False, virtual=True))
    check('REF on A1, whole list in the tree', FakePanel(TRACKS, empty_default=False, virtual=False))
    # left over from a run before: reference already REF, some sources ticked, a stray tick on Audio 1
    p = FakePanel(TRACKS, empty_default=True, virtual=True, ref_default='REF')
    p.checked[0] = True
    p.checked[3] = True
    check('settings left from before', p)
    # names that don't match: must not press Render, must say why
    p = FakePanel(['REF', 'something else'], empty_default=False)
    ok, msg = AP.drive('REF', SOURCES, backend=FakeBackend(p), log=lambda m: None, wait_s=0)
    assert not ok and not p.rendering and 'source tracks' in msg, msg
    print(f'  wrong track names: refused ({msg})')
    # no Auto-Align window at all
    b = FakeBackend(FakePanel(TRACKS))
    b.apps = lambda: [('other', 'com.github.Electron', 'Electron', 11)]
    ok, msg = AP.drive('REF', SOURCES, backend=b, log=lambda m: None, wait_s=0)
    assert not ok and 'did not appear' in msg, msg
    print(f'  panel not open: {msg}')
    print('AAP PANEL OK')


if __name__ == '__main__':
    main()
