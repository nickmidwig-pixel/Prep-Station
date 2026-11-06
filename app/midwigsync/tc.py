"""Timecode math.

Engine-wide time base: integer samples at 48 kHz since timecode midnight
(the same convention as BWF TimeReference). At 23.976 one frame = 2002 samples.
"""
from fractions import Fraction
import re

SR = 48000

KNOWN_RATES = {
    '23.976': Fraction(24000, 1001), '23.98': Fraction(24000, 1001),
    '24': Fraction(24), '25': Fraction(25),
    '29.97': Fraction(30000, 1001), '30': Fraction(30),
    '47.952': Fraction(48000, 1001), '48': Fraction(48),
    '50': Fraction(50), '59.94': Fraction(60000, 1001), '60': Fraction(60),
}


def parse_rate(x):
    """'23.976', '23.98p', '24000/1001', 23.976, Fraction -> Fraction (snapped to a known rate)."""
    if x is None:
        return None
    if isinstance(x, Fraction):
        f = x
    else:
        s = str(x).strip().lower().replace('fps', '').replace('p', '').replace('i', '')
        s = s.replace('nd', '').replace('df', '').replace('-', '').strip()
        if '/' in s:
            a, b = s.split('/')[:2]
            try:
                f = Fraction(int(float(a)), int(float(b)))
            except (ValueError, ZeroDivisionError):
                return None
        else:
            try:
                f = Fraction(s)
            except ValueError:
                return None
    if f <= 0:
        return None
    best = min(KNOWN_RATES.values(), key=lambda r: abs(float(r) - float(f)))
    if abs(float(best) - float(f)) < 0.02:
        return best
    return f.limit_denominator(1001)


def nominal(fps):
    return int(round(float(fps)))


def is_ntsc(fps):
    return Fraction(fps).denominator == 1001


def rate_label(fps):
    for k in ('23.976', '24', '25', '29.97', '30', '47.952', '48', '50', '59.94', '60'):
        if KNOWN_RATES[k] == fps:
            return k
    return f'{float(fps):.3f}'


def samples_per_frame(fps):
    return Fraction(SR) / Fraction(fps)


def tc_to_frames(tc, fps, df=None):
    """'HH:MM:SS:FF' or 'HH:MM:SS;FF' -> frame count since midnight."""
    m = re.match(r'^\s*(\d{1,2})[:.](\d{2})[:.](\d{2})([:;.,])(\d{2,3})\s*$', str(tc))
    if not m:
        raise ValueError(f'bad timecode {tc!r}')
    h, mi, s, sep, f = int(m[1]), int(m[2]), int(m[3]), m[4], int(m[5])
    nb = nominal(fps)
    if df is None:
        df = sep in ';,' and nb in (30, 60)
    frames = ((h * 60 + mi) * 60 + s) * nb + f
    if df:
        drop = 2 if nb == 30 else 4
        total_min = h * 60 + mi
        frames -= drop * (total_min - total_min // 10)
    return frames


def frames_to_tc(frames, fps, df=False):
    nb = nominal(fps)
    frames = int(frames) % (nb * 86400)
    if df and nb in (30, 60):
        drop = 2 if nb == 30 else 4
        per10 = nb * 600 - drop * 9
        d, mfr = divmod(frames, per10)
        if mfr > drop:
            frames += drop * 9 * d + drop * ((mfr - drop) // (nb * 60 - drop))
        else:
            frames += drop * 9 * d
        sep = ';'
    else:
        sep = ':'
    f = frames % nb
    s = frames // nb % 60
    m = frames // (nb * 60) % 60
    h = frames // (nb * 3600) % 24
    return f'{h:02}:{m:02}:{s:02}{sep}{f:02}'


def frames_to_samples(frames, fps):
    return int(round(Fraction(int(frames)) * samples_per_frame(fps)))


def samples_to_frames(samples, fps, rounding='round'):
    x = Fraction(int(samples)) / samples_per_frame(fps)
    if rounding == 'floor':
        return x.numerator // x.denominator
    if rounding == 'ceil':
        return -((-x.numerator) // x.denominator)
    return int(round(x))


def tc_to_samples(tc, fps, df=None):
    return frames_to_samples(tc_to_frames(tc, fps, df), fps)


def samples_to_tc(samples, fps, df=False):
    return frames_to_tc(samples_to_frames(samples, fps), fps, df)


def frame_fraction(samples, fps):
    """How far (0..1 of a frame) a sample position sits from the nearest frame edge."""
    x = Fraction(int(samples)) / samples_per_frame(fps)
    return float(abs(x - round(x)))


def day_samples(fps):
    """One timecode day (24:00:00:00) in engine samples."""
    return frames_to_samples(nominal(fps) * 86400, fps)


def wrap(samples, fps):
    """Engine samples -> samples since timecode midnight (for writing BWF time references)."""
    if samples is None:
        return None
    return int(samples) % day_samples(fps)


def fmt_dur(samples):
    s = int(samples) // SR
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f'{h}:{m:02}:{s:02}' if h else f'{m}:{s:02}'


def sony_ltc_value(value):
    """Sony NRT XML <LtcChange value="..."> (BCD FF SS MM HH with flag bits) -> ('HH:MM:SS:FF', df)."""
    v = str(value).strip()
    if len(v) != 8:
        raise ValueError(value)
    b = [int(v[i:i + 2], 16) for i in (0, 2, 4, 6)]
    ff, ss, mm, hh = b

    def bcd(x):
        return (x >> 4) * 10 + (x & 0xF)
    df = bool(ff & 0x40)
    return f'{bcd(hh & 0x3F):02}:{bcd(mm & 0x7F):02}:{bcd(ss & 0x7F):02}{";" if df else ":"}{bcd(ff & 0x3F):02}', df
