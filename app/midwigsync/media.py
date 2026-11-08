"""Probe camera and audio files for timecode / format, and stream their audio."""
import os
import re
import struct
import glob
import datetime as dt
import xml.etree.ElementTree as ET
from fractions import Fraction
import numpy as np

from . import tc as TC
from .bwf import WavInfo

VIDEO_EXT = {'.mxf', '.mp4', '.mov', '.m4v', '.mts', '.m2ts', '.avi', '.mkv', '.mpg', '.mpeg', '.3gp',
             '.crm',     # Canon Cinema RAW Light: picture is unreadable here, audio + timecode are fine
             '.braw',    # Blackmagic RAW: same
             '.insv'}    # Insta360
RAW_TRY_EXT = {'.r3d', '.nev', '.zraw'}           # RED, Nikon N-RAW, Z CAM: try; else use a proxy's audio
AUDIO_EXT = {'.wav', '.bwf', '.w64', '.rf64'}
AUDIO_OTHER_EXT = {'.aif', '.aiff', '.m4a', '.mp3', '.flac', '.caf', '.ogg', '.opus', '.aac', '.wma'}
UNSUPPORTED_EXT = {'.ari', '.arx', '.cine', '.dng', '.zax', '.r3d', '.nev', '.zraw'}
WHY = {
    '.zax': 'Zaxcom MARF file. Convert it to WAV with ZaxConvert first, then drop the WAVs in.',
    '.r3d': 'RED RAW: the audio can\'t be read here. Drop the clip\'s proxy (MOV/MP4) next to it, or export the '
            'audio from Resolve, and it gets used instead.',
    '.nev': 'Nikon N-RAW: the audio can\'t be read here. Keep the camera\'s MP4 proxy next to it and '
            'it gets used instead.',
    '.zraw': 'Z CAM ZRAW: the audio can\'t be read here. Record a ProRes/H.265 proxy or export the audio.',
    '.ari': 'ARRIRAW frames (no audio in the files).',
    '.arx': 'ARRIRAW frames (no audio in the files).',
    '.cine': 'Phantom .cine (no audio).',
    '.dng': 'CinemaDNG frames. Drop the clip\'s WAV instead.',
}

CAMERA_NAMES = {'ILME-FX6': 'FX6', 'ILME-FX3': 'FX3', 'ILME-FX30': 'FX30', 'ILME-FX9': 'FX9', 'PXW-FX9': 'FX9',
                'ILME-FX2': 'FX2', 'ILME-FR7': 'FR7', 'MPC-2610': 'VENICE', 'MPC-3610': 'VENICE 2', 'PXW-FX9': 'FX9',
                'ILME-BURANO': 'BURANO', 'EOS C500 Mark II': 'C500 II', 'EOS C500 M': 'C500 II', 'EOS C80': 'C80', 'EOS C70': 'C70',
                'EOS C300 Mark III': 'C300 III', 'EOS C400': 'C400', 'EOS R5 C': 'R5 C', 'EOS C50': 'C50',
                'Blackmagic Pocket Cinema Camera 6K': 'BMPCC 6K', 'Blackmagic Pocket Cinema Camera 4K': 'BMPCC 4K',
                'Blackmagic Cinema Camera 6K': 'BMCC 6K', 'Blackmagic PYXIS': 'PYXIS', 'URSA Cine': 'URSA Cine',
                'NIKON Z 9': 'Z9', 'NIKON Z 8': 'Z8', 'NIKON Z6_3': 'Z6III', 'NIKON Z f': 'Zf', 'NIKON ZR': 'ZR',
                'HERO13': 'GoPro 13', 'HERO12': 'GoPro 12', 'HERO11': 'GoPro 11', 'HERO10': 'GoPro 10',
                'E2-F6': 'Z CAM F6', 'E2-S6': 'Z CAM S6', 'E2-F8': 'Z CAM F8', 'E2-M4': 'Z CAM M4',
                'DC-S5M2': 'S5II', 'DC-S5M2X': 'S5IIX', 'DC-GH6': 'GH6', 'DC-GH7': 'GH7', 'DC-BGH1': 'BGH1',
                'X-H2S': 'X-H2S', 'X-H2': 'X-H2', 'ALEXA 35': 'ALEXA 35', 'ALEXA Mini LF': 'Mini LF',
                'iPhone': 'iPhone', 'Osmo Pocket 3': 'Pocket 3', 'Ronin 4D': 'Ronin 4D'}


def _av():
    import av
    av.logging.set_level(av.logging.ERROR) if hasattr(av, 'logging') else None
    return av


def short_camera(model):
    if not model:
        return ''
    m = re.sub(r'^Canon\s+', '', model.strip(), flags=re.I)       # Canon's own box says "Canon EOS C80"
    for k, v in CAMERA_NAMES.items():
        if m.upper().startswith(k.upper()) and not (len(m) > len(k) and m[len(k)].isdigit() and k[-1].isdigit()):
            return v
    return model


# ------------------------------------------------------------------ Sony sidecar XML
def sony_sidecar(path):
    stem, _ = os.path.splitext(path)
    for cand in (stem + 'M01.XML', stem + 'M01.xml', stem + '.XML', stem + '.xml'):
        if os.path.exists(cand):
            try:
                root = ET.parse(cand).getroot()
            except ET.ParseError:
                return None
            out = {'xml': cand}
            for el in root.iter():
                tag = el.tag.split('}')[-1]
                if tag == 'LtcChangeTable':
                    out['tcFps'] = el.get('tcFps')
                elif tag == 'LtcChange' and el.get('frameCount') == '0' and 'tc' not in out:
                    try:
                        out['tc'], out['df'] = TC.sony_ltc_value(el.get('value'))
                    except ValueError:
                        pass
                elif tag == 'Device':
                    out['model'] = el.get('modelName')
                    out['serial'] = el.get('serialNo')
                elif tag == 'VideoFrame':
                    out['fps'] = el.get('formatFps') or el.get('captureFps')
                    out['capture_fps'] = el.get('captureFps')      # S&Q: shot at this, plays at formatFps
                elif tag == 'CreationDate':
                    out['created'] = el.get('value')
                elif tag == 'Duration':
                    out['duration_frames'] = el.get('value')
            return out
    return None


# ------------------------------------------------------------------ QuickTime atoms (Canon RAW, BRAW, MOV)
QT_EXT = ('.mov', '.mp4', '.m4v', '.crm', '.braw', '.insv', '.3gp')
_CANON_UUID = bytes.fromhex('85c0b687820f11e08111f4ce462b6a48')


def qt_atoms(f, start, end):
    """(type, header_pos, payload_pos, end) for each atom between start and end. Reads headers only."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(16)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack('>I', hdr[:4])[0], hdr[4:8]
        hl = 8
        if size == 1:
            if len(hdr) < 16:
                return
            size, hl = struct.unpack('>Q', hdr[8:16])[0], 16
        elif size == 0:
            size = end - pos
        if size < hl or pos + size > end + 8:
            return
        yield typ, pos, pos + hl, min(pos + size, end)
        pos += size


def _qt_trak(f, a, b, tables=False):
    """What one trak says about itself: media handler, media timescale, first sample description,
    time-to-sample table and the first chunk's file offset. tables=True also returns every chunk offset (co),
    the sample-to-chunk runs (stsc) and the sample size table header (stsz), for reading samples directly."""
    t = dict(handler=None, timescale=None, stsd=None, stts=[], chunk=None, co=None, stsc=None, stsz=None)
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        for typ, _, pl, end in qt_atoms(f, x, y):
            if typ in (b'mdia', b'minf', b'stbl'):
                stack.append((pl, end))
            elif typ == b'hdlr' and t['handler'] is None:          # mdia's handler comes before minf's
                f.seek(pl + 8)
                t['handler'] = f.read(4)
            elif typ == b'mdhd':
                f.seek(pl)
                v = f.read(1)
                f.seek(pl + (20 if v == b'\x01' else 12))
                t['timescale'] = struct.unpack('>I', f.read(4))[0]
            elif typ == b'stsd':
                f.seek(pl + 4)
                if struct.unpack('>I', f.read(4))[0]:
                    sz, fmt = struct.unpack('>I4s', f.read(8))
                    t['stsd'] = (fmt, f.read(max(0, min(sz - 8, 512))))
            elif typ == b'stts':
                f.seek(pl + 4)
                n = min(struct.unpack('>I', f.read(4))[0], 100000)
                raw = f.read(8 * n)
                t['stts'] = [struct.unpack('>II', raw[i:i + 8]) for i in range(0, len(raw) - 7, 8)]
            elif typ in (b'stco', b'co64'):
                f.seek(pl + 4)
                cnt = struct.unpack('>I', f.read(4))[0]
                if cnt:
                    if tables:
                        w = 8 if typ == b'co64' else 4
                        t['co'] = np.frombuffer(f.read(cnt * w), '>u8' if w == 8 else '>u4').astype(np.int64)
                        t['chunk'] = int(t['co'][0])
                    else:
                        t['chunk'] = (struct.unpack('>Q', f.read(8))[0] if typ == b'co64'
                                      else struct.unpack('>I', f.read(4))[0])
            elif tables and typ == b'stsc':
                f.seek(pl + 4)
                cnt = struct.unpack('>I', f.read(4))[0]
                t['stsc'] = np.frombuffer(f.read(cnt * 12), '>u4').reshape(-1, 3).astype(np.int64)
            elif tables and typ == b'stsz':
                f.seek(pl + 4)
                t['stsz'] = struct.unpack('>II', f.read(8))          # (constant size or 0, sample count)
    return t


def _canon_uuid(data):
    """Model and body serial from the Canon uuid atom in moov (Cinema RAW Light, XF-AVC MP4)."""
    out = {}
    runs = [r.decode('ascii', 'replace') for r in re.findall(rb'[\x20-\x7e]{4,}', data)]
    for r in runs:
        m = re.search(r'Canon\s+[ -~]{2,40}', r)
        if m and 'model' not in out:
            out['model'] = m.group(0).strip()
    for r in runs:
        for m in re.finditer(r'(?<!\d)(\d{9,14})(?!\d)', r):
            s = m.group(1)
            if set(s) == {'0'} or (len(s) in (12, 14) and re.match(r'20[0-9]{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])', s)):
                continue                                          # a date written as digits, not a serial
            if r.strip() == s or len(r.strip()) - len(s) <= 4:   # standalone (or behind a 4-letter tag)
                out['serial'] = s
                break
        if 'serial' in out:
            break
    return out


def qt_info(path):
    """Read a QuickTime-style file's own boxes (no decoder needed), for files whose picture ffmpeg can't open:
    tmcd start (frame number at the timecode's own rate), video rate and frame count from the video track,
    and Canon's model + body serial. -> dict (empty if it isn't a QuickTime file)."""
    out = {}
    try:
        f = open(path, 'rb')
    except OSError:
        return out
    with f:
        f.seek(0, 2)
        size = f.tell()
        for typ, _, pl, end in qt_atoms(f, 0, size):
            if typ != b'moov':
                continue
            for t2, _, pl2, end2 in qt_atoms(f, pl, end):
                if t2 == b'trak':
                    t = _qt_trak(f, pl2, end2)
                    if t['handler'] == b'tmcd' and 'tc_frames' not in out and t['stsd'] and t['chunk'] is not None:
                        fmt, p = t['stsd']
                        if fmt == b'tmcd' and len(p) >= 25:
                            flags, ts, fd = struct.unpack('>III', p[12:24])
                            f.seek(t['chunk'])
                            raw = f.read(4)
                            if len(raw) == 4 and ts and fd and not flags & 8:     # bit 3: counter, not timecode
                                out.update(tc_frames=struct.unpack('>I', raw)[0], tc_rate=Fraction(ts, fd),
                                           tc_nf=p[24], tc_df=bool(flags & 1))
                    elif t['handler'] == b'vide' and 'video_frames' not in out and t['stts'] and t['timescale']:
                        out['video_frames'] = sum(n for n, _ in t['stts'])
                        delta = max(t['stts'], key=lambda e: e[0])[1]
                        if delta:
                            out['video_rate'] = Fraction(t['timescale'], delta)
                elif t2 == b'uuid' and end2 - pl2 > 16:
                    f.seek(pl2)
                    head = f.read(16)
                    data = f.read(min(end2 - pl2 - 16, 1 << 20))
                    got = _canon_uuid(data) if head == _CANON_UUID or b'Canon' in data[:65536] else {}
                    for k, v in got.items():
                        out.setdefault(k, v)
            break
    return out


_CANON_PRVW_UUID = bytes.fromhex('eaf42b5e1c984b88b9fbb7dc406e4d16')


def _jpeg_in(buf):
    a = buf.find(b'\xff\xd8\xff')
    if a < 0:
        return None
    b = buf.rfind(b'\xff\xd9')
    return bytes(buf[a:b + 2]) if b > a else None


def embedded_preview(path, kind='prvw'):
    """The picture the camera saved inside the file, read without decoding the video: Canon RAW (.CRM) keeps a
    2048x1080 preview JPEG in a top-level uuid box (PRVW) and a 160x120 thumbnail inside moov (THMB). Both are of
    the start of the clip, in the clip's own gamma (usually Canon Log). -> JPEG bytes or None. Reads only."""
    try:
        f = open(path, 'rb')
    except OSError:
        return None
    with f:
        f.seek(0, 2)
        size = f.tell()
        for typ, _, pl, end in qt_atoms(f, 0, size):
            if kind == 'prvw' and typ == b'uuid' and end - pl > 16:
                f.seek(pl)
                if f.read(16) == _CANON_PRVW_UUID:
                    return _jpeg_in(f.read(min(end - pl - 16, 16 << 20)))
            if kind == 'thmb' and typ == b'moov':
                for t2, _, pl2, end2 in qt_atoms(f, pl, end):
                    if t2 == b'uuid' and end2 - pl2 > 16:
                        f.seek(pl2)
                        if f.read(16) != _CANON_UUID:
                            continue
                        for t3, _, pl3, end3 in qt_atoms(f, pl2 + 16, end2):
                            if t3 == b'THMB':
                                f.seek(pl3)
                                return _jpeg_in(f.read(min(end3 - pl3, 1 << 20)))
            if typ == b'mdat' and kind == 'prvw' and end >= size:
                break
    return None


def qt_timecode(info):
    """(tc string, fps, df) from qt_info's tmcd fields, or None."""
    if 'tc_frames' not in info:
        return None
    try:
        fps = TC.parse_rate(info['tc_rate'])
    except (ValueError, ZeroDivisionError):
        return None
    if info.get('tc_nf') and TC.nominal(fps) != info['tc_nf']:
        return None                                    # rate and frames-per-second disagree: don't guess
    nb = TC.nominal(fps)
    df = info['tc_df'] and nb in (30, 60)
    day = nb * 86400 - ((2 if nb == 30 else 4) * (1440 - 144) if df else 0)
    return TC.frames_to_tc(info['tc_frames'] % day, fps, df), fps, df


# ------------------------------------------------------------------ probes
def probe_wav(path):
    w = WavInfo(path)
    rate, df = w.tc_rate()
    d = w.desc_fields()
    tr = w.time_reference
    names = w.track_names()
    ub = d.get('dUBITS') or d.get('UBITS') or (w.ixml_text('UBITS') or '')
    has_tc = w.bext_raw is not None and (tr not in (None, 0) or 'dTC' in d or w.ixml_text('SPEED', 'TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO'))
    start = None
    if has_tc and tr is not None:
        start = int(round(Fraction(tr) * TC.SR / w.sr)) if w.sr else tr
    fmt_bits = w.bits if w.format_tag != 3 else 32
    return dict(
        kind='audio', path=path, sr=w.sr, channels=w.channels, bits=w.bits, float=(w.format_tag == 3),
        fmt_bits=fmt_bits, nframes=w.nframes, length=int(round(w.nframes * TC.SR / w.sr)) if w.sr else 0,
        start=start, tc_source='bext' if start is not None else None, fps=TC.parse_rate(rate) if rate else None,
        df=df, ubits=ub.strip('$').strip(), track_names=names, originator=w.bext_field('originator'),
        tape=w.ixml_text('TAPE') or '', scene=w.ixml_text('SCENE') or '', take=w.ixml_text('TAKE') or '',
        created=_mtime(path), data_off=w.data_off, rf64=w.is_rf64,
        bext_date=w.bext_field('date'), bext_time=w.bext_field('time'),
    )


def _mtime(path):
    try:
        return dt.datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec='seconds')
    except OSError:
        return ''


def _stream_info(c):
    out = []
    for a in c.streams.audio:
        cc = a.codec_context
        if cc is None:
            continue
        nch = getattr(cc, 'channels', None) or (cc.layout.nb_channels if getattr(cc, 'layout', None) else 1)
        out.append(dict(index=a.index, channels=int(nch or 1), sr=int(cc.sample_rate or 48000), codec=cc.name))
    return out


def p2_audio_files(path):
    """Panasonic P2 (and other OP-Atom cards): CONTENTS/VIDEO/0001AB.MXF has its audio in
    CONTENTS/AUDIO/0001AB00.MXF, 0001AB01.MXF ... one file per channel."""
    d = os.path.dirname(path)
    if os.path.basename(d).upper() != 'VIDEO':
        return []
    adir = os.path.join(os.path.dirname(d), 'AUDIO')
    if not os.path.isdir(adir):
        for n in os.listdir(os.path.dirname(d)):
            if n.upper() == 'AUDIO':
                adir = os.path.join(os.path.dirname(d), n)
                break
        else:
            return []
    stem = os.path.splitext(os.path.basename(path))[0].upper()
    out = []
    for n in sorted(os.listdir(adir)):
        b, e = os.path.splitext(n)
        if e.lower() == '.mxf' and b.upper().startswith(stem) and b[len(stem):].isdigit():
            out.append(os.path.join(adir, n))
    return out


_CAPTURE_KEY = re.compile(r'(capture|sensor|shoot|off.?speed|recording|project).{0,12}(fps|frame.?rate|rate)', re.I)


def capture_rate(md):
    """The rate the camera actually shot at, when the file says so (ARRI sensor fps, Blackmagic sensor rate,
    off-speed metadata). None if the file doesn't say."""
    for k, v in md.items():
        if 'project' in k:
            continue
        if _CAPTURE_KEY.search(k):
            m = re.search(r'\d+(?:\.\d+)?', str(v))
            if m:
                try:
                    f = float(m.group(0))
                except ValueError:
                    continue
                if 1 <= f <= 2000:
                    return TC.parse_rate(m.group(0))
    return None


def probe_video(path, _audio_from=None):
    av = _av()
    out = dict(kind='video', path=path, start=None, tc_source=None, df=False, audio_streams=[], fps=None,
               width=0, height=0, length=0, nframes=0, model='', reel='', created=_mtime(path), codec='')
    side = sony_sidecar(path)
    with av.open(path) as c:
        md = {k.lower(): v for k, v in (c.metadata or {}).items()}
        v = c.streams.video[0] if c.streams.video else None
        tcs = md.get('timecode')
        src = 'container' if tcs else None
        for s in c.streams:
            if not tcs and s.metadata.get('timecode'):
                tcs, src = s.metadata['timecode'], 'stream'
        if v is not None:
            fps = v.average_rate or getattr(v, 'guessed_rate', None) or v.base_rate
            out['fps'] = TC.parse_rate(Fraction(fps)) if fps else None
            cc = v.codec_context
            if cc is not None:
                out['width'], out['height'] = cc.width, cc.height
                out['codec'] = cc.name
            else:
                out['codec'] = 'raw (not decodable here)'
                out['raw_picture'] = True
            nf = v.frames or 0
            if not nf and out['fps']:
                if v.duration and v.time_base:
                    nf = int(round(float(v.duration * v.time_base) * float(out['fps'])))
                elif c.duration:
                    nf = int(round(c.duration / 1e6 * float(out['fps'])))
            out['nframes'] = nf
            vmd = {k.lower(): val for k, val in (v.metadata or {}).items()}
            out['reel'] = vmd.get('reel_name', '') or md.get('reel_name', '')
            cap = capture_rate({**md, **vmd})
            if cap:
                out['capture_fps'] = cap
        out['audio_streams'] = _stream_info(c)
        model = md.get('product_name') or md.get('com.apple.quicktime.model') or md.get('model') or ''
        make = md.get('company_name') or md.get('com.apple.quicktime.make') or md.get('make') or ''
        if make.strip().lower() in ('ffmpeg', 'lavf') or 'muxer' in model.lower():
            model = make = ''
        if not model:
            model = md.get('camera_type', '') or md.get('encoder_device_name', '')
        out['model'] = (model or make or '').strip()
        if md.get('creation_time') or md.get('com.apple.quicktime.creationdate'):
            out['created'] = md.get('com.apple.quicktime.creationdate') or md['creation_time']
            out['created_meta'] = True
    # P2 / OP-Atom: audio lives in separate files
    if not out['audio_streams']:
        for ap in p2_audio_files(path):
            try:
                with av.open(ap) as ac:
                    for st in _stream_info(ac):
                        st['path'] = ap
                        out['audio_streams'].append(st)
            except Exception:  # noqa: BLE001
                pass
        if out['audio_streams']:
            out['opatom'] = True
    if side:
        out['model'] = side.get('model') or out['model']
        if side.get('created'):
            out['created'] = side['created']
            out['created_meta'] = True
        if not out['fps'] and side.get('fps'):
            out['fps'] = TC.parse_rate(side['fps'])
        if side.get('capture_fps'):
            try:
                out['capture_fps'] = TC.parse_rate(re.sub(r'[^0-9.]', '', side['capture_fps']))
            except (ValueError, ZeroDivisionError):
                pass
        if side.get('tc') and not tcs:
            tcs, src = side['tc'], 'sony-xml'
            out['df'] = side.get('df', False)
        if side.get('serial'):
            out['serial'] = side['serial']
    if os.path.splitext(path)[1].lower() in QT_EXT:
        # Canon RAW / BRAW: ffmpeg can't open the picture, so it never sees the tmcd track. Read the boxes.
        try:
            qi = qt_info(path)
        except (OSError, struct.error, ValueError):
            qi = {}
        if not out['fps'] and qi.get('video_rate'):
            try:
                out['fps'] = TC.parse_rate(qi['video_rate'])
            except (ValueError, ZeroDivisionError):
                pass
        if qi.get('video_frames') and (not out['nframes'] or out.get('raw_picture')):
            out['nframes'] = qi['video_frames']
        if qi.get('model') and not out['model']:
            out['model'] = qi['model']
        if qi.get('serial') and not out.get('serial'):
            out['serial'] = qi['serial']
        q = None if tcs else qt_timecode(qi)
        if q:
            tcs, tfps, tdf = q
            if not out['fps']:
                out['fps'] = tfps
            out['df'] = tdf
            out['start'] = TC.tc_to_samples(tcs, tfps, tdf)     # at the timecode's own rate
            out['tc_source'], out['tc_string'] = 'qt-tmcd', tcs
            if TC.nominal(tfps) != TC.nominal(out['fps']):
                out['tc_rate'] = TC.rate_label(tfps)
    if out['fps'] and out['nframes']:
        out['length'] = TC.frames_to_samples(out['nframes'], out['fps'])
    if tcs and out['fps'] and out.get('tc_source') != 'qt-tmcd':
        try:
            out['df'] = out['df'] or (';' in tcs)
            out['start'] = TC.tc_to_samples(tcs, out['fps'], out['df'])
            out['tc_source'] = src
            out['tc_string'] = tcs
        except ValueError:
            pass
    out['camera'] = short_camera(out['model'])
    return out


def find_proxy(path):
    """A playable file with the same clip name next to a RAW file (or in a proxy folder beside it)."""
    d, b = os.path.split(path)
    stem = os.path.splitext(b)[0].upper()
    dirs = [d]
    parent = os.path.dirname(d)
    for base in (d, parent):
        try:
            for n in os.listdir(base):
                if any(k in n.lower() for k in ('proxy', 'proxies', 'sub')) and os.path.isdir(os.path.join(base, n)):
                    dirs.append(os.path.join(base, n))
        except OSError:
            pass
    for dd in dirs:
        try:
            names = os.listdir(dd)
        except OSError:
            continue
        for n in sorted(names):
            nb, ne = os.path.splitext(n)
            if ne.lower() in ('.mp4', '.mov', '.mxf') and (nb.upper() == stem or nb.upper().startswith(stem + '_')
                                                          or nb.upper().startswith(stem + 'S')):
                return os.path.join(dd, n)
    return None


def probe_raw(path):
    """RED / Nikon N-RAW / Z CAM: try reading the file itself; otherwise take audio + timecode from its proxy."""
    try:
        info = probe_video(path)
        if info['audio_streams'] and info['length']:
            return info
    except Exception:  # noqa: BLE001
        pass
    px = find_proxy(path)
    if not px:
        raise ValueError(WHY.get(os.path.splitext(path)[1].lower(), 'unsupported RAW file'))
    info = probe_video(px)
    info['path'] = path
    info['audio_path'] = px
    info['codec'] = 'raw (audio from proxy)'
    info['raw_picture'] = True
    for st in info['audio_streams']:
        st['path'] = px
    return info


def probe(path, convert_dir=None):
    from . import convert as CV
    ext = os.path.splitext(path)[1].lower()
    if ext in AUDIO_EXT:
        info = probe_wav(path)
        if info['sr'] != TC.SR and convert_dir:
            out = CV.resample_bwf(path, convert_dir)
            info2 = probe_wav(out)
            info2.update(source_path=path, converted=f"{info['sr'] / 1000:g} kHz → 48 kHz", source_sr=info['sr'])
            info = info2
    elif ext in AUDIO_OTHER_EXT:
        if not convert_dir:
            raise ValueError('needs converting to WAV')
        out = CV.to_wav(path, convert_dir)
        info = probe_wav(out)
        info.update(source_path=path, converted=f"{ext[1:].upper()} → 48 kHz WAV")
    elif ext in VIDEO_EXT:
        info = probe_video(path)
    elif ext in RAW_TRY_EXT:
        info = probe_raw(path)
    else:
        raise ValueError('unsupported file type')
    try:
        check_ltc(info)
    except Exception:  # noqa: BLE001
        pass
    return info


def read_head(item, seconds=6.0):
    """First `seconds` of every audio channel -> (array (nch, n) float32, sr)."""
    if item['kind'] == 'audio':
        w = WavInfo(item['path'])
        n = min(w.nframes, int(seconds * w.sr))
        x = w.read(0, n)
        x = x.reshape(-1, 1) if x.ndim == 1 else x
        return x.T.astype(np.float32), w.sr
    chans, sr = [], 48000
    for k in range(total_audio_channels(item)):
        sr = audio_sr(item, k)
        blocks = [b for _, b in iter_audio(item, k, max_samples=int(seconds * sr))]
        chans.append(np.concatenate(blocks) if blocks else np.zeros(0, np.float32))
    if not chans:
        return np.zeros((0, 0), np.float32), sr
    n = min(len(c) for c in chans)
    return np.stack([c[:n] for c in chans]), sr


def check_ltc(info, fps_hint=None, force=False):
    """Look for timecode recorded as audio (LTC). Only when the file has no usable timecode of its own,
    unless force. Sets info['ltc'], and the start if the file had none."""
    from . import ltc as LTC
    zero = info.get('start') is not None and info['start'] < TC.SR
    if not force and info.get('start') is not None and not zero:
        return None
    if total_audio_channels(info) == 0:
        return None
    x, sr = read_head(info, 8.0)
    for k in range(min(x.shape[0], 8)):
        r = LTC.start_from_ltc(x[k], sr, fps_hint or info.get('fps') or TC.parse_rate('23.976'))
        if r:
            off = info.get('_audio_offset', 0)
            info['ltc'] = dict(channel=k, tc=TC.samples_to_tc(r['start'], r['fps'], r['df']), fps=TC.rate_label(r['fps']),
                               frames=r['frames'], agree=r['agree'], first_tc=r['first_tc'], anchor=r.get('anchor'),
                               rate_from='file' if info.get('fps') else 'guess')
            if info.get('start') is None or zero:
                info['start'] = r['start'] - off
                info['tc_source'] = f'LTC on ch {k + 1}'
                info['df'] = r['df']
                if not info.get('fps'):
                    info['fps'] = r['fps']
            info['listen_ch'] = next((j for j in range(x.shape[0]) if j != k and np.abs(x[j]).max() > 1e-4), None)
            return info['ltc']
    return None


# ------------------------------------------------------------------ audio streaming
def _frame_to_float(frame, channel, nch):
    arr = frame.to_ndarray()
    name = frame.format.name
    if frame.format.is_planar:
        mat = arr  # (ch, n)
    else:
        mat = arr.reshape(-1, nch).T
    if channel == 'mix':
        x = mat.astype(np.float64).mean(0)
    else:
        x = mat[min(int(channel), mat.shape[0] - 1)]
    if name.startswith('s16'):
        return (np.asarray(x, np.float64) / 32768.0).astype(np.float32)
    if name.startswith('s32'):
        return (np.asarray(x, np.float64) / 2147483648.0).astype(np.float32)
    if name.startswith('u8'):
        return ((np.asarray(x, np.float64) - 128) / 128.0).astype(np.float32)
    return np.asarray(x, np.float32)


def iter_audio(item, channel=0, max_samples=None, block=48000 * 10):
    """Yield float32 mono blocks of an item's audio at its native rate, from the very start of the file.
    For video: 'channel' indexes the audio channels across streams (FX6 CH1 = 0); 'mix' mixes stream 0.
    Yields (offset_samples_at_native_rate_of_first_block, block) — offset accounts for audio starting
    later than video (positive) — only on the first block; later blocks have offset None."""
    if item['kind'] == 'audio':
        w = WavInfo(item['path'])
        n = w.nframes if max_samples is None else min(w.nframes, max_samples)
        with open(item['path'], 'rb') as fh:
            first = True
            for s0 in range(0, n, block):
                cnt = min(block, n - s0)
                if channel == 'mix':
                    x = w.read(s0, cnt, None, fh).mean(1)
                else:
                    x = w.read(s0, cnt, min(int(channel), w.channels - 1), fh)
                yield (0 if first else None), x
                first = False
        return
    av = _av()
    # resolve which stream holds the requested channel
    streams = item.get('audio_streams') or []
    if not streams:
        return
    sidx, ch = streams[0]['index'], 'mix'
    if channel != 'mix':
        acc = 0
        for s in streams:
            if channel < acc + s['channels']:
                sidx, ch = s['index'], channel - acc
                break
            acc += s['channels']
        else:
            sidx, ch = streams[0]['index'], 0
    sinfo = [s for s in streams if s['index'] == sidx]
    if channel != 'mix':
        acc = 0
        for s in streams:
            if channel < acc + s['channels']:
                sinfo = [s]
                break
            acc += s['channels']
    sinfo = sinfo[0] if sinfo else streams[0]
    src = sinfo.get('path') or item.get('audio_path') or item['path']
    if (os.path.splitext(src)[1].lower() in QT_EXT and ch != 'mix' and not item.get('no_fast_audio')
            and os.environ.get('PREP_SLOW_AUDIO') != '1'):
        fast = _iter_qt_pcm(src, sinfo, int(ch), max_samples, block)
        if fast is not None:
            first_off = 0
            try:                               # audio that starts after the picture (edit lists): same offset as below
                with av.open(src) as c0:
                    a0 = c0.streams[sinfo['index']]
                    v0 = c0.streams.video[0] if c0.streams.video else None
                    if a0.start_time is not None and v0 is not None and v0.start_time is not None:
                        first_off = int(round((float(a0.start_time * a0.time_base) - float(v0.start_time * v0.time_base))
                                              * (sinfo.get('sr') or 48000)))
            except Exception:  # noqa: BLE001
                first_off = 0
            for off, blk in fast:
                yield (first_off if off is not None else None), blk
            return
    with av.open(src) as c:
        st = c.streams[sinfo['index']] if sinfo['index'] < len(c.streams) else c.streams.audio[0]
        nch = sinfo['channels']
        vstart = 0.0
        if c.streams.video and c.streams.video[0].start_time is not None:
            vs = c.streams.video[0]
            vstart = float(vs.start_time * vs.time_base)
        got = 0
        first = True
        buf = []
        bufn = 0
        for frame in c.decode(st):
            x = _frame_to_float(frame, ch, nch)
            if first:
                off = 0
                if frame.pts is not None and frame.time_base is not None:
                    off = int(round((float(frame.pts * frame.time_base) - vstart) * (frame.sample_rate or 48000)))
                first_off = off
                first = False
            if max_samples is not None and got + len(x) > max_samples:
                x = x[:max_samples - got]
            buf.append(x)
            bufn += len(x)
            got += len(x)
            if bufn >= block:
                yield (first_off if first_off is not None else None), np.concatenate(buf)
                first_off = None
                buf, bufn = [], 0
            if max_samples is not None and got >= max_samples:
                break
        if buf:
            yield (first_off if not first and first_off is not None else None), np.concatenate(buf)


_PCM = {'pcm_s16le': '<i2', 'pcm_s16be': '>i2', 'pcm_s24le': 's24le', 'pcm_s24be': 's24be',
        'pcm_s32le': '<i4', 'pcm_s32be': '>i4', 'pcm_f32le': '<f4', 'pcm_f32be': '>f4'}
_QT_AUDIO_CACHE = {}


def _qt_pcm_layout(path, stream_index):
    """Where one PCM audio track's samples sit in a QuickTime file: [(file offset, frames)] per chunk.
    stream_index is ffmpeg's stream number, which follows the trak order. None if anything doesn't add up."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_size, st.st_mtime_ns, stream_index)
    if key in _QT_AUDIO_CACHE:
        return _QT_AUDIO_CACHE[key]
    out = None
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            n = f.tell()
            for typ, _, pl, end in qt_atoms(f, 0, n):
                if typ != b'moov':
                    continue
                traks = [(pl2, end2) for t2, _, pl2, end2 in qt_atoms(f, pl, end) if t2 == b'trak']
                if stream_index < len(traks):
                    t = _qt_trak(f, *traks[stream_index], tables=True)
                    if t['handler'] == b'soun' and t['co'] is not None and t['stsc'] is not None and t['stsz']:
                        co, stsc = t['co'], t['stsc']
                        per = np.zeros(len(co), np.int64)
                        for i, (first, spc, _) in enumerate(stsc):
                            stop = stsc[i + 1][0] - 1 if i + 1 < len(stsc) else len(co)
                            per[first - 1:stop] = spc
                        if per.sum() == t['stsz'][1]:
                            out = dict(chunks=list(zip(co.tolist(), per.tolist())), size=t['stsz'][0])
                break
    except (OSError, struct.error, ValueError):
        out = None
    if len(_QT_AUDIO_CACHE) > 512:
        _QT_AUDIO_CACHE.clear()
    _QT_AUDIO_CACHE[key] = out
    return out


def _iter_qt_pcm(path, sinfo, ch, max_samples, block):
    """Camera audio straight from the file's sample table: only the audio bytes are read, never the picture
    (a 17 GB Canon RAW clip holds ~23 MB of audio per channel). Yields like iter_audio; None if not possible."""
    fmt = _PCM.get(sinfo.get('codec') or '')
    nch = int(sinfo.get('channels') or 1)
    if not fmt:
        return None
    lay = _qt_pcm_layout(path, sinfo['index'])
    if not lay:
        return None
    bps = 3 if fmt.startswith('s24') else int(fmt[-1])
    frame_bytes = bps * nch
    if lay['size'] not in (frame_bytes, 0):
        return None

    def gen():
        got, buf, bufn, first = 0, [], 0, True
        with open(path, 'rb') as f:
            for off, frames in lay['chunks']:
                if max_samples is not None and got >= max_samples:
                    break
                f.seek(off)
                raw = f.read(frames * frame_bytes)
                k = len(raw) // frame_bytes
                if not k:
                    break
                raw = raw[:k * frame_bytes]
                if fmt.startswith('s24'):
                    a = np.frombuffer(raw, np.uint8).reshape(-1, nch, 3)[:, ch, :].astype(np.int32)
                    v = (a[:, 0] | (a[:, 1] << 8) | (a[:, 2] << 16)) if fmt == 's24le' else (a[:, 2] | (a[:, 1] << 8) | (a[:, 0] << 16))
                    v = np.where(v & 0x800000, v - 0x1000000, v)
                    x = v.astype(np.float32) / 8388608.0
                else:
                    a = np.frombuffer(raw, fmt).reshape(-1, nch)[:, ch]
                    x = a.astype(np.float32) / (1.0 if 'f' in fmt else float(2 ** (8 * bps - 1)))
                if max_samples is not None and got + len(x) > max_samples:
                    x = x[:max_samples - got]
                buf.append(x)
                bufn += len(x)
                got += len(x)
                if bufn >= block:
                    yield (0 if first else None), np.concatenate(buf)
                    first, buf, bufn = False, [], 0
            if buf:
                yield (0 if first else None), np.concatenate(buf)
    return gen()


def audio_sr(item, channel=0):
    if item['kind'] == 'audio':
        return item['sr']
    streams = item.get('audio_streams') or []
    return streams[0]['sr'] if streams else 48000


def total_audio_channels(item):
    if item['kind'] == 'audio':
        return item['channels']
    return sum(s['channels'] for s in item.get('audio_streams') or [])


def find_media(paths, skip_dirs=()):
    """Walk dropped files/folders -> list of media paths (sorted), plus list of unsupported files."""
    found, unsupported = [], []
    skip_names = {'sub', 'thmbnl', 'take', 'general', 'proxy', 'proxies', '.spotlight-v100', '.fseventsd',
                  '.trashes', '.ds_store', 'thumbnails', 'thumbnail',
                  'trash', 'undo'}       # Sound Devices: deleted takes and undo copies are not part of the day
    out_markers = ('ISO_ALIGNED', 'POLY_ALIGNED', 'POLY_UNALIGNED', 'AAP_RENDERS', 'REF_', 'TC_FIXED', '_MIDWIG', 'WORK FILES (not for the edit)', '_SYNC_WORK', 'CONVERTED_48K', 'EDITORIAL', 'SPLIT_MONO')
    skip_dirs = [os.path.realpath(s) for s in skip_dirs]

    def consider(p):
        b = os.path.basename(p)
        if b.startswith('.') or b.startswith('._'):
            return
        ext = os.path.splitext(b)[1].lower()
        parts = [x.upper() for x in os.path.normpath(p).split(os.sep)[-3:-1]]
        if ext == '.mxf' and parts == ['CONTENTS', 'AUDIO']:
            return                                   # P2 per-channel audio: read through its VIDEO clip
        seg = re.search(r'_(\d{3})$', os.path.splitext(b)[0])
        if ext == '.r3d' and seg and int(seg.group(1)) > 1:
            return                                   # RED spanned clip: only the _001 segment is the clip
        if ext in VIDEO_EXT or ext in AUDIO_EXT or ext in AUDIO_OTHER_EXT or ext in RAW_TRY_EXT:
            found.append(p)
        elif ext in UNSUPPORTED_EXT:
            unsupported.append(p)

    for p in paths:
        p = p.rstrip('/') if len(p) > 1 else p
        if os.path.isfile(p):
            consider(p)
            continue
        visited = set()
        for root, dirs, files in os.walk(p, followlinks=True):
            rr = os.path.realpath(root)
            if rr in visited:                       # linked folders: never walk the same place twice
                dirs[:] = []
                continue
            visited.add(rr)
            if any(os.path.isdir(os.path.join(root, d)) for d in ('_SYNC_WORK', '_MIDWIG', 'WORK FILES (not for the edit)')):   # our own output
                dirs[:] = []
                continue
            if any(rr == s or rr.startswith(s + os.sep) for s in skip_dirs):
                dirs[:] = []
                continue
            dirs[:] = sorted(d for d in dirs if d.lower() not in skip_names and not d.startswith('.')
                             and not d.startswith(out_markers) and not d.endswith(('_SYNCED', '_MIDWIG')))
            for fn in sorted(files):
                consider(os.path.join(root, fn))
    seen, uniq = set(), []
    for p in found:
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq, unsupported
