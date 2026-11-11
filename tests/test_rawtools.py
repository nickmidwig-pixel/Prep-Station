"""RAW pictures through the maker's tool: a stand-in REDline / art-cmd on PATH is called with the documented
arguments, writes its still into OUR temp folder (never beside the clip), and the folder is removed afterwards."""
import os, stat, subprocess, sys, tempfile, shutil
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync import rawtools, convert

D = tempfile.mkdtemp(prefix='rawtools_')
try:
    card = os.path.join(D, 'card'); os.makedirs(card)
    r3d = os.path.join(card, 'A001_C001_0101AB_001.R3D'); open(r3d, 'wb').write(b'RED2' * 64)
    mxf = os.path.join(card, 'C_0001C001_250807_130114_c1I82.mxf'); open(mxf, 'wb').write(b'\x06\x0e\x2b\x34' * 64)
    jpg = os.path.join(D, 'src.jpg')
    subprocess.run([convert.ffmpeg_exe(), '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=320x180', '-frames:v', '1', jpg], check=True)
    tif = os.path.join(D, 'src.tif')
    subprocess.run([convert.ffmpeg_exe(), '-y', '-v', 'error', '-i', jpg, tif], check=True)
    bind = os.path.join(D, 'bin'); os.makedirs(bind)
    log = os.path.join(D, 'calls.txt')
    for name, body in (('REDline', f'''#!/bin/sh
echo "REDline $@" >> "{log}"
while [ $# -gt 0 ]; do case "$1" in --outDir) out="$2";; --o) nm="$2";; esac; shift; done
cp "{jpg}" "$out/$nm.000000.jpg"
'''), ('art-cmd', f'''#!/bin/sh
echo "art-cmd $@" >> "{log}"
while [ $# -gt 0 ]; do case "$1" in --output) out="$2";; esac; shift; done
cp "{tif}" "$(dirname "$out")/0000000.tif"
''')):
        fp = os.path.join(bind, name); open(fp, 'w').write(body); os.chmod(fp, os.stat(fp).st_mode | stat.S_IEXEC)
    os.environ['PATH'] = bind + os.pathsep + os.environ['PATH']
    before = sorted(os.listdir(card))
    for src, ext in ((r3d, '.jpg'), (mxf, '.tif')):
        fp, d = rawtools.frame_file(src, 48, 480, 'arriraw' if src.endswith('.mxf') else '')
        assert fp and fp.endswith(ext) and os.path.getsize(fp) > 100, (src, fp)
        assert not os.path.commonpath([fp, card]) == card, 'still written beside the clip'
        rawtools.cleanup(d)
        assert not os.path.exists(d)
    calls = open(log).read()
    assert '--format 3' in calls and '--start 48' in calls and '--frameCount 1' in calls, calls
    assert 'process --input' in calls and '--duration 1' in calls and 'Rec.709/D65/BT.1886' in calls, calls
    assert sorted(os.listdir(card)) == before, 'something was added beside the originals'
    assert rawtools.tool_for('/x/a.braw')[1] is False and rawtools.tool_for('/x/a.mov') is None
    print('RAW TOOLS GOOD')
finally:
    shutil.rmtree(D, ignore_errors=True)
