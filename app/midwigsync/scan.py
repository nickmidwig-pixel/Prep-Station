"""Turn dropped files into clips grouped by roll (camera card / recorder track), with flags."""
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import numpy as np

from . import tc as TC
from . import media
from .bwf import WavInfo

ZAX_TOKEN = re.compile(r'_(\d{1,2}[A-Za-z][A-Za-z0-9]*)_\d{2,4}$')
GOPRO = re.compile(r'^(?:(G[HXL])(\d{2})(\d{4})|GOPR(\d{4})|GP(\d{2})(\d{4}))\.(mp4|MP4)$')
CONVERT_DIR = os.path.expanduser('~/Library/Caches/Midwig Sync/converted')


def gopro_chapter(name):
    """GX010123.MP4 -> ('GX0123', 1); GOPR0123.MP4 -> ('GP0123', 1); GP010123.MP4 -> ('GP0123', 2)."""
    m = GOPRO.match(name)
    if not m:
        return None
    if m.group(1):
        return f'{m.group(1)}{m.group(3)}', int(m.group(2))
    if m.group(4):
        return f'GP{m.group(4)}', 1
    return f'GP{m.group(6)}', int(m.group(5)) + 1


def card_roll(path):
    """Camera roll name = the card/folder that holds the camera's folder structure."""
    parts = os.path.normpath(path).split(os.sep)
    for marker in ('XDROOT', 'PRIVATE', 'CONTENTS', 'DCIM', 'CLIPS001', 'M4ROOT', 'CLIPS002', 'BRAW', 'NIKON'):
        if marker in parts:
            i = parts.index(marker)
            if i >= 1 and parts[i - 1] not in ('', 'Volumes'):
                return parts[i - 1]
    return parts[-2] if len(parts) >= 2 else 'CAM'


def audio_roll(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    folder = os.path.basename(os.path.dirname(path))
    m = ZAX_TOKEN.search(stem)
    if re.match(r'^\d{1,2}[A-Za-z]', folder or ''):
        return folder
    if m:
        return m.group(1).upper()
    return folder or 'AUDIO'


def track_number(roll):
    m = re.match(r'^(\d{1,3})', roll)
    return int(m.group(1)) if m else None


class Project:
    def __init__(self):
        self.clips = []
        self.unsupported = []      # dict(path, why)
        self.errors = []
        self.fps = TC.parse_rate('23.976')
        self.df = False

    # ------------------------------------------------------------ scanning
    def add_paths(self, paths, progress=None):
        files, unsup = media.find_media(paths)
        known = {os.path.realpath(c.get('source_path') or c['path']) for c in self.clips}
        files = [f for f in files if os.path.realpath(f) not in known]
        have = {u['path'] for u in self.unsupported}
        for u in unsup:
            if u not in have:
                self.unsupported.append(dict(path=u, why=media.WHY.get(os.path.splitext(u)[1].lower(),
                                                                           'This file type can\'t be read.')))
        done = [0]

        def one(p):
            try:
                info = media.probe(p, convert_dir=CONVERT_DIR)
            except Exception as e:  # noqa: BLE001
                ext = os.path.splitext(p)[1].lower()
                if ext in media.RAW_TRY_EXT:
                    self.unsupported.append(dict(path=p, why=str(e)))
                else:
                    self.errors.append((p, str(e)))
                info = None
            done[0] += 1
            if progress:
                progress(done[0], len(files), os.path.basename(p))
            return info

        with ThreadPoolExecutor(max_workers=6) as ex:
            infos = list(ex.map(one, files))
        next_id = max([c['id'] for c in self.clips], default=0) + 1
        new = []
        for info in infos:
            if not info:
                continue
            c = dict(info)
            c['id'] = next_id
            next_id += 1
            c['name'] = os.path.basename(c.get('source_path') or c['path'])
            if c.get('source_path'):
                c['out_name'] = os.path.basename(c['path'])
                c['roll'] = audio_roll(c['source_path'])
            elif c['kind'] == 'audio':
                c['roll'] = audio_roll(c['path'])
            else:
                c['roll'] = card_roll(c['path'])
            c['flags'] = []
            c['include'] = True
            c['orig_start'] = c['start']
            c['sync'] = None   # waveform-sync result
            c['manual'] = None
            self.clips.append(c)
            new.append(c)
        self.chain_chapters(new)
        self.analyze()
        return len(files)

    def chain_chapters(self, clips):
        """GoPro (and similar) split long takes into chapters. A chapter without its own timecode
        starts where the previous one ended."""
        groups = {}
        for c in clips:
            g = gopro_chapter(c['name']) if c['kind'] == 'video' else None
            if g:
                c['chapter'] = g[1]
                groups.setdefault((os.path.dirname(c['path']), g[0]), []).append(c)
        for (_, key), cl in groups.items():
            cl.sort(key=lambda c: c['chapter'])
            for a, b in zip(cl, cl[1:]):
                b['chapter_of'] = key
                a['chapter_of'] = key
                if b['start'] is None and a['start'] is not None and a['length']:
                    b['start'] = b['orig_start'] = a['start'] + a['length']
                    b['tc_source'] = f'follows chapter {a["chapter"]}'

    def remove(self, ids):
        ids = set(ids)
        self.clips = [c for c in self.clips if c['id'] not in ids]
        self.analyze()

    def clear(self):
        self.__init__()

    # ------------------------------------------------------------ analysis
    def rolls(self):
        out = {}
        for c in self.clips:
            r = out.setdefault(c['roll'], dict(name=c['roll'], kind='camera' if c['kind'] == 'video' else 'audio',
                                               clips=[], camera='', track_no=None, channels=0))
            r['clips'].append(c)
        for r in out.values():
            if r['kind'] == 'camera':
                cams = Counter(c.get('camera') or '' for c in r['clips'])
                r['camera'] = cams.most_common(1)[0][0]
            else:
                r['track_no'] = track_number(r['name'])
                r['channels'] = max(c['channels'] for c in r['clips'])
            r['clips'].sort(key=lambda c: (c['start'] if c['start'] is not None else 1 << 62, c['name']))
        return out

    def audio_rolls_ordered(self):
        rs = [r for r in self.rolls().values() if r['kind'] == 'audio']
        return sorted(rs, key=lambda r: (r['track_no'] if r['track_no'] is not None else 999, r['name']))

    def camera_rolls(self):
        return sorted([r for r in self.rolls().values() if r['kind'] == 'camera'], key=lambda r: r['name'])

    def analyze(self):
        vids = [c for c in self.clips if c['kind'] == 'video' and c.get('fps')]
        if vids:
            self.fps = Counter(c['fps'] for c in vids).most_common(1)[0][0]
            self.df = Counter(bool(c['df']) for c in vids).most_common(1)[0][0]
        else:
            rates = [c['fps'] for c in self.clips if c.get('fps')]
            if rates:
                self.fps = Counter(rates).most_common(1)[0][0]
        for c in self.clips:
            keep_sync = [f for f in c['flags'] if f.startswith('sync')]
            c['flags'] = keep_sync
            if c.get('manual'):
                c['start'] = c['manual']['start']
            if c['kind'] == 'audio' and not c.get('fps'):
                c['fps_assumed'] = True
        starts = [c['start'] for c in self.clips if c['start'] is not None and c['start'] > TC.SR]
        med = float(np.median(starts)) if starts else None
        ubits = Counter(c.get('ubits') for c in self.clips if c['kind'] == 'audio' and c.get('ubits'))
        main_ub = ubits.most_common(1)[0][0] if ubits else None
        for c in self.clips:
            f = c['flags']
            if c['orig_start'] is None:
                f.append('no_tc')
            elif c['orig_start'] < TC.SR:
                f.append('zero_tc')
            elif med is not None and abs(c['orig_start'] - med) > 8 * 3600 * TC.SR:
                f.append('tc_far')
            if c['kind'] == 'audio' and c['sr'] != TC.SR:
                f.append('sample_rate')
            if c['kind'] == 'video' and c.get('raw_picture'):
                f.append('raw')
            if c['kind'] == 'video' and c.get('fps') and c['fps'] != self.fps:
                f.append('fps_mismatch')
            if c['kind'] == 'video' and not c.get('audio_streams'):
                f.append('no_audio')
            if c.get('ltc'):
                f.append('ltc')
            if c.get('converted'):
                f.append('converted')
            if c.get('manual') and 'sync_manual' not in f:
                f.append('sync_manual')
            if main_ub and c['kind'] == 'audio' and c.get('ubits') and c['ubits'] != main_ub:
                f.append('ubits')
        # overlaps within a roll (TC jump / reset)
        for r in self.rolls().values():
            cl = [c for c in r['clips'] if c['start'] is not None]
            cl.sort(key=lambda c: c['start'])
            for a, b in zip(cl, cl[1:]):
                if b['start'] < a['start'] + a['length'] - TC.SR // 2:
                    for c in (a, b):
                        if 'tc_overlap' not in c['flags']:
                            c['flags'].append('tc_overlap')

    def check_short_clips(self, max_len_s=20, quiet_db=-40.0):
        """Mark very short accidental audio clips (lav auto-record on power-up) as excluded."""
        for c in self.clips:
            if c['kind'] != 'audio' or c['length'] > max_len_s * TC.SR or c.get('manual'):
                continue
            try:
                w = WavInfo(c['path'])
                x = w.read(0, w.nframes)
                x = x.mean(1) if x.ndim == 2 else x
                sec = max(1, w.sr // 2)
                n = len(x) // sec
                lvl = -120.0
                if n:
                    r = np.sqrt((x[:n * sec].reshape(n, sec) ** 2).mean(1) + 1e-20)
                    lvl = float(20 * np.log10(r.max()))
            except Exception:  # noqa: BLE001
                lvl = -120.0
            c['peak_rms_db'] = round(lvl, 1)
            if c['length'] < 2 * TC.SR or lvl < quiet_db:
                if 'short_silent' not in c['flags']:
                    c['flags'].append('short_silent')
                c['include'] = False

    # ------------------------------------------------------------ helpers
    def day_span(self, include_only=True):
        cl = [c for c in self.clips if c['start'] is not None and (c['include'] or not include_only)]
        if not cl:
            return None
        return min(c['start'] for c in cl), max(c['start'] + c['length'] for c in cl)

    def by_id(self, cid):
        for c in self.clips:
            if c['id'] == cid:
                return c
        return None

    def default_reference(self):
        """FX6 (or the camera roll with the most coverage) channel 1."""
        cams = self.camera_rolls()
        if not cams:
            return None
        def score(r):
            cover = sum(c['length'] for c in r['clips'] if c['start'] is not None and c.get('audio_streams'))
            return (r['camera'] == 'FX6', cover)
        best = max(cams, key=score)
        ch = 0
        for c in best['clips']:
            if c.get('ltc') and c['ltc']['channel'] == 0 and c.get('listen_ch') is not None:
                ch = c['listen_ch']
                break
        return dict(roll=best['name'], channel=ch)

    def to_json(self):
        rolls = self.rolls()
        out_clips = []
        for c in self.clips:
            fps = c.get('fps') or self.fps
            out_clips.append(dict(
                id=c['id'], name=c['name'], path=c['path'], kind=c['kind'], roll=c['roll'], start=c['start'],
                orig_start=c['orig_start'],
                length=c['length'], include=c['include'], flags=c['flags'],
                tc=TC.samples_to_tc(c['start'], fps, c.get('df', False)) if c['start'] is not None else None,
                orig_tc=TC.samples_to_tc(c['orig_start'], fps, c.get('df', False)) if c['orig_start'] is not None else None,
                dur=TC.fmt_dur(c['length']), camera=c.get('camera', ''), channels=media.total_audio_channels(c),
                sync=c.get('sync'), fps=TC.rate_label(fps) if fps else '',
                tc_source=c.get('tc_source'), ltc=c.get('ltc'), manual=c.get('manual'), converted=c.get('converted'),
                codec=c.get('codec', ''), chapter=c.get('chapter') if c.get('chapter_of') else None,
                chapter_of=c.get('chapter_of'), audio_from=os.path.basename(c['audio_path']) if c.get('audio_path') else None,
                has_audio=media.total_audio_channels(c) > 0 and not (c.get('ltc') and c.get('listen_ch') is None),
                source_path=c.get('source_path'), opatom=c.get('opatom', False),
            ))
        return dict(
            fps=TC.rate_label(self.fps), df=self.df, clips=out_clips,
            rolls=[dict(name=r['name'], kind=r['kind'], camera=r['camera'], track_no=r['track_no'], n=len(r['clips']))
                   for r in sorted(rolls.values(), key=lambda r: (r['kind'] != 'camera', r['track_no'] or 999, r['name']))],
            span=self.day_span(False), unsupported=self.unsupported, errors=self.errors,
            reference=self.default_reference(),
        )
