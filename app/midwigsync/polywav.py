"""Polywav chunks: earliest start -> latest end, one channel per track (track-number order), silence where a
recorder wasn't rolling. Each chunk is a whole number of timecode minutes and stays under the 4 GiB WAV limit,
carrying its own start TC, track names, user bits and frame rate (bext + iXML)."""
import datetime as dt
import os
import numpy as np

from . import tc as TC
from .brand import APP_NAME
from .bwf import WavInfo, WavWriter, build_bext, build_ixml, wav_header, RIFF_LIMIT

SR = TC.SR
BLK = 60 * SR


def plan_chunks(t0, t1, nch, bits, fps, header_len=4096, mode='max'):
    """Chunk boundaries (absolute samples). mode 'max' = largest whole TC minute under 4 GiB."""
    bpf = nch * bits // 8
    max_frames = (RIFF_LIMIT - header_len - 64) // bpf
    per_min = TC.frames_to_samples(TC.nominal(fps) * 60, fps)
    minutes = max(1, max_frames // per_min)
    if mode == 'hour' and minutes >= 60:
        minutes = (minutes // 60) * 60
    L = minutes * per_min
    out = []
    a = t0
    while a < t1:
        out.append((a, min(a + L, t1)))
        a += L
    return out, minutes


def build_polywavs(sources, channel_names, out_dir, prefix, fps, df=False, ubits='', project='', note='',
                   mode='max', log=print, progress=None, cancel=None, day_date=''):
    """sources: [dict(path, start, channels=[poly channel index per file channel])]."""
    progress = progress or (lambda *a: None)
    cancel = cancel or (lambda: False)
    infos = []
    for s in sources:
        w = WavInfo(s['path'])
        infos.append(dict(s, info=w, n=w.nframes))
    if not infos:
        return []
    nch = len(channel_names)
    all_float = all(i['info'].format_tag == 3 for i in infos)
    bits, fmt = (32, 3) if all_float else (24, 1)
    raw_ok = all(i['info'].format_tag == 1 and i['info'].bits == 24 for i in infos) and not all_float
    t0 = min(i['start'] for i in infos)
    t1 = max(i['start'] + i['n'] for i in infos)
    fr = TC.parse_rate(fps)
    date = ''
    if ubits and len(ubits) >= 8 and ubits[:8].isdigit():
        date = f'{ubits[:4]}-{ubits[4:6]}-{ubits[6:8]}'
    # then the shoot day itself. Today's date is the last resort: a delivery whose origination date is the day
    # the sync ran (not the day it was shot) reads as wrong in Avid and Premiere.
    date = date or day_date or dt.date.today().isoformat()

    def header(a, n):
        tcs = TC.samples_to_tc(a, fr, df)
        desc = ''.join(f'sTRK{i + 1}={nm}\r\n' for i, nm in enumerate(channel_names))
        desc += f'dTC={tcs}\r\n'
        if ubits:
            desc += f'dUBITS=${ubits}\r\n'
        desc += f'dFRAMERATE={TC.rate_label(fr)}{"DF" if df else "ND"}\r\nsSPEED={TC.rate_label(fr).zfill(7)}-{"DF" if df else "ND"}\r\n'
        hist = f'A=PCM,F={SR},W={bits},M={nch},T={note or APP_NAME + " polywav"}\r\n'
        if len(desc) > 256:   # many channels: keep TC fields, names stay in iXML
            desc = f'dTC={tcs}\r\ndUBITS=${ubits}\r\ndFRAMERATE={TC.rate_label(fr)}\r\n'
        be = build_bext(desc, APP_NAME, project[:32], date, tcs[:8], TC.wrap(a, fr), hist)
        ix = build_ixml(project, TC.rate_label(fr), f'{fr.numerator}/{fr.denominator}', df, TC.wrap(a, fr), SR, bits,
                        list(channel_names), note=note)
        return be, ix

    be, ix = header(t0, 1)
    hlen = len(wav_header(1, nch, SR, bits, fmt, be, ix))
    chunks, minutes = plan_chunks(t0, t1, nch, bits, fr, hlen + 512, mode)
    log(f'  {len(chunks)} polywav file(s) of up to {minutes // 60}:{minutes % 60:02}:00:00, {nch} channels')
    fhs = {}
    outs = []
    try:
        for k, (a, b) in enumerate(chunks):
            n = b - a
            tcs = TC.samples_to_tc(a, fr, df)
            name = f"{prefix}_{k + 1:02}of{len(chunks):02}_TC{tcs.replace(':', '').replace(';', '')[:6]}.WAV"
            path = os.path.join(out_dir, name)
            be, ix = header(a, n)
            ww = WavWriter(path, n, nch, SR, bits, fmt, be, ix)
            try:
                for s0 in range(a, b, BLK):
                    if cancel():
                        raise KeyboardInterrupt
                    progress(k + (s0 - a) / n, len(chunks), f'Polywav {k + 1}/{len(chunks)}')
                    s1 = min(s0 + BLK, b)
                    if raw_ok:
                        buf = np.zeros((s1 - s0, nch, 3), np.uint8)
                    else:
                        buf = np.zeros((s1 - s0, nch), np.float32)
                    for s in infos:
                        x0, x1 = max(s0, s['start']), min(s1, s['start'] + s['n'])
                        if x1 <= x0:
                            continue
                        w = s['info']
                        fh = fhs.get(s['path']) or fhs.setdefault(s['path'], open(s['path'], 'rb'))
                        if raw_ok:
                            raw = np.frombuffer(w.read_raw(x0 - s['start'], x1 - x0, fh), np.uint8)
                            raw = raw[:(x1 - x0) * w.channels * 3].reshape(-1, w.channels, 3)
                            for fc, pc in enumerate(s['channels']):
                                buf[x0 - s0:x0 - s0 + len(raw), pc, :] = raw[:, fc, :]
                        else:
                            x = w.read(x0 - s['start'], x1 - x0, None, fh)
                            for fc, pc in enumerate(s['channels']):
                                buf[x0 - s0:x1 - s0, pc] = x[:, fc]
                    if raw_ok:
                        ww.write_raw(buf.tobytes())
                    else:
                        ww.write_float(buf)
                ww.close()
            except BaseException:
                ww.abort()
                raise
            outs.append(dict(path=path, start=a, n=n, tc=tcs, bytes=os.path.getsize(path)))
            log(f'  wrote {name}  ({os.path.getsize(path) / 1e9:.2f} GB)')
    finally:
        for f in fhs.values():
            f.close()
    return outs
