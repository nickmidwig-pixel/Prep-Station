"""Full-length aligned ISOs from Auto-Align Post renders.

AAP renders only the pieces of each ISO that sit under a reference clip, as plain WAVs with no timecode.
Render <src>_si0ch0off<N>-AtAlgn-<hash>.wav: render sample 0 = source start + N.
Where there's no render (camera not rolling, or AAP cut a render short), the original is used, shifted
by the offset + polarity measured at the nearest render edge (plain normalized cross-correlation),
or borrowed from the other ISOs at the same time (same recorder clock family).
Output: same start TC, same length, original bext/iXML + a coding-history line.
"""
import json
import os
import numpy as np

from . import tc as TC
from .bwf import WavInfo, WavWriter, build_bext
from .resolve_aap import RENDER_RE
from .brand import APP_NAME

SR = TC.SR
XF = 480                 # 10 ms crossfades
WIN = 10 * SR            # measurement window
R = SR // 2              # +/- 500 ms search
BLK = 60 * SR
CONF = 0.3


def _mono(x):
    return x.mean(1) if x.ndim == 2 else x


def ncc_lag(o_ext, a):
    """o_ext = original[t0-R : t0+W+R], a = render[t0 : t0+W] -> (L, cc): render(t) ~ cc_sign * orig(t+L)."""
    W = len(a)
    n = 1 << int(np.ceil(np.log2(len(o_ext) + W)))
    c = np.fft.irfft(np.fft.rfft(o_ext, n) * np.conj(np.fft.rfft(a, n)), n)[:len(o_ext) - W + 1]
    oo = np.concatenate([[0], np.cumsum(o_ext.astype(np.float64) ** 2)])
    eo = oo[W:W + len(c)] - oo[:len(c)]
    cc = c / np.sqrt((a.astype(np.float64) ** 2).sum() * np.maximum(eo, 1e-20))
    k = int(np.argmax(np.abs(cc)))
    return k - R, float(cc[k])


class Assembler:
    def __init__(self, sources, render_files, out_dir, log=print, progress=None, note='', cancel=None, ref_windows=None):
        """sources: [dict(path, name, start, roll, out_path?)]  (start = absolute 48k samples of the file's first sample)
        ref_windows: {source path: [(a, b)]} where that source IS the reference (copied through unchanged)."""
        self.ref_windows = ref_windows or {}
        self.log = log
        self.progress = progress or (lambda *a: None)
        self.cancel = cancel or (lambda: False)
        self.out_dir = out_dir
        self.note = note
        self.src = {}
        for s in sources:
            w = WavInfo(s['path'])
            stem = os.path.splitext(os.path.basename(s['path']))[0]
            self.src[stem] = dict(s, info=w, stem=stem, n=w.nframes)
        self.rends = []
        for p in render_files:
            m = RENDER_RE.match(os.path.basename(p))
            if not m or m['src'] not in self.src:
                continue
            o = self.src[m['src']]
            try:
                w = WavInfo(p)
            except ValueError:
                continue
            if w.sr != o['info'].sr:
                self.log(f'  skipping render with a different sample rate: {os.path.basename(p)}')
                continue
            self.rends.append(dict(path=p, src=m['src'], start=o['start'] + int(m['off']), n=w.nframes, info=w))
        self.plans = {}

    # -------------------------------------------------------------- measuring
    def _orig(self, o, abs_t, count):
        return o['info'].read(abs_t - o['start'], count)

    def _rend(self, r, abs_t, count):
        return r['info'].read(abs_t - r['start'], count)

    def _edge(self, o, r, a, b, which):
        best = (0, 0.0)
        span = b - a
        if span < 2 * SR:
            return dict(L=0, cc=0.0, t=a)
        W = min(WIN, span)
        starts = [a + k * W for k in range(4)] if which == 'start' else [b - (k + 1) * W for k in range(4)]
        t_best = a if which == 'start' else b
        for t in starts:
            if t < a or t + W > b:
                continue
            av = _mono(self._rend(r, t, W))
            if np.abs(av).max() < 1e-3:
                continue
            ov = _mono(self._orig(o, t - R, W + 2 * R))
            if np.abs(ov).max() < 1e-4:
                continue
            L, cc = ncc_lag(ov, av)
            if abs(cc) > abs(best[1]):
                best, t_best = (L, cc), t
        return dict(L=int(best[0]), cc=round(best[1], 3), t=int(t_best))

    def plan_all(self):
        # pass 1: pieces + edge measurements for every source
        for i, (stem, o) in enumerate(self.src.items()):
            self.progress(i, len(self.src), f'Measuring renders: {stem}')
            S, E = o['start'], o['start'] + o['n']
            pieces = []
            for r in sorted([r for r in self.rends if r['src'] == stem], key=lambda r: r['start']):
                a, b = max(r['start'], S), min(r['start'] + r['n'], E)
                if b - a < SR:
                    continue
                if pieces and a < pieces[-1]['b']:
                    mid = (a + pieces[-1]['b']) // 2
                    pieces[-1]['b'] = mid
                    a = mid
                pieces.append(dict(a=a, b=b, render=r['path']))
            for pc in pieces:
                r = next(x for x in self.rends if x['path'] == pc['render'])
                pc['start_edge'] = self._edge(o, r, pc['a'], pc['b'], 'start')
                pc['end_edge'] = self._edge(o, r, pc['a'], pc['b'], 'end')
            for a, b in self.ref_windows.get(o['path'], []):
                a, b = max(a, S), min(b, E)
                if b - a >= SR // 10:      # this track is the reference here: offset 0 by definition
                    pieces.append(dict(a=a, b=b, render=None, start_edge=dict(L=0, cc=1.0, t=a),
                                       end_edge=dict(L=0, cc=1.0, t=b)))
            pieces.sort(key=lambda p: p['a'])
            for p0, p1 in zip(pieces, pieces[1:]):
                if p1['a'] < p0['b']:
                    p0['b'] = p1['a'] = (p0['b'] + p1['a']) // 2
            self.plans[stem] = dict(pieces=pieces)
        # every confident edge, for borrowing
        edges = []
        for stem, pl in self.plans.items():
            for pc in pl['pieces']:
                if pc.get('render') is None:
                    continue                      # a reference stretch says nothing about the other tracks
                for e in (pc['start_edge'], pc['end_edge']):
                    if abs(e['cc']) > CONF:
                        edges.append((e['t'], e['L'], stem))
        # pass 2: segments
        for stem, o in self.src.items():
            pl = self.plans[stem]
            S, E = o['start'], o['start'] + o['n']
            segs, cur, flags = [], S, []

            def edge_ok(e):
                return e if e and abs(e['cc']) > CONF else None

            def borrow(t0, t1):
                mid = (t0 + t1) / 2
                near = [L for (t, L, st) in edges if st != stem and abs(t - mid) < 10 * 60 * SR]
                if near:
                    flags.append(f'borrowed offset for {TC.fmt_dur(t1 - t0)} at {TC.samples_to_tc(t0, TC.parse_rate("23.976"))}')
                    return int(np.median(near)), 1
                if t1 - t0 > 5 * SR:
                    flags.append(f'{TC.fmt_dur(t1 - t0)} left as recorded (no alignment info nearby)')
                return 0, 1

            def ramp(t0, t1, eb, ea, step=5 * SR):
                """Gap between two measured edges: follow the clock drift with small offset steps."""
                pb, pa = (1 if eb['cc'] > 0 else -1), (1 if ea['cc'] > 0 else -1)
                if pb != pa or abs(ea['t'] - eb['t']) < SR:
                    mid = (t0 + t1) // 2
                    return [[t0, mid, 'gap', eb['L'], pb], [mid, t1, 'gap', ea['L'], pa]]
                k = max(1, int(round((t1 - t0) / step)))
                out = []
                for i in range(k):
                    a = t0 + (t1 - t0) * i // k
                    b = t0 + (t1 - t0) * (i + 1) // k
                    m = (a + b) / 2
                    f = min(1.0, max(0.0, (m - eb['t']) / (ea['t'] - eb['t'])))
                    out.append([a, b, 'gap', int(round(eb['L'] + f * (ea['L'] - eb['L']))), pb])
                return out

            pcs = pl['pieces']
            for i, pc in enumerate(pcs):
                if pc['a'] > cur:
                    eb = edge_ok(pcs[i - 1]['end_edge']) if i > 0 else None
                    ea = edge_ok(pc['start_edge'])
                    if eb is None and ea is None:
                        L, pol = borrow(cur, pc['a'])
                        segs.append([cur, pc['a'], 'gap', L, pol])
                    elif eb is None:
                        segs.append([cur, pc['a'], 'gap', ea['L'], 1 if ea['cc'] > 0 else -1])
                    elif ea is None:
                        segs.append([cur, pc['a'], 'gap', eb['L'], 1 if eb['cc'] > 0 else -1])
                    else:
                        segs += ramp(cur, pc['a'], eb, ea)
                segs.append([pc['a'], pc['b'], 'aap', 0, pc['render']] if pc['render'] else [pc['a'], pc['b'], 'ref', 0, 1])
                cur = pc['b']
            if cur < E:
                e = edge_ok(pcs[-1]['end_edge']) if pcs else None
                if e:
                    segs.append([cur, E, 'gap', e['L'], 1 if e['cc'] > 0 else -1])
                else:
                    L, pol = borrow(cur, E)
                    segs.append([cur, E, 'gap', L, pol])
            pl['segs'] = segs
            pl['flags'] = flags
            pl['aligned_s'] = round(sum(s[1] - s[0] for s in segs if s[2] in ('aap', 'ref')) / SR, 1)
            pl['total_s'] = round(o['n'] / SR, 1)
        return self.plans

    # -------------------------------------------------------------- writing
    def _piece(self, o, seg, t0, n, rmap):
        a, b, kind, L, x = seg
        if kind == 'aap':
            r = rmap[x]
            return self._rend(r, t0, n)
        if kind == 'ref':
            return self._orig(o, t0, n)
        return self._orig(o, t0 + L, n) * x

    def _block(self, o, segs, t0, t1, rmap):
        ch = o['info'].channels
        out = np.zeros((t1 - t0, ch), np.float32)
        for i, s in enumerate(segs):
            a, b = s[0], s[1]
            lo = max(a - XF // 2, t0) if i > 0 else max(a, t0)
            hi = min(b + XF // 2, t1) if i < len(segs) - 1 else min(b, t1)
            if hi <= lo:
                continue
            tt = np.arange(lo, hi)
            w = np.ones(hi - lo, np.float32)
            if i > 0:
                w *= np.clip((tt - (a - XF // 2)) / XF, 0, 1)
            if i < len(segs) - 1:
                w *= np.clip(((b + XF // 2) - tt) / XF, 0, 1)
            pcs = self._piece(o, s, lo, hi - lo, rmap)
            if pcs.ndim == 1:
                pcs = pcs[:, None]
            if pcs.shape[1] != ch:
                pcs = np.repeat(pcs[:, :1], ch, 1) if pcs.shape[1] < ch else pcs[:, :ch]
            out[lo - t0:hi - t0] += pcs * w[:, None]
        return out

    def write_all(self, out_names=None):
        os.makedirs(self.out_dir, exist_ok=True)
        rmap = {r['path']: r for r in self.rends}
        results = []
        for i, (stem, o) in enumerate(self.src.items()):
            if self.cancel():
                raise KeyboardInterrupt
            pl = self.plans[stem]
            w = o['info']
            od = os.path.join(self.out_dir, o['roll'])
            fn = o.get('out_path') or os.path.join(od, (out_names or {}).get(stem, stem + '_ALN') + '.WAV')
            os.makedirs(os.path.dirname(fn), exist_ok=True)
            n_al = sum(1 for s in pl['segs'] if s[2] in ('aap', 'ref'))
            hist = (f'A=PCM,F={w.sr},W={w.bits},M={"mono" if w.channels == 1 else w.channels},'
                    f'T=Auto-Align Post 2 ({n_al} sections) ref {self.note}; {APP_NAME}\r\n')
            if w.bext_raw:
                be = build_bext(base=w.bext_raw, coding_history=hist)
            else:
                be = build_bext('', APP_NAME, o['roll'], '', '', o['start'], hist)
            ixml = w.ixml_raw.split(b'\0')[0] if w.ixml_raw else None
            fmt = w.format_tag if w.format_tag in (1, 3) else 1
            ww = WavWriter(fn, o['n'], w.channels, w.sr, w.bits, fmt, be, ixml)
            try:
                for s0 in range(0, o['n'], BLK):
                    self.progress(i + s0 / max(o['n'], 1), len(self.src), f'Writing aligned {stem}')
                    t0 = o['start'] + s0
                    t1 = o['start'] + min(s0 + BLK, o['n'])
                    ww.write_float(self._block(o, pl['segs'], t0, t1, rmap))
                ww.close()
            except BaseException:
                ww.abort()
                raise
            results.append(dict(source=o['path'], aligned=fn, roll=o['roll'], start=o['start'], n=o['n'],
                                sections=n_al, aligned_s=pl['aligned_s'], total_s=pl['total_s'], flags=pl['flags']))
        return results

    def save_plan(self, path):
        def clean(v):
            if isinstance(v, dict):
                return {k: clean(x) for k, x in v.items() if k != 'info'}
            if isinstance(v, (list, tuple)):
                return [clean(x) for x in v]
            if isinstance(v, (np.integer,)):
                return int(v)
            if isinstance(v, (np.floating,)):
                return float(v)
            return v
        with open(path, 'w') as f:
            json.dump(clean(self.plans), f, indent=1)
