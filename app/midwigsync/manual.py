"""Hands-on timecode tools for one clip: type a timecode, nudge it, or find it by waveform on demand.

A position set here is "locked": runs keep it, it's written into the TC_FIXED copy, and other clips can
be placed against it.
"""
import os
import re

from . import tc as TC
from . import media
from . import stamp as ST
from .wavesync import SyncAnalyzer, refine

ENV_CACHE = os.path.expanduser('~/Library/Caches/Midwig Sync/envelopes')


def parse_tc_input(text, fps, df=False):
    """'14:22:31:07', '14:22:31;07', '14.22.31.07', '14223107', '14:22:31' -> 48k samples since midnight."""
    t = str(text or '').strip()
    digits = re.sub(r'\D', '', t)
    if re.fullmatch(r'\d{1,2}[:;.,]\d{2}[:;.,]\d{2}', t):
        t += ':00'
    elif re.fullmatch(r'\d{8}', t):
        t = f'{t[:2]}:{t[2:4]}:{t[4:6]}:{t[6:]}'
    elif re.fullmatch(r'\d{6}', t):
        t = f'{t[:2]}:{t[2:4]}:{t[4:6]}:00'
    elif len(digits) == 8 and not re.search(r'[:;.,]', t):
        t = f'{digits[:2]}:{digits[2:4]}:{digits[4:6]}:{digits[6:]}'
    h, m, s, f = [int(x) for x in re.split(r'[:;.,]', t)]
    nb = TC.nominal(fps)
    if h > 23 or m > 59 or s > 59 or f >= nb:
        raise ValueError(f'{text} isn’t a valid timecode at {TC.rate_label(fps)} fps')
    use_df = df or (';' in t and nb in (30, 60))
    return TC.tc_to_samples(t, fps, use_df)


def confidence(score, margin):
    if score is None:
        return 'none'
    if score >= 0.5 and margin >= 2.0:
        return 'strong'
    if score >= 0.35 and margin >= 1.4:
        return 'good'
    return 'weak'


class ClipTools:
    def __init__(self, project, ref=None, log=print, progress=None, cancel=None):
        self.p = project
        self.ref = ref
        self.log = log
        self.progress = progress or (lambda *a: None)
        self.cancel = cancel or (lambda: False)

    # -------------------------------------------------------------- placing by hand
    def set_tc(self, c, start, how, note=''):
        c['manual'] = dict(start=int(start), how=how, note=note)
        c['start'] = int(start)
        c['include'] = True
        for f in ('sync_failed',):
            if f in c['flags']:
                c['flags'].remove(f)
        self.p.analyze()
        return c

    def clear(self, c):
        c['manual'] = None
        c['start'] = c['orig_start']
        c['flags'] = [f for f in c['flags'] if f != 'sync_manual']
        self.p.analyze()
        return c

    # -------------------------------------------------------------- waveform search for one clip
    def find(self, c, against=None):
        """Search the whole day for where clip c's audio matches the other rolls.
        against: None = every other roll; or a roll name. -> dict(candidates=[...], near=...)"""
        p = self.p
        others = [x for x in p.clips if x['include'] and x['roll'] != c['roll'] and x['start'] is not None]
        if not others:
            return dict(error='There’s nothing else with timecode to compare it against yet.')
        an = SyncAnalyzer(p, cache_dir=ENV_CACHE, log=self.log, progress=self.progress, ref=self.ref,
                          cam_check_s=None, cancel=self.cancel)
        if an._ch(c) is None or media.total_audio_channels(c) == 0:
            return dict(error='This clip has no audio to match, so set its timecode by hand.')
        # envelopes: the target plus everything that can act as a bed
        keep = [c] + [x for x in others if against in (None, '', 'auto', x['roll'])]
        saved = {x['id']: x['include'] for x in p.clips}
        try:
            for x in p.clips:
                x['include'] = x in keep
            an.envelopes()
        finally:
            for x in p.clips:
                x['include'] = saved[x['id']]
        if c['id'] not in an.env:
            return dict(error='Couldn’t read this clip’s audio.')
        # beds from everything that has a position (own TC, earlier fixes, or set by hand)
        placed = {x['id']: x['start'] for x in keep if x['id'] != c['id'] and x['start'] is not None}
        an.build_beds(trusted_rolls=set(), corrected=placed)
        rolls = [r for r in an.beds if r != c['roll'] and against in (None, '', 'auto', r)]
        cands = []
        for r in rolls:
            m = an.measure(c, r)
            if m and m['score'] > 0.15:
                cands.append(m)
        near = None
        claimed = c['manual']['start'] if c.get('manual') else c['orig_start']
        if claimed is not None:
            for r in rolls:
                m = an.measure(c, r, claimed, window_s=120)
                if m and (near is None or m['score'] > near['score']):
                    m['start'] = claimed + m['offset']
                    near = m
        cands.sort(key=lambda m: -m['score'])
        # merge candidates that agree (several rolls pointing to the same spot)
        merged = []
        for m in cands:
            for g in merged:
                if abs(g['start'] - m['start']) < 0.1 * TC.SR:
                    g['rolls'].append(m['roll'])
                    break
            else:
                merged.append(dict(start=m['start'], score=m['score'], margin=m['margin'], roll=m['roll'], rolls=[m['roll']]))
        out = []
        env, off = an.env[c['id']]
        for g in merged[:4]:
            st = g['start']
            fine = None
            cand = an._refine_candidate(c, int(st), g['roll'])
            if cand is not None:
                try:
                    d, cc = refine(c, int(st), env, cand, cand_channel=an._ch(cand) or 0, target_channel=an._ch(c) or 0,
                                   target_off=off)
                    if cc >= 0.2:
                        st = st + d
                        fine = dict(cc=round(cc, 2), vs=cand['name'])
                except Exception as e:  # noqa: BLE001
                    self.log(f'  fine sync failed: {e}')
            out.append(self._describe(c, int(round(st)), g['score'], g['margin'], g['rolls'], fine))
        res = dict(candidates=out)
        if near is not None:
            res['near'] = self._describe(c, int(round(near['start'])), near['score'], near['margin'], [near['roll']], None)
        return res

    def _describe(self, c, start, score, margin, rolls, fine):
        fps, df = self.p.fps, self.p.df
        cur = c['start']
        return dict(start=start, tc=TC.samples_to_tc(start, fps, df), score=round(float(score), 2),
                    margin=round(float(margin), 2), rolls=rolls, confidence=confidence(score, margin),
                    fine=fine, shift=None if cur is None else int(start - cur),
                    shift_s=None if cur is None else round((start - cur) / TC.SR, 3))

    # -------------------------------------------------------------- write a corrected copy right now
    def stamp_now(self, c, out_dir):
        fps, df = self.p.fps, self.p.df
        if c['start'] is None:
            raise ValueError('Give the clip a timecode first.')
        fixed = os.path.join(out_dir, 'TC_FIXED', c['roll'])
        if c['kind'] == 'audio':
            path, st = ST.stamp_wav(c, os.path.join(fixed, c.get('out_name', c['name'])), c['start'], fps, df,
                                    note='Midwig Sync: timecode set by hand')
            return path
        if c.get('raw_picture'):
            raise ValueError('RAW camera files can’t be rewritten. The XML places this clip at the new timecode instead.')
        path, frames = ST.stamp_video(c, os.path.join(fixed, c['name']), c['start'], fps, df)
        return path
