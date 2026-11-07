"""Original media is read-only to Prep Station. Enforced, not just intended.

Every file the app reads as media (camera clips, sound files, sidecars, P2 audio, proxies) and every folder you
drop are registered here. A Python audit hook then sits under every file operation the app makes (opening for
writing, renaming, deleting, truncating, changing dates, permissions or extended attributes, hard links, and the
output of every ffmpeg command) and refuses any that would touch:
  - a registered original, by path or by the file itself (so a link or alias to it is caught too), or
  - anything inside a dropped folder, except inside the job's own output folder.
A refused operation raises PermissionError and is recorded, so the job stops instead of changing anything.
There is no switch in the app to turn this off.
"""
import ctypes
import ctypes.util
import os
import shutil
import sys
import threading

from .brand import CACHE, LOGS, SUPPORT

_files = set()      # realpaths of originals
_inodes = set()     # (st_dev, st_ino) of originals
_roots = set()      # dropped folders (realpaths)
_outputs = set()    # output folders the app may write in (realpaths)
_own = {os.path.realpath(p) for p in (CACHE, LOGS, SUPPORT)}
blocked = []        # (action, path) for every refused operation
_lock = threading.Lock()
_busy = threading.local()
_installed = False

# card structures: never write inside these, whatever the output folder says
CARD_DIRS = {'CONTENTS', 'PRIVATE', 'DCIM', 'XDROOT', 'M4ROOT', 'CLIPS001', 'AVCHD', 'BPAV', 'CANONMSC',
             'BRAW', 'MISC', 'CLIP', 'XDCAM'}


def _rp(p):
    if isinstance(p, bytes):
        p = os.fsdecode(p)
    return os.path.realpath(os.path.abspath(os.fspath(p)))


def _under(p, root):
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def protect_file(path):
    try:
        rp = _rp(path)
        if any(_under(rp, o) for o in _own):
            return            # the app's own cache (converted copies) is not an original
        st = os.stat(rp)
    except (OSError, TypeError, ValueError):
        return
    with _lock:
        _files.add(rp)
        _inodes.add((st.st_dev, st.st_ino))


def protect_root(path):
    try:
        rp = _rp(path)
    except (OSError, TypeError, ValueError):
        return
    if os.path.isdir(rp):
        with _lock:
            _roots.add(rp)
    else:
        protect_file(rp)


def protect_clip(c):
    """Register every file path held anywhere in a clip record (path, source, audio streams, proxy, sidecars)."""
    def walk(v):
        if isinstance(v, str):
            if os.path.isabs(v) and os.path.isfile(v):
                protect_file(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
    walk(c)


def allow_output(path):
    """The job's own output folder: new files may be written here (never over an original)."""
    rp = _rp(path)
    with _lock:
        _outputs.add(rp)
    return rp


def output_problem(out_dir):
    """Why an output folder isn't allowed, or None. It may not sit inside a camera card or a folder that holds
    original media files directly, and may not be an original file."""
    if not out_dir:
        return 'Choose an output folder.'
    rp = _rp(out_dir)
    parts = rp.split(os.sep)
    for name in parts:
        if name.upper() in CARD_DIRS:
            return f'The output folder is inside a camera card ({name}). Pick a folder outside the card.'
    with _lock:
        files = list(_files)
    for f in files:
        d = os.path.dirname(f)
        if _under(rp, d):
            return (f'The output folder is inside {os.path.basename(d)}, which holds original media. '
                    'Pick a folder beside it instead.')
    if rp in files:
        return 'The output folder points at an original file.'
    return None


def _refuse(action, path):
    blocked.append((action, str(path)))
    raise PermissionError(f'Prep Station never changes original media: refused to {action} {path}')


def check(path, action='write'):
    """Raise PermissionError if this write/delete/rename would touch an original."""
    if path is None or isinstance(path, int):
        return
    try:
        rp = _rp(path)
    except (TypeError, ValueError):
        return
    if rp in _files:
        _refuse(action, rp)
    try:
        st = os.stat(rp)
    except OSError:
        st = None
    if st is not None and (st.st_dev, st.st_ino) in _inodes:
        _refuse(action, rp)        # the same file under another name (a hard link made earlier)
    if any(_under(rp, r) for r in _roots) and not any(_under(rp, o) for o in _outputs):
        if action == 'create' and any(_under(o, rp) for o in _outputs) and not os.path.exists(rp):
            return            # a missing parent folder of the output folder
        _refuse(action, rp)


def _is_original(path):
    try:
        rp = _rp(path)
        if rp in _files:
            return True
        st = os.stat(rp)
        return (st.st_dev, st.st_ino) in _inodes
    except (OSError, TypeError, ValueError):
        return False


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_ONE = {'os.remove': 'delete', 'os.rmdir': 'delete', 'shutil.rmtree': 'delete', 'os.truncate': 'truncate',
        'os.utime': 'change the dates of', 'os.chmod': 'change permissions of', 'os.chown': 'change the owner of',
        'os.chflags': 'change flags of', 'os.lchflags': 'change flags of', 'os.lchmod': 'change permissions of',
        'os.setxattr': 'change attributes of', 'os.removexattr': 'change attributes of', 'os.mkdir': 'create',
        'shutil.chown': 'change the owner of'}


def _hook(event, args):
    if not (_files or _roots):
        return
    if getattr(_busy, 'on', False):
        return
    if event == 'open':
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        if isinstance(mode, str):
            w = any(ch in mode for ch in 'wax+')
        else:
            w = isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
        if not w or isinstance(path, int) or path is None:
            return
        _busy.on = True
        try:
            check(path)
        finally:
            _busy.on = False
        return
    if event in _ONE:
        _busy.on = True
        try:
            check(args[0], _ONE[event])
        finally:
            _busy.on = False
        return
    if event in ('os.rename', 'shutil.move'):
        _busy.on = True
        try:
            check(args[0], 'move or rename')
            check(args[1], 'replace')
        finally:
            _busy.on = False
        return
    if event in ('os.link', 'os.symlink', 'shutil.copyfile', 'shutil.copytree'):
        _busy.on = True
        try:
            if event == 'os.link' and _is_original(args[0]):
                _refuse('hard-link', args[0])     # a hard link would share (and re-date) the original
            check(args[1], 'write')
        finally:
            _busy.on = False
        return
    if event == 'subprocess.Popen':
        argv = args[1]
        if isinstance(argv, (str, bytes)) or not argv:
            return
        argv = [os.fsdecode(a) if isinstance(a, bytes) else str(a) for a in argv]
        exe = os.path.basename(argv[0]).lower()
        if 'ffmpeg' in exe or exe in ('cp', 'ditto', 'mv', 'rm', 'touch', 'xattr', 'chmod', 'sox'):
            _busy.on = True
            try:
                for i, a in enumerate(argv[1:], 1):
                    if not os.path.isabs(a):
                        continue
                    if 'ffmpeg' in exe and argv[i - 1] in ('-i', '-f'):
                        continue                # inputs are only read
                    if exe in ('cp', 'ditto') and i < len(argv) - 1:
                        continue                # copy sources are only read
                    check(a, f'let {exe} write')
            finally:
                _busy.on = False


def install():
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True


def clone_or_copy(src, dst):
    """A separate copy of src at dst. On APFS this is a clone (instant, no extra space, its own file: writing
    to it can never reach the original). Never a hard link."""
    check(dst)
    if sys.platform == 'darwin':
        try:
            lib = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
            if lib.clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0:
                return dst
        except (OSError, AttributeError):
            pass
    shutil.copyfile(src, dst)
    return dst


def summary():
    with _lock:
        return dict(originals=len(_files), folders=len(_roots), blocked=list(blocked))
