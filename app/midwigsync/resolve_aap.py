"""Hand-off to Sound Radix Auto-Align Post inside DaVinci Resolve Studio.

1. plan_timeline(): reference clips (FX6 CH1 WAVs) on A1, every ISO cut into the pieces that sit under a
   reference clip on A2..An, all at their timecode.  (Same layout that worked by hand on 2026-11-03.)
2. ResolveBridge.build(): creates that project + timeline through Resolve's scripting API.
3. open_aap_and_render(): opens Workspace > Workflow Integrations > Auto-Align Post and presses Render
   through macOS Accessibility. If that fails the app asks the user for that one click.
4. RenderWatcher: waits for Auto-Align's renders (<source>_si0ch0off<N>-AtAlgn-<hash>.wav).
"""
import glob
import os
import re
import subprocess
import sys
import time
from fractions import Fraction

from . import tc as TC

RENDER_RE = re.compile(r'^(?P<src>.+?)_si(?P<si>\d+)ch(?P<ch>\d+)off(?P<off>-?\d+)-AtAlgn', re.I)

RESOLVE_LIB_CANDIDATES = [
    '/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so',
    '/Applications/DaVinci Resolve Studio/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so',
    '/opt/resolve/libs/Fusion/fusionscript.so',
]


# ------------------------------------------------------------------ planning (pure python, testable)
def plan_timeline(refs, sources, fps, min_piece_s=2.0):
    """refs: [dict(path, start, length, name)]; sources: [dict(path, start, length, roll, name, channels)]
    (already ordered by track). Returns the timeline plan."""
    spf = TC.samples_per_frame(fps)
    rolls = []
    for s in sources:
        if s['roll'] not in rolls:
            rolls.append(s['roll'])
    # every mono track that is a reference somewhere still needs its own track for the other stretches
    tracks = [dict(index=1, name='REF', kind='ref', channels=1)]
    for i, r in enumerate(rolls):
        ch = max(s['channels'] for s in sources if s['roll'] == r)
        tracks.append(dict(index=i + 2, name=r, kind='source', channels=min(ch, 2)))
    track_of = {t['name']: t['index'] for t in tracks}
    if not refs:
        return dict(tracks=tracks, items=[], expected={}, timeline_start_frame=0)
    first = min(r['start'] for r in refs)
    start_frame = (TC.samples_to_frames(first, fps, 'floor') // (TC.nominal(fps) * 3600)) * TC.nominal(fps) * 3600
    items, expected = [], {}

    def fr(x, mode):
        return TC.samples_to_frames(x, fps, mode)

    for r in sorted(refs, key=lambda r: r['start']):
        r_in, r_out = fr(r['start'], 'ceil'), fr(r['start'] + r['length'], 'floor')
        f0 = Fraction(r.get('file_start', r['start'])) / spf      # a reference piece can start inside its file
        items.append(dict(track=1, path=r['path'], name=r['name'], src_in=r_in - f0,
                          src_out=r_out - f0, record=r_in, kind='ref', abs_in=r_in, abs_out=r_out))
        for s in sources:
            if s['path'] == r['path']:
                continue                         # the reference mic itself isn't aligned to itself
            a = max(r['start'], s['start'])
            b = min(r['start'] + r['length'], s['start'] + s['length'])
            if b - a < min_piece_s * TC.SR:
                continue
            fa, fb = fr(a, 'ceil'), fr(b, 'floor')
            if fb - fa < 2:
                continue
            s_in = Fraction(fa) - Fraction(s['start']) / spf
            items.append(dict(track=track_of[s['roll']], path=s['path'], name=s['name'], src_in=s_in,
                              src_out=s_in + (fb - fa), record=fa, kind='source', abs_in=fa, abs_out=fb))
            stem = os.path.splitext(s['name'])[0]
            expected[stem] = expected.get(stem, 0) + 1
    return dict(tracks=tracks, items=items, expected=expected, timeline_start_frame=start_frame,
                fps=TC.rate_label(fps))


# ------------------------------------------------------------------ Resolve scripting
class ResolveError(RuntimeError):
    pass


def load_resolve_module():
    lib = os.environ.get('RESOLVE_SCRIPT_LIB')
    cands = [lib] if lib else RESOLVE_LIB_CANDIDATES
    for p in cands:
        if p and os.path.exists(p):
            import importlib.machinery
            import importlib.util
            loader = importlib.machinery.ExtensionFileLoader('fusionscript', p)
            spec = importlib.util.spec_from_loader('fusionscript', loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            return mod
    raise ResolveError('DaVinci Resolve Studio is not installed in /Applications (fusionscript.so not found).')


class ResolveBridge:
    def __init__(self, log=print):
        self.log = log
        self.resolve = None

    def connect(self, launch=True, wait_s=120):
        bmd = load_resolve_module()
        r = bmd.scriptapp('Resolve')
        if r is None and launch and sys.platform == 'darwin':
            self.log('Opening DaVinci Resolve...')
            subprocess.run(['open', '-a', 'DaVinci Resolve'], check=False)
            t0 = time.time()
            while r is None and time.time() - t0 < wait_s:
                time.sleep(3)
                r = bmd.scriptapp('Resolve')
        if r is None:
            raise ResolveError('Could not talk to Resolve. In Resolve: Preferences > System > General > '
                               '"External scripting using" = Local, then try again.')
        self.resolve = r
        return r

    def build(self, plan, project_name, render_dir):
        r = self.resolve or self.connect()
        pm = r.GetProjectManager()
        name = project_name
        proj = pm.CreateProject(name)
        k = 2
        while proj is None and k < 50:
            name = f'{project_name}_{k}'
            proj = pm.CreateProject(name)
            k += 1
        if proj is None:
            raise ResolveError('Resolve would not create a new project.')
        fps = plan['fps']
        for key in ('timelineFrameRate', 'timelinePlaybackFrameRate'):
            proj.SetSetting(key, fps)
        mp = proj.GetMediaPool()
        root = mp.GetRootFolder()
        bins = {}
        for label in ('REF', 'ISO'):
            bins[label] = mp.AddSubFolder(root, label) or root
        paths_ref = sorted({i['path'] for i in plan['items'] if i['kind'] == 'ref'})
        paths_src = sorted({i['path'] for i in plan['items'] if i['kind'] == 'source'})
        mpi = {}
        for label, paths in (('REF', paths_ref), ('ISO', paths_src)):
            mp.SetCurrentFolder(bins[label])
            got = mp.ImportMedia(paths) or []
            for it in got:
                fp = it.GetClipProperty('File Path')
                mpi[os.path.realpath(fp)] = it
        missing = [p for p in paths_ref + paths_src if os.path.realpath(p) not in mpi]
        if missing:
            raise ResolveError(f'Resolve did not import {len(missing)} file(s), e.g. {missing[0]}')
        mp.SetCurrentFolder(root)
        tl = mp.CreateEmptyTimeline(f'{name}_AAP')
        if tl is None:
            raise ResolveError('Could not create the timeline.')
        proj.SetCurrentTimeline(tl)
        tl.SetStartTimecode(TC.frames_to_tc(plan['timeline_start_frame'], TC.parse_rate(fps)))
        # tracks: replace the default audio track(s) with mono/stereo tracks we name
        for _ in range(int(tl.GetTrackCount('audio') or 0)):
            if not tl.DeleteTrack('audio', 1):
                break
        for t in plan['tracks']:
            tl.AddTrack('audio', 'mono' if t['channels'] == 1 else 'stereo')
        n_audio = int(tl.GetTrackCount('audio') or 0)
        offset = n_audio - len(plan['tracks'])   # if a default track survived, ours start after it
        for t in plan['tracks']:
            tl.SetTrackName('audio', t['index'] + offset, t['name'])
        infos = []
        for it in plan['items']:
            infos.append({'mediaPoolItem': mpi[os.path.realpath(it['path'])],
                          'startFrame': float(it['src_in']), 'endFrame': float(it['src_out']) - 1,
                          'recordFrame': int(it['record']), 'trackIndex': it['track'] + offset, 'mediaType': 2})
        placed = []
        for k in range(0, len(infos), 50):
            placed += mp.AppendToTimeline(infos[k:k + 50]) or []
        if len(placed) < len(infos):
            self.log(f'  Resolve placed {len(placed)} of {len(infos)} clips')
        # sanity check positions
        bad = 0
        for info, ti in zip(infos, placed):
            try:
                if ti and abs(int(ti.GetStart()) - info['recordFrame']) > 1:
                    bad += 1
            except Exception:  # noqa: BLE001
                pass
        if bad:
            self.log(f'  warning: {bad} clips landed away from their timecode')
        pm.SaveProject()
        try:
            r.OpenPage('fairlight')
        except Exception:  # noqa: BLE001
            pass
        return dict(project=name, timeline=f'{name}_AAP', placed=len(placed), wanted=len(infos),
                    ref_track=1 + offset, source_tracks=[t['index'] + offset for t in plan['tracks'][1:]])


# ------------------------------------------------------------------ Accessibility: open AAP + press Render
def _ax():
    import ApplicationServices as AS  # pyobjc-framework-ApplicationServices
    return AS


def ax_trusted(prompt=True):
    if sys.platform != 'darwin':
        return False
    try:
        AS = _ax()
        opts = {AS.kAXTrustedCheckOptionPrompt: bool(prompt)}
        return bool(AS.AXIsProcessTrustedWithOptions(opts))
    except Exception:  # noqa: BLE001
        return False


def _attr(AS, el, name):
    try:
        err, val = AS.AXUIElementCopyAttributeValue(el, name, None)
        return val if err == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _walk(AS, el, depth=0, max_depth=40):
    yield el, depth
    if depth >= max_depth:
        return
    for ch in (_attr(AS, el, 'AXChildren') or []):
        yield from _walk(AS, ch, depth + 1, max_depth)


def _label(AS, el):
    parts = [_attr(AS, el, k) for k in ('AXTitle', 'AXDescription', 'AXValue', 'AXHelp')]
    return ' '.join(str(p) for p in parts if p not in (None, '') and not hasattr(p, 'pointerValue'))[:200]


def _running_apps():
    from AppKit import NSWorkspace
    return list(NSWorkspace.sharedWorkspace().runningApplications())


def _app_el(AS, match):
    for a in _running_apps():
        bid = str(a.bundleIdentifier() or '')
        nm = str(a.localizedName() or '')
        if match(bid, nm):
            el = AS.AXUIElementCreateApplication(a.processIdentifier())
            try:
                AS.AXUIElementSetAttributeValue(el, 'AXManualAccessibility', True)  # Electron: build the AX tree
            except Exception:  # noqa: BLE001
                pass
            return el, a
    return None, None


def _is_aap(bid, nm):
    return 'electron' in bid.lower() or 'auto-align' in nm.lower() or 'autoalign' in bid.lower().replace('-', '')


def open_aap_panel(log=print):
    """Workspace > Workflow Integrations > Auto-Align Post, via the Resolve menu bar."""
    AS = _ax()
    app, ra = _app_el(AS, lambda b, n: 'davinci' in b.lower() or 'davinci resolve' in n.lower())
    if app is None:
        raise ResolveError('Resolve is not running.')
    ra.activateWithOptions_(0)
    time.sleep(0.5)
    bar = _attr(AS, app, 'AXMenuBar')
    path = ['Workspace', 'Workflow Integrations', 'Auto-Align Post']
    el = bar
    for i, name in enumerate(path):
        found = None
        for cand, _ in _walk(AS, el, 0, 3):
            if str(_attr(AS, cand, 'AXTitle') or '').strip().lower().startswith(name.lower()):
                found = cand
                break
        if found is None:
            # fall back to AppleScript for the menu click
            script = ('tell application "System Events" to tell process "DaVinci Resolve" to click menu item '
                      '"Auto-Align Post" of menu 1 of menu item "Workflow Integrations" of menu 1 of '
                      'menu bar item "Workspace" of menu bar 1')
            r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
            if r.returncode != 0:
                raise ResolveError(f'Could not open the Auto-Align Post panel ({name} not found).')
            return True
        AS.AXUIElementPerformAction(found, 'AXPress')
        time.sleep(0.6)
        el = found
    return True


def dump_aap_tree(out_path):
    AS = _ax()
    app, _ = _app_el(AS, _is_aap)
    if app is None:
        return False
    time.sleep(1.0)
    with open(out_path, 'w') as f:
        for el, d in _walk(AS, app, 0, 60):
            f.write('  ' * d + f"{_attr(AS, el, 'AXRole')} | {_label(AS, el)}\n")
    return True


def press_render(log=print, wait_s=40):
    """Find the Auto-Align Post panel and press its Render button."""
    AS = _ax()
    t0 = time.time()
    while time.time() - t0 < wait_s:
        app, _ = _app_el(AS, _is_aap)
        if app is not None:
            for el, _d in _walk(AS, app, 0, 60):
                if _attr(AS, el, 'AXRole') != 'AXButton':
                    continue
                lab = _label(AS, el).strip().lower()
                if re.match(r'^(render|align|process)\b', lab):
                    if AS.AXUIElementPerformAction(el, 'AXPress') == 0:
                        log(f'  pressed "{lab}" in Auto-Align Post')
                        return True
        time.sleep(2)
    return False


# ------------------------------------------------------------------ render pickup
class RenderWatcher:
    """dirs: list of paths (searched recursively) or (path, recursive) tuples."""

    def __init__(self, expected, dirs, since):
        self.expected = dict(expected)        # source stem -> pieces
        self.dirs = [(d, True) if isinstance(d, str) else d for d in dirs if d]
        self.since = since
        self.sizes = {}

    def scan(self):
        found = {}
        for d, rec in self.dirs:
            if not os.path.isdir(d):
                continue
            pats = [os.path.join(d, '**', '*AtAlgn*.[wW][aA][vV]')] if rec else [os.path.join(d, '*AtAlgn*.[wW][aA][vV]'),
                                                                               os.path.join(d, '*', '*AtAlgn*.[wW][aA][vV]')]
            for pat in pats:
                for p in glob.glob(pat, recursive=rec):
                    m = RENDER_RE.match(os.path.basename(p))
                    if not m or m['src'] not in self.expected:
                        continue
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    if st.st_mtime < self.since - 5:
                        continue
                    found[os.path.realpath(p)] = (m['src'], st.st_size)
        return found

    @staticmethod
    def _complete(p, size):
        """Header agrees with the bytes on disk (or the header can't tell us)."""
        try:
            from .bwf import WavInfo
            w = WavInfo(p)
        except Exception:  # noqa: BLE001
            return False
        dec = w.declared_data_size
        if dec in (None, 0, 0xFFFFFFFF):
            return True
        return w.data_off + dec <= size + 1

    def status(self):
        found = self.scan()
        stable = {}
        now = time.time()
        for p, (src, size) in found.items():
            prev = self.sizes.get(p)
            self.sizes[p] = (size, prev[1] if prev and prev[0] == size else now)
            if now - self.sizes[p][1] > 6 and size > 1024 and self._complete(p, size):
                stable[p] = src
        per = {}
        for p, src in stable.items():
            per[src] = per.get(src, 0) + 1
        done = sum(min(per.get(s, 0), n) for s, n in self.expected.items())
        want = sum(self.expected.values())
        return dict(files=sorted(stable), done=done, want=want, complete=done >= want and want > 0,
                    seen=len(found))
