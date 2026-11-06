"""Linear timecode (LTC, SMPTE 12M) recorded as audio: decode it, and find which channel carries it.

Typical case: a Tentacle / Deity / UltraSync box plugged into the mic input of a camera with no timecode
input (GoPro, DSLR, iPhone rig, Z CAM...). One audio channel then holds the LTC "buzz"; the other has
the scratch mic. We read the timecode from the buzz and keep the buzz out of waveform matching.

Bit layout (80 bits per frame, LSB first): frame units 0-3, frame tens 8-9, drop flag 10, seconds units
16-19, seconds tens 24-26, minutes units 32-35, minutes tens 40-42, hours units 48-51, hours tens 56-57,
sync word 64-79 = 0011111111111101. Biphase mark: a transition at every bit edge, plus one mid-bit for a 1.
The start of bit 0 lines up with the start of the video frame the code names.
"""
import numpy as np

from . import tc as TC

SYNC = np.array([0, 0] + [1] * 12 + [0, 1], np.int8)


# ------------------------------------------------------------------ encode (used by the tests)
def frame_bits(frames, fps, df=False):
    nb = TC.nominal(fps)
    t = TC.frames_to_tc(frames, fps, df)
    h, m, s, f = int(t[0:2]), int(t[3:5]), int(t[6:8]), int(t[9:11])
    b = np.zeros(80, np.int8)

    def put(val, at, n):
        for i in range(n):
            b[at + i] = (val >> i) & 1
    put(f % 10, 0, 4)
    put(f // 10, 8, 2)
    b[10] = 1 if (df and nb in (30, 60)) else 0
    put(s % 10, 16, 4)
    put(s // 10, 24, 3)
    put(m % 10, 32, 4)
    put(m // 10, 40, 3)
    put(h % 10, 48, 4)
    put(h // 10, 56, 2)
    b[64:80] = SYNC
    # polarity correction bit: even number of zeros in the frame
    pc = 59 if nb == 25 else 27
    if (80 - int(b.sum())) % 2:
        b[pc] = 1 - b[pc]
    return b


def encode(start_frames, n_frames, fps, sr=48000, df=False, amp=0.5, lead_s=0.0):
    """Synthesize LTC audio: n_frames starting at frame count start_frames; frame 0 of the code begins
    `lead_s` seconds into the returned signal."""
    bits = np.concatenate([frame_bits(start_frames + k, fps, df) for k in range(n_frames)])
    bit_len = sr / (80 * float(fps))
    lead = int(round(lead_s * sr))
    n = lead + int(np.ceil(len(bits) * bit_len)) + 1
    out = np.full(n, amp, np.float32)
    level = amp
    edges = []
    for i, bit in enumerate(bits):
        t0 = lead + i * bit_len
        edges.append(t0)
        if bit:
            edges.append(t0 + bit_len / 2)
    edges = np.round(np.array(edges)).astype(np.int64)
    sign = np.ones(n, np.float32)
    flips = np.zeros(n + 1, np.int32)
    np.add.at(flips, np.clip(edges, 0, n), 1)
    parity = np.cumsum(flips[:n]) % 2
    sign = np.where(parity == 1, -1.0, 1.0).astype(np.float32)
    out = level * sign
    out[:lead] = 0
    return out


# ------------------------------------------------------------------ decode
def _edges(x):
    """Zero-crossing times (fractional samples) of a two-level signal, ignoring noise via hysteresis."""
    x = np.asarray(x, np.float64)
    x = x - np.median(x)
    amp = np.percentile(np.abs(x), 90)
    if amp < 1e-4:
        return np.zeros(0)
    th = 0.3 * amp
    st = np.zeros(len(x), np.int8)
    st[x > th] = 1
    st[x < -th] = -1
    nz = np.nonzero(st)[0]
    if len(nz) < 10:
        return np.zeros(0)
    filled = st[nz]
    flips = nz[1:][filled[1:] != filled[:-1]]          # first sample past the threshold after a change
    sg = np.signbit(x)
    zc = np.nonzero(sg[1:] != sg[:-1])[0]               # x[j], x[j+1] straddle zero
    if not len(zc):
        return np.zeros(0)
    k = np.searchsorted(zc, flips) - 1
    k = k[k >= 0]
    j = zc[k]
    a, b = x[j], x[j + 1]
    t = j + a / np.where(a - b == 0, 1e-12, a - b)
    return np.unique(t)


def decode(x, sr):
    """-> list of dict(pos=sample position of bit 0 (float, in x), tc='HH:MM:SS:FF', frames_in_sec=f, df=bool)."""
    t = _edges(x)
    if len(t) < 200:
        return []
    d = np.diff(t)
    lo, hi = sr / (80 * 64) * 0.4, sr / (80 * 22) * 1.4        # plausible half/full-bit lengths
    dd = d[(d > lo) & (d < hi)]
    if len(dd) < 100:
        return []
    # two clusters: half bits and full bits
    full = np.percentile(dd, 90)
    for _ in range(8):
        half_c = dd[dd < 0.75 * full]
        full_c = dd[dd >= 0.75 * full]
        if not len(full_c):
            break
        full = float(np.median(full_c))
        if len(half_c):
            full = 0.5 * (full + 2 * float(np.median(half_c)))
    T = full
    bits, pos = [], []
    i = 0
    n = len(d)
    while i < n:
        di = d[i]
        if 0.75 * T <= di <= 1.3 * T:
            bits.append(0)
            pos.append(t[i])
            i += 1
        elif 0.3 * T <= di < 0.75 * T and i + 1 < n and 0.3 * T <= d[i + 1] < 0.75 * T:
            bits.append(1)
            pos.append(t[i])
            i += 2
        else:
            bits.append(-1)                                  # glitch: breaks any frame across it
            pos.append(t[i])
            i += 1
    bits = np.array(bits, np.int8)
    pos = np.array(pos)
    if len(bits) < 80:
        return []
    # sync word positions (index of bit 79)
    win = np.lib.stride_tricks.sliding_window_view(bits, 16)
    ends = np.nonzero((win == SYNC).all(1))[0] + 15
    out = []
    for e in ends:
        s0 = e - 79
        if s0 < 0:
            continue
        fb = bits[s0:e + 1]
        if (fb < 0).any():
            continue

        def get(at, k):
            return int(sum(int(fb[at + q]) << q for q in range(k)))
        fu, ft = get(0, 4), get(8, 2)
        su, st_ = get(16, 4), get(24, 3)
        mu, mt = get(32, 4), get(40, 3)
        hu, ht = get(48, 4), get(56, 2)
        if fu > 9 or su > 9 or mu > 9 or hu > 9 or st_ > 5 or mt > 5:
            continue
        h, m, s, f = ht * 10 + hu, mt * 10 + mu, st_ * 10 + su, ft * 10 + fu
        if h > 23:
            continue
        df = bool(fb[10])
        out.append(dict(pos=float(pos[s0]), tc=f'{h:02}:{m:02}:{s:02}{";" if df else ":"}{f:02}', h=h, m=m, s=s, f=f,
                        df=df, bit_len=T))
    return out


def rate_from_frames(frames, hint=None):
    """Guess the frame rate family from the highest frame number seen; 23.976 vs 24 (and 29.97 vs 30)
    can't be told apart from LTC, so the project rate (hint) decides those."""
    top = max((fr['f'] for fr in frames), default=0)
    if top >= 30:
        fam = 60 if top >= 50 else 50
    elif top >= 25:
        fam = 30
    elif top >= 24:
        fam = 25
    else:
        fam = 24
    if hint is not None and TC.nominal(hint) == fam:
        return hint
    return {24: TC.parse_rate('24'), 25: TC.parse_rate('25'), 30: TC.parse_rate('29.97'),
            50: TC.parse_rate('50'), 60: TC.parse_rate('59.94')}[fam]


def start_from_ltc(x, sr, fps_hint=None, min_frames=8):
    """Where (48k samples since midnight) sample 0 of x sits, according to the LTC in x.
    -> dict(start, frames, agree, fps, df, first_tc) or None."""
    fr = decode(x, sr)
    if len(fr) < min_frames:
        return None
    fps = rate_from_frames(fr, fps_hint)
    df = sum(f['df'] for f in fr) > len(fr) / 2 and TC.nominal(fps) in (30, 60)
    est = []
    for f in fr:
        try:
            s = TC.tc_to_samples(f['tc'], fps, df)
        except ValueError:
            continue
        est.append(s - f['pos'] * TC.SR / sr)
    if len(est) < min_frames:
        return None
    est = np.array(est)
    med = float(np.median(est))
    tol = 0.5 * float(TC.samples_per_frame(fps))
    good = np.abs(est - med) < tol
    if good.mean() < 0.6:
        return None
    start = float(np.median(est[good]))
    return dict(start=int(round(start)), frames=int(good.sum()), agree=round(float(good.mean()), 2), fps=fps, df=df,
                first_tc=fr[0]['tc'])


def looks_like_ltc(x, sr):
    """Quick check: does this audio carry LTC? (a handful of valid frames in a few seconds)"""
    try:
        return len(decode(x, sr)) >= 4
    except Exception:  # noqa: BLE001
        return False
