"""Pictures from camera RAW that only its maker can decode, through the maker's own free command-line tool when
it is installed on this Mac:
  RED (.R3D)            REDline, installed with REDCINE-X PRO
  ARRIRAW / ARRICORE    art-cmd, the ARRI Reference Tool CMD
Blackmagic RAW has no command-line tool; it is read through macOS (macframe) when a Blackmagic RAW media
extension is installed. Read-only: the clip is only opened; the still is written to a temp folder of our own."""
import glob
import os
import shutil
import subprocess
import tempfile

_found = {}


def _find(name, patterns):
    if name not in _found:
        hit = shutil.which(name)
        for pat in ([] if hit else patterns):
            got = sorted(glob.glob(os.path.expanduser(pat), recursive=True))
            if got:
                hit = got[-1]
                break
        _found[name] = hit
    return _found[name]


def redline():
    return _find('REDline', ['/Applications/REDCINE-X Professional/REDCINE-X PRO.app/Contents/MacOS/REDline',
                             '/Applications/REDCINE-X*/**/REDline', '~/Applications/REDCINE-X*/**/REDline'])


def art_cmd():
    return _find('art-cmd', ['/Applications/ARRI*/**/art-cmd', '/Applications/art-cmd*/**/art-cmd',
                             '~/Applications/**/art-cmd', '/usr/local/bin/art-cmd', '/opt/**/art-cmd'])


def is_red(path):
    return path.lower().endswith('.r3d')


def is_arri(path, codec=''):
    p = path.lower()
    return p.endswith(('.ari', '.arx')) or (p.endswith('.mxf') and 'arri' in (codec or '').lower())


def tool_for(path, codec=''):
    """(what's needed, is it installed) for a file only its maker's tool can show; None for anything else."""
    if is_red(path):
        return 'REDCINE-X PRO (free from RED)', bool(redline())
    if is_arri(path, codec):
        return 'ARRI Reference Tool CMD (free from ARRI)', bool(art_cmd())
    if path.lower().endswith('.braw'):
        return 'a Blackmagic RAW media extension for macOS', False
    return None


def _first(d, exts):
    for root, _dirs, files in os.walk(d):
        for fn in sorted(files):
            if fn.lower().endswith(exts):
                return os.path.join(root, fn)
    return None


def frame_file(path, frame, width=480, codec=''):
    """Path of a still (JPEG or TIFF) of frame number `frame`, in a temp folder the caller removes; or (None, None)."""
    d = tempfile.mkdtemp(prefix='ps_raw_')
    try:
        if is_red(path) and redline():
            # --format 3 = JPEG, --res 4 = quarter debayer, gamma/colour 1 = Rec.709 (REDline argument legend)
            subprocess.run([redline(), '--i', path, '--outDir', d, '--o', 'still', '--format', '3',
                            '--start', str(int(frame)), '--frameCount', '1', '--res', '4',
                            '--gammaCurve', '1', '--colorSpace', '1', '--resizeX', str(int(width))],
                           capture_output=True, timeout=90)
            return _first(d, ('.jpg', '.jpeg')), d
        if is_arri(path, codec) and art_cmd():
            subprocess.run([art_cmd(), 'process', '--input', path, '--start', str(int(frame)), '--duration', '1',
                            '--target-colorspace', 'Rec.709/D65/BT.1886', '--output-width', str(int(width)),
                            '--output', os.path.join(d, '%07d.tif')], capture_output=True, timeout=120)
            return _first(d, ('.tif', '.tiff')), d
    except (OSError, subprocess.SubprocessError):
        pass
    return None, d


def cleanup(d):
    shutil.rmtree(d, ignore_errors=True)
