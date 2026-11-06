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
