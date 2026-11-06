"""Bring odd audio into the 48 kHz broadcast-WAV world the rest of the app works in.

Copies go to a cache folder; originals are never touched. Timecode, bext and iXML (track names, scene,
take...) are carried over when the source has them.
"""
import hashlib
import os
import shutil
import subprocess

from . import tc as TC
from .bwf import WavInfo, build_bext, build_ixml, wav_header, FMT_FLOAT, FMT_PCM
from .stamp import ffmpeg_exe
from .brand import APP_NAME, CONVERT_DIR

CACHE = CONVERT_DIR


def _dest(src, convert_dir, ext='.WAV'):
    st = os.stat(src)
    key = hashlib.sha1(f'{os.path.realpath(src)}|{st.st_size}|{st.st_mtime}'.encode()).hexdigest()[:16]
    d = os.path.join(convert_dir or CACHE, key)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, os.path.splitext(os.path.basename(src))[0] + ext)


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or '').strip()[-400:] or 'ffmpeg failed')


def to_wav(src, convert_dir=None):
    """Any audio file ffmpeg can read (AIFF, M4A, MP3, FLAC, CAF...) -> 48 kHz 24-bit WAV."""
    out = _dest(src, convert_dir)
    if os.path.exists(out):
        return out
    tmp = out + '.partial.wav'
    _run([ffmpeg_exe(), '-y', '-v', 'error', '-i', src, '-map', '0:a:0', '-vn', '-map_metadata', '-1',
          '-ar', str(TC.SR), '-c:a', 'pcm_s24le', '-rf64', 'auto', tmp])
    os.replace(tmp, out)
    return out


def resample_bwf(src, convert_dir=None):
    """Broadcast WAV at 44.1 / 88.2 / 96 / 192 kHz -> 48 kHz, same timecode, bext and iXML.
    32-bit float stays 32-bit float (keeps the headroom above 0 dBFS)."""
    out = _dest(src, convert_dir)
    if os.path.exists(out):
        return out
    w = WavInfo(src)
    is_float = w.format_tag == FMT_FLOAT
    raw = out + '.partial.raw'
    _run([ffmpeg_exe(), '-y', '-v', 'error', '-i', src, '-map', '0:a:0', '-ar', str(TC.SR), '-af',
          'aresample=resampler=soxr:precision=28' if _has_soxr() else 'aresample',
          '-f', 'f32le' if is_float else 's24le', '-c:a', 'pcm_f32le' if is_float else 'pcm_s24le', raw])
    bits = 32 if is_float else 24
    nframes = os.path.getsize(raw) // (w.channels * bits // 8)
    tr = w.time_reference
    tr48 = int(round(tr * TC.SR / w.sr)) if tr is not None else 0
    hist = f'A={"FLOAT" if is_float else "PCM"},F=48000,W={bits},M={w.channels},T={APP_NAME}: resampled from {w.sr} Hz\r\n'
    be = build_bext(w.description, base=w.bext_raw, time_reference=tr48, coding_history=hist) if w.bext_raw else \
        build_bext('', APP_NAME, '', '', '', tr48, hist)
    ix = None
    base = w.ixml()
    rate, df = w.tc_rate()
    fr = TC.parse_rate(rate) if rate else TC.parse_rate('23.976')
    if base is not None:
        for el in base.iter():
            if el.tag in ('FILE_SAMPLE_RATE', 'DIGITIZER_SAMPLE_RATE', 'TIMESTAMP_SAMPLE_RATE'):
                el.text = str(TC.SR)
    names = [w.track_names().get(i + 1, f'CH{i + 1}') for i in range(w.channels)]
    ix = build_ixml('', TC.rate_label(fr), f'{fr.numerator}/{fr.denominator}', bool(df), tr48, TC.SR, bits, names,
                    base_xml=base)
    hdr = wav_header(nframes, w.channels, TC.SR, bits, FMT_FLOAT if is_float else FMT_PCM, be, ix)
    tmp = out + '.partial.wav'
    with open(tmp, 'wb') as dst, open(raw, 'rb') as r:
        dst.write(hdr)
        shutil.copyfileobj(r, dst, 1 << 24)
        if (nframes * w.channels * bits // 8) & 1:
            dst.write(b'\0')
    os.remove(raw)
    os.replace(tmp, out)
    return out


_SOXR = None


def _has_soxr():
    global _SOXR
    if _SOXR is None:
        try:
            r = subprocess.run([ffmpeg_exe(), '-hide_banner', '-buildconf'], capture_output=True, text=True)
            _SOXR = 'libsoxr' in (r.stdout + r.stderr)
        except Exception:  # noqa: BLE001
            _SOXR = False
    return _SOXR
