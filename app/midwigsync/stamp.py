"""Write corrected-timecode copies. Originals are never modified."""
import os
import subprocess
from . import tc as TC
from .brand import APP_NAME
from .bwf import WavInfo, build_bext, build_ixml, set_desc_field, wav_header, _chunk  # noqa: F401
import struct


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return 'ffmpeg'


def stamp_wav(clip, out_path, start_48k, fps, df=False, note=None, pad_to_frame=True):
    """Copy with new TC. With pad_to_frame, silence is added at the head so the file starts exactly on a
    frame (NLEs place audio by whole frames). Returns (path, new_start_48k)."""
    note = note or f'{APP_NAME} waveform-sync'
    w = WavInfo(clip['path'])
    pad = 0
    if pad_to_frame:
        f0 = TC.samples_to_frames(start_48k, fps, 'floor')
        new_start = TC.frames_to_samples(f0, fps)
        pad = int(round((start_48k - new_start) * w.sr / TC.SR))
        if pad <= 1:
            pad = 0
        else:
            start_48k = new_start
    tr = int(round(TC.wrap(start_48k, fps) * w.sr / TC.SR))
    tcs = TC.samples_to_tc(start_48k, fps, df)
    hist = f'A=PCM,F={w.sr},W={w.bits},M={"mono" if w.channels == 1 else w.channels},T={note}; TC set to {tcs}\r\n'
    if w.bext_raw:
        desc = set_desc_field(w.description, 'dTC', tcs)
        be = build_bext(desc, base=w.bext_raw, time_reference=tr, coding_history=hist)
    else:
        desc = (f"sTRK1={clip['roll']}\r\n" if w.channels == 1 else
                ''.join(f"sTRK{i + 1}={clip['roll']}_{i + 1}\r\n" for i in range(w.channels)))
        desc += f'dTC={tcs}\r\ndFRAMERATE={TC.rate_label(fps)}{"DF" if df else "ND"}\r\n'
        be = build_bext(desc, APP_NAME, clip['roll'], '', '', tr, hist)
    base = w.ixml()
    names = [w.track_names().get(i + 1, clip['roll'] if w.channels == 1 else f"{clip['roll']}_{i + 1}")
             for i in range(w.channels)]
    fr = TC.parse_rate(fps)
    ix = build_ixml(clip['roll'], TC.rate_label(fr), f'{fr.numerator}/{fr.denominator}', df, tr, w.sr,
                    w.bits, names, base_xml=base)
    fmt = w.format_tag if w.format_tag in (1, 3) else 1
    hdr = wav_header(w.nframes + pad, w.channels, w.sr, w.bits, fmt, be, ix)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = os.path.join(os.path.dirname(out_path), '.' + os.path.basename(out_path) + '.partial')
    with open(clip['path'], 'rb') as src, open(tmp, 'wb') as dst:
        dst.write(hdr)
        if pad:
            dst.write(b'\0' * (pad * w.block_align) if fmt == 1 or w.bits != 8 else b'\x80' * (pad * w.block_align))
        src.seek(w.data_off)
        left = w.nframes * w.block_align
        while left > 0:
            b = src.read(min(left, 1 << 24))
            if not b:
                break
            dst.write(b)
            left -= len(b)
        if left > 0:
            dst.write(b'\0' * left)
        if ((w.nframes + pad) * w.block_align) & 1:
            dst.write(b'\0')
    os.replace(tmp, out_path)
    return out_path, start_48k


def stamp_video(clip, out_path, start_48k, fps, df=False):
    """Re-wrap (no re-encode) into .mov with a timecode track at the nearest whole frame."""
    frames = TC.samples_to_frames(start_48k, fps)
    tcs = TC.frames_to_tc(frames, fps, df)
    out_path = os.path.splitext(out_path)[0] + '.mov'
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = os.path.join(os.path.dirname(out_path), '.' + os.path.basename(out_path) + '.partial.mov')
    cmd = [ffmpeg_exe(), '-y', '-v', 'error', '-i', clip['path'], '-map', '0:v:0?', '-map', '0:a?',
           '-c', 'copy', '-timecode', tcs, '-metadata', f"reel_name={clip['roll']}"]
    if clip.get('codec') in ('hevc', 'h265'):
        cmd += ['-tag:v', 'hvc1']
    cmd += [tmp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError(r.stderr.strip()[-400:])
    os.replace(tmp, out_path)
    return out_path, frames


# ------------------------------------------------------------------ camera copies, only the timecode changed
QT_EXT = ('.mov', '.mp4', '.m4v', '.crm', '.braw', '.insv', '.3gp')
MXF_TC_KEY = bytes.fromhex('060e2b34025301010d01010101011400')     # Timecode Component (local set)


def _qt_atoms(f, start, end):
    """(type, header_pos, payload_pos, end) for each atom between start and end."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(16)
        size, typ = struct.unpack('>I', hdr[:4])[0], hdr[4:8]
        hl = 8
        if size == 1:
            size, hl = struct.unpack('>Q', hdr[8:16])[0], 16
        elif size == 0:
            size = end - pos
        if size < hl:
            return
        yield typ, pos, pos + hl, pos + size
        pos += size


def _qt_tmcd_offset(path):
    """File offset of the timecode sample (4-byte frame number) of a QuickTime/MP4 tmcd track, or None."""
    with open(path, 'rb') as f:
        f.seek(0, 2)
        n = f.tell()
        for typ, _, pl, end in _qt_atoms(f, 0, n):
            if typ != b'moov':
                continue
            for t2, _, pl2, end2 in _qt_atoms(f, pl, end):
                if t2 != b'trak':
                    continue
                handler, offs = None, None
                stack = [(pl2, end2)]
                while stack:
                    a, b = stack.pop()
                    for t3, _, pl3, end3 in _qt_atoms(f, a, b):
                        if t3 in (b'mdia', b'minf', b'stbl'):
                            stack.append((pl3, end3))
                        elif t3 == b'hdlr' and handler is None:      # the media handler (mdia), not minf's data handler
                            f.seek(pl3 + 8)
                            handler = f.read(4)
                        elif t3 in (b'stco', b'co64'):
                            f.seek(pl3 + 4)
                            cnt = struct.unpack('>I', f.read(4))[0]
                            if cnt:
                                offs = struct.unpack('>Q', f.read(8))[0] if t3 == b'co64' else struct.unpack('>I', f.read(4))[0]
                if handler == b'tmcd' and offs is not None:
                    return offs
    return None


def _mxf_patch(path, old_frames, new_frames):
    """Rewrite StartTimecode in every Timecode Component of the header metadata (start and end of the file)
    that holds old_frames. Returns how many were changed."""
    n = 0
    size = os.path.getsize(path)
    regions = [(0, min(size, 8 << 20)), (max(0, size - (8 << 20)), size)]
    with open(path, 'r+b') as f:
        done = set()
        for a, b in regions:
            f.seek(a)
            buf = f.read(b - a)
            i = buf.find(MXF_TC_KEY)
            while i >= 0:
                p = i + 16
                L = buf[p]
                if L & 0x80:
                    k = L & 0x7F
                    length = int.from_bytes(buf[p + 1:p + 1 + k], 'big')
                    p += 1 + k
                else:
                    length = L
                    p += 1
                q, end = p, min(p + length, len(buf))
                while q + 4 <= end:
                    tag, ln = struct.unpack('>HH', buf[q:q + 4])
                    if tag == 0x1501 and ln == 8:
                        v = struct.unpack('>q', buf[q + 4:q + 12])[0]
                        at = a + q + 4
                        if v == old_frames and at not in done:
                            f.seek(at)
                            f.write(struct.pack('>q', int(new_frames)))
                            done.add(at)
                            n += 1
                    q += 4 + ln
                i = buf.find(MXF_TC_KEY, i + 16)
    return n


def copy_with_tc(clip, out_dir, start_48k):
    """A copy of a camera file with only its timecode changed: an exact (cloned) copy of the original bytes with the
    timecode number rewritten (QuickTime/MP4/CRM/BRAW tmcd, MXF timecode components). If the format can't be patched
    that way, the streams are copied into a .mov (no re-encode, every frame bit-identical). Returns (path, how)."""
    from . import safety
    from .media import probe_video
    src = clip['path']
    fps = clip.get('fps')
    df = bool(clip.get('df'))
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, os.path.basename(src))
    old = TC.samples_to_frames(clip['orig_start'], fps) if clip.get('orig_start') is not None else None
    new = TC.samples_to_frames(start_48k, fps)
    ext = os.path.splitext(src)[1].lower()
    tmp = os.path.join(out_dir, '.' + os.path.basename(src) + '.partial')
    try:
        if ext in QT_EXT or ext == '.mxf':
            safety.clone_or_copy(src, tmp)
            ok = False
            if ext in QT_EXT:
                off = _qt_tmcd_offset(tmp)
                if off is not None:
                    with open(tmp, 'r+b') as f:
                        f.seek(off)
                        f.write(struct.pack('>I', int(new) & 0xFFFFFFFF))
                    ok = True
            elif old is not None:
                ok = _mxf_patch(tmp, old, new) > 0
            if ok:
                got = probe_video(tmp)
                if got.get('start') is not None and abs(got['start'] - TC.frames_to_samples(new, fps)) <= TC.samples_per_frame(fps):
                    os.replace(tmp, out)
                    return out, 'exact copy, only the timecode changed'
            os.remove(tmp)
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)
    path, _ = stamp_video(clip, out, start_48k, fps, df)
    return path, 'streams copied into .mov, every frame bit-identical'
