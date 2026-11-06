"""Checks before a run, so problems show up before an hour of rendering instead of during it.

Each check: dict(id, label, status ('ok' | 'warn' | 'fail' | 'skip'), detail). Nothing here blocks a run.
Lessons from 2026-11-03: Auto-Align writes temp files to the internal disk (needs ~40 GB free), iLok has to be
running, Resolve needs external scripting set to Local, and Prep Station needs Accessibility to press Render.
"""
import glob
import os
import shutil
import subprocess
import sys

from . import media
from .brand import APP_NAME

GB = 1024 ** 3
AAP_TEMP_GB = 40
AAP_PLUGIN_DIRS = ['/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins',
                   os.path.expanduser('~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins')]
RESOLVE_APPS = ['/Applications/DaVinci Resolve/DaVinci Resolve.app', '/Applications/DaVinci Resolve Studio/DaVinci Resolve.app']


def _free(path):
    p = path
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    try:
        return shutil.disk_usage(p or '/').free
    except OSError:
        return None


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _procs():
    try:
        r = subprocess.run(['ps', '-axo', 'comm='], capture_output=True, text=True, timeout=5)
        return r.stdout.lower()
    except Exception:  # noqa: BLE001
        return ''


def estimate_bytes(project, settings):
    """Rough space the run will write to the output drive."""
    clips = [c for c in project.clips if c['include']]
    audio = sum(_size(c['path']) for c in clips if c['kind'] == 'audio')
    multi = sum(_size(c['path']) for c in clips if c['kind'] == 'audio' and c.get('channels', 1) > 1)
    need = audio                                    # aligned ISOs
    need += audio if settings.get('aap_mode') == 'resolve' else 0     # Auto-Align renders
    need += 2 * multi                               # mono splits + their aligned versions
    need += audio if settings.get('poly_aligned') else 0
    need += audio if settings.get('poly_unaligned') else 0
    if settings.get('synced_clips'):
        need += sum(_size(c['path']) for c in clips if c['kind'] == 'video') * 1.1
    return int(need * 1.1)


def run_checks(project, settings):
    out = []
    aap = settings.get('aap_mode') == 'resolve'

    def add(id_, label, status, detail=''):
        out.append(dict(id=id_, label=label, status=status, detail=detail))

    # ---- disk space on the output drive
    od = settings.get('out_dir') or ''
    need = estimate_bytes(project, settings)
    free = _free(od) if od else None
    if free is None:
        add('space', 'Space on the output drive', 'skip', 'Choose an output folder first.')
    elif free < need:
        add('space', 'Space on the output drive', 'fail', f'About {need / GB:.0f} GB needed, {free / GB:.0f} GB free.')
    elif free < need * 1.5:
        add('space', 'Space on the output drive', 'warn', f'About {need / GB:.0f} GB needed, {free / GB:.0f} GB free. Tight.')
    else:
        add('space', 'Space on the output drive', 'ok', f'About {need / GB:.0f} GB needed, {free / GB:.0f} GB free.')
    # ---- internal disk for Auto-Align's temp files
    if aap:
        f0 = _free(os.path.expanduser('~'))
        if f0 is None:
            add('tmp', 'Space on the Mac’s internal disk', 'skip')
        elif f0 < AAP_TEMP_GB * GB:
            add('tmp', 'Space on the Mac’s internal disk', 'fail' if f0 < 15 * GB else 'warn',
                f'{f0 / GB:.0f} GB free. Auto-Align writes temp files there; keep at least {AAP_TEMP_GB} GB free '
                f'or Resolve can stall with "No space left on device".')
        else:
            add('tmp', 'Space on the Mac’s internal disk', 'ok', f'{f0 / GB:.0f} GB free.')
    if sys.platform != 'darwin':
        return out
    procs = _procs()
    # ---- Resolve + Auto-Align + iLok + Accessibility
    if aap:
        app = next((a for a in RESOLVE_APPS if os.path.exists(a)), None)
        if not app:
            add('resolve', 'DaVinci Resolve Studio', 'fail', 'Not found in /Applications.')
        else:
            add('resolve', 'DaVinci Resolve Studio', 'ok', 'Installed. Preferences › System › General › External scripting '
                'using must be Local.' + (' Running.' if 'resolve' in procs else ' It opens by itself.'))
        found = []
        for d in AAP_PLUGIN_DIRS:
            found += [p for p in glob.glob(os.path.join(d, '*')) if 'align' in os.path.basename(p).lower()
                      or 'radix' in os.path.basename(p).lower()]
        add('aap', 'Auto-Align Post in Resolve', 'ok' if found else 'warn',
            'Workflow integration installed.' if found else 'Couldn’t find the Auto-Align Post workflow integration. '
            'If Workspace › Workflow Integrations doesn’t list it, reinstall Auto-Align Post.')
        ilok = any(k in procs for k in ('ilok', 'licensedaemon', 'ldsvc', 'paceap', 'eden'))
        add('ilok', 'iLok license service', 'ok' if ilok else 'warn',
            'Running.' if ilok else 'Not seen running. Open iLok License Manager; if it shows error 2000/2001, allow '
            'PACE in System Settings › General › Login Items & Extensions.')
        try:
            from .resolve_aap import ax_trusted
            trusted = ax_trusted(prompt=False)
        except Exception:  # noqa: BLE001
            trusted = False
        add('ax', 'Pressing Render for you', 'ok' if trusted else 'warn',
            'Allowed.' if trusted else f'Give {APP_NAME} Accessibility access (System Settings › Privacy & Security › '
            'Accessibility), or press Render yourself when asked.')
    # ---- media that might still be copying / offline
    missing = [c['name'] for c in project.clips if c['include'] and not os.path.exists(c.get('source_path') or c['path'])]
    if missing:
        add('media', 'All files reachable', 'fail', f"{len(missing)} file(s) are gone (drive unplugged?): {', '.join(missing[:3])}")
    unsup = [u for u in project.unsupported if os.path.splitext(u['path'])[1].lower() in media.RAW_TRY_EXT]
    if unsup:
        add('raw', 'RAW files without a proxy', 'warn', f'{len(unsup)} RAW file(s) can’t be synced: see the list above the timeline.')
    return out


def notify(title, message, sound='Glass'):
    if sys.platform != 'darwin':
        return
    script = ('on run argv\n display notification (item 2 of argv) with title (item 1 of argv) sound name "' + sound +
              '"\nend run')
    try:
        subprocess.run(['osascript', '-', title, message], input=script, text=True, capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass
