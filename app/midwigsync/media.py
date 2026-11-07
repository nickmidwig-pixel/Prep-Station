"""Probe camera and audio files for timecode / format, and stream their audio."""
import os
import re
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
                'ILME-BURANO': 'BURANO', 'EOS C500 Mark II': 'C500 II', 'EOS C80': 'C80', 'EOS C70': 'C70',
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
    for k, v in CAMERA_NAMES.items():
        if model.upper().startswith(k.upper()):
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
    if out['fps'] and out['nframes']:
        out['length'] = TC.frames_to_samples(out['nframes'], out['fps'])
    if tcs and out['fps']:
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
                  '.trashes', '.ds_store', 'thumbnails', 'thumbnail'}
    out_markers = ('ISO_ALIGNED', 'POLY_ALIGNED', 'POLY_UNALIGNED', 'AAP_RENDERS', 'REF_', 'TC_FIXED', '_MIDWIG', '_SYNC_WORK', 'CONVERTED_48K', 'EDITORIAL', 'SPLIT_MONO')
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
            if any(os.path.isdir(os.path.join(root, d)) for d in ('_SYNC_WORK', '_MIDWIG')):   # our own output
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
