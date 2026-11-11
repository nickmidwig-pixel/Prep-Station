"""A frame through the video library that ships inside the app (PyAV). Its decoders are newer than the ffmpeg
program's (ProRes RAW among them), so this is the second thing tried when ffmpeg can't read a picture.
Read-only. Returns JPEG bytes or None."""
import io

import av
import numpy as np


def frame_jpeg(path, sec=0.0, width=480):
    try:
        with av.open(path, 'r') as c:
            v = next((s for s in c.streams if s.type == 'video'), None)
            if v is None:
                return None
            v.thread_type = 'AUTO'
            if sec > 0.2 and v.time_base:
                try:
                    c.seek(int(sec / v.time_base), stream=v, backward=True, any_frame=False)
                except Exception:  # noqa: BLE001 - a file that can't seek: decode from the top
                    pass
            got = None
            for i, f in enumerate(c.decode(v)):
                got = f
                if f.pts is None or v.time_base is None or float(f.pts * v.time_base) >= sec - 0.02 or i > 240:
                    break
            if got is None:
                return None
            h = max(2, int(round(got.height * width / max(1, got.width) / 2)) * 2)
            raw = got.format.name.startswith('bayer') or 'raw' in (v.codec_context.name or '')
            rgb = got.reformat(width=width, height=h, format='rgb48le' if raw else 'rgb24').to_ndarray()
            if raw:
                rgb = _levels(rgb)
            return _jpeg(rgb)
    except Exception:  # noqa: BLE001
        return None


def _levels(x16):
    """Sensor data (linear or log, no white balance, raised blacks) into a picture you can judge a shot by:
    per-channel black and white points, then a gamma that puts the middle of the picture at middle grey."""
    x = x16.astype(np.float32)
    lo = np.percentile(x, 1.0, axis=(0, 1))
    hi = np.percentile(x, 99.5, axis=(0, 1))
    x = np.clip((x - lo) / np.maximum(hi - lo, 1.0), 0, 1)
    med = float(np.clip(np.median(x), 0.02, 0.98))
    g = float(np.clip(np.log(0.42) / np.log(med), 0.4, 2.4))      # <1 lifts a dark (linear) frame, >1 pulls down a washed one
    return (x ** g * 255 + 0.5).astype(np.uint8)


def _jpeg(rgb):
    h, w = rgb.shape[:2]
    out = io.BytesIO()
    with av.open(out, 'w', format='mjpeg') as oc:
        st = oc.add_stream('mjpeg', rate=1)
        st.width, st.height, st.pix_fmt = w, h, 'yuvj420p'
        st.options = {'q:v': '5'}
        fr = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format='rgb24').reformat(format='yuvj420p')
        fr.pts = 0
        for p in st.encode(fr):
            oc.mux(p)
        for p in st.encode(None):
            oc.mux(p)
    data = out.getvalue()
    return data if data[:2] == b'\xff\xd8' else None
