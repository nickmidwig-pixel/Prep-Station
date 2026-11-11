"""Turn dropped files into clips grouped by roll (camera card / recorder track), with flags."""
import datetime as dt
import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import numpy as np

from . import tc as TC
from . import media
from . import safety as SAFE
from . import framerate as FR
from .bwf import WavInfo

ZAX_TOKEN = re.compile(r'_(\d{1,2}[A-Za-z][A-Za-z0-9]*)_\d{2,4}$')
GOPRO = re.compile(r'^(?:(G[HXL])(\d{2})(\d{4})|GOPR(\d{4})|GP(\d{2})(\d{4}))\.(mp4|MP4)$')
from .brand import CONVERT_DIR  # noqa: E402


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
    for marker in ('XDROOT', 'PRIVATE', 'CONTENTS', 'DCIM', 'CLIPS001', 'M4ROOT', 'CLIPS002', 'BRAW', 'NIKON', 'CRM',
                   'XFVC'):     # XFVC: Canon C80/C400 XF-AVC S / XF-HEVC S (.MP4) folder, beside CONTENTS and CRM
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


NIGHT_HOURS = 5          # recordings before 05:00 belong to the night before


def _local(ts):
    """ISO time string (UTC 'Z', with offset, or naive local) -> naive local datetime, or None."""
    if not ts:
        return None
    t = str(ts).strip().replace(' ', 'T')
    try:
        if t.endswith('Z'):
            d = dt.datetime.fromisoformat(t[:-1].split('.')[0]).replace(tzinfo=dt.timezone.utc)
            return d.astimezone().replace(tzinfo=None)
        d = dt.datetime.fromisoformat(t.split('.')[0] if '+' not in t[10:] and '-' not in t[10:] else t)
        if d.tzinfo is not None:
            d = d.astimezone().replace(tzinfo=None)
        return d
    except ValueError:
        return None


def shoot_day(c):
    """-> (shoot day 'YYYY-MM-DD' or None, trusted). Trusted: the sound recorder's user-bits or bext date, or a
    recording date the camera wrote into the file. A file's modified date (when it was copied) isn't trusted."""
    tc_h = (c['orig_start'] / TC.SR / 3600.0) if c.get('orig_start') is not None else None
    if c['kind'] == 'audio':
        ub = c.get('ubits') or ''
        if len(ub) >= 8 and ub[:8].isdigit() and ub[:2] == '20':
            try:
                d = dt.date(int(ub[:4]), int(ub[4:6]), int(ub[6:8]))
                if tc_h is not None and tc_h < NIGHT_HOURS:
                    # after midnight: some recorders roll their date at 00:00, some keep the day's date
                    # (Zaxcom names and stamps 00:21 as the evening's date). assign_days settles it.
                    c['day_alt'] = d.isoformat()
                    d -= dt.timedelta(days=1)
                return d.isoformat(), True
            except ValueError:
                pass
        bd = c.get('bext_date') or ''
        if re.match(r'^20\d\d[-:/]\d\d[-:/]\d\d$', bd):
            try:
                d = dt.datetime.strptime(bd.replace(':', '-').replace('/', '-') + ' ' +
                                         (c.get('bext_time') or '12:00:00')[:8].replace('-', ':'), '%Y-%m-%d %H:%M:%S')
                return (d - dt.timedelta(hours=NIGHT_HOURS)).date().isoformat(), True
            except ValueError:
                pass
    d = _local(c.get('created'))
    if d is None or d.year < 2000:
        return None, False
    return (d - dt.timedelta(hours=NIGHT_HOURS)).date().isoformat(), bool(c.get('created_meta'))


_CANON_NAME_TIME = re.compile(r'C\d{3}[A-Z](\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})[0-9A-Z]{2}_CANON', re.I)


def _name_start(c):
    """The record-start clock a camera wrote into the file name, as a local datetime, or None.
    Canon C80 / C400 / R5 C: B_0001C015A250209_001152HN_CANON = 2025-02-09 00:11:52. (Their XF-AVC .MXF files
    carry the END of the take as creation time, the RAW and MP4 files the start, so the name is the one stamp
    that always means the same thing.)"""
    m = _CANON_NAME_TIME.search(c.get('name') or os.path.basename(c.get('path') or ''))
    if not m:
        return None
    yy, mo, dd, hh, mi, ss = (int(x) for x in m.groups())
    try:
        return dt.datetime(2000 + yy, mo, dd, hh, mi, ss)
    except ValueError:
        return None


def _clock_start(c):
    """The camera's clock at the start of the clip (local), or None."""
    d = _name_start(c)
    if d is None and c.get('created_meta'):
        d = _local(c.get('created'))
    if d is None or d.year < 2000:
        return None
    return d


def _clock_samples(c):
    """The camera's clock at the start of the clip, as samples since midnight (local), or None."""
    d = _clock_start(c)
    if d is None:
        return None
    return int((d.hour * 3600 + d.minute * 60 + d.second) * TC.SR)


def _recording_span(c):
    """Seconds the take really took: from the start the camera wrote (file name, else creation stamp) to the end
    (creation stamp when the camera stamps the end, as Canon XF-AVC does, else the file's last write). None when
    nothing usable survived the copy (end before start, or more than a day apart)."""
    start = _clock_start(c)
    if start is None:
        return None
    ends = []
    if _name_start(c) is not None and c.get('created_meta'):
        ends.append(_local(c.get('created')))
    try:
        ends.append(dt.datetime.fromtimestamp(os.path.getmtime(c['path'])))
    except OSError:
        pass
    for end in ends:
        if end is None:
            continue
        span = (end - start).total_seconds()
        if span < 3 and end is not ends[-1]:
            continue                  # that stamp is the start too (Canon MP4 / RAW), not the end: use the last write
        if 0 < span < 86400:
            return span
    return None


_NOMINAL = (24, 25, 30, 48, 50, 60, 72, 75, 90, 96, 100, 110, 120, 144, 150, 160, 180, 200, 240, 300)


def _shot_rate(play_fps, speed):
    """The rate a clip was shot at, from its playback rate and how much slower it plays, as a cameraman says it:
    23.976 played 5x slower -> '120'. Snaps to the nearest common rate when close, else '~N'."""
    try:
        from fractions import Fraction
        f = float(Fraction(str(play_fps))) / max(1e-6, float(speed))
    except (TypeError, ValueError, ZeroDivisionError):
        return '?'
    best = min(_NOMINAL, key=lambda n: abs(n - f) / n)
    return str(best) if abs(best - f) / best < 0.08 else f'~{f:.0f}'


def _mmss(sec):
    sec = int(sec)                      # floor, the same as the clip list's Length column
    return f'{sec // 60}:{sec % 60:02d}' if sec >= 60 else f'{sec} s'


_FOLDER_DATE_RES = (re.compile(r'(?<!\d)(20\d{2})[-_. ]?(\d{2})[-_. ]?(\d{2})(?!\d)'),    # 20250208, 2025-02-08
                    re.compile(r'(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)'))                     # 250208 / 261106


def folder_date(name):
    """A shoot date written into a folder name, as 'YYYY-MM-DD', or None."""
    for i, rx in enumerate(_FOLDER_DATE_RES):
        for m in rx.finditer(name or ''):
            y, mo, d = (int(x) for x in m.groups())
            if i == 1:
                if not 20 <= y <= 39:
                    continue
                y += 2000
            try:
                return dt.date(y, mo, d).isoformat()
            except ValueError:
                continue
    return None


def track_number(roll):
    m = re.match(r'^(\d{1,3})', roll)
    return int(m.group(1)) if m else None


_LETTER_RES = (re.compile(r'^([A-Z])(\d{3})C\d{3}'),           # A001C003 (ARRI, Sony, Canon XF, RED)
               re.compile(r'^([A-Z])_(\d{4})C\d{3}'),          # C_0001C002 (Canon C80 / C400 / R5 C)
               re.compile(r'^([A-Z])(\d{3})_C?\d{3}'),         # A002_C001
               re.compile(r'(?:^|[_ -])([A-Z])(\d{3})(?:$|[_ -])'))
_CARD_LETTER_RE = re.compile(r'[_ -]([A-Z])[_ -]\d{1,3}$')     # UDP_261106_C80_B_001: the letter you gave the card


def camera_letter(c):
    """Camera letter: first the one written into the card folder's name (UDP_261106_C80_B_001 -> B), since that's
    the letter the camera had that day; then the camera's own clip naming (A001C003, C_0001C002, A002_C001, a card
    called FX6_A001), or None."""
    path = c.get('source_path') or c['path']
    dirs = []
    d = os.path.dirname(path)
    for _ in range(4):                       # CONTENTS/CLIPS001, PRIVATE/M4ROOT/CLIP... sit under the card folder
        b = os.path.basename(d)
        if not b:
            break
        dirs.append(b)
        d = os.path.dirname(d)
    for s in dirs + [c.get('roll') or '']:
        m = _CARD_LETTER_RE.search(s)
        if m:
            return m.group(1)
    for s in [c.get('name') or ''] + dirs[:1] + [c.get('roll') or '']:
        s = os.path.splitext(s)[0]
        for rx in _LETTER_RES:
            m = rx.search(s)
            if m:
                return m.group(1)
    return None


class Project:
    def __init__(self):
        self.clips = []
        self.unsupported = []      # dict(path, why)
        self.errors = []
        self.fps = TC.parse_rate('23.976')
        self.df = False
        self.dropped = []          # what you dropped (folders/files), for where the output goes
        self.fps_override = None   # '25', '29.97 DF'... when you set the project rate yourself
        self.reports = []          # sound reports found next to the media (frame rate evidence)
        self.lane_user = {}        # card/roll name -> camera letter you gave it (⌘G grouping, relettering)
        self.fps_info = FR.assess(self)

    # ------------------------------------------------------------ scanning
    def add_paths(self, paths, progress=None):
        SAFE.install()
        for p in paths:
            ap = os.path.abspath(p)
            if ap not in self.dropped:
                self.dropped.append(ap)
            SAFE.protect_root(p)
            if os.path.isfile(p):
                SAFE.protect_root(os.path.dirname(os.path.abspath(p)))
        files, unsup = media.find_media(paths)
        for f in files:
            SAFE.protect_file(f)
        have_rep = {r['path'] for r in self.reports}
        for rp in FR.find_reports(paths):
            if rp not in have_rep:
                r = FR.read_report(rp)
                if r:
                    self.reports.append(r)
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
            SAFE.protect_clip(c)
            c['id'] = next_id
            next_id += 1
            c['name'] = os.path.basename(c.get('source_path') or c['path'])
            try:
                c['size'] = os.path.getsize(c.get('source_path') or c['path'])
            except OSError:
                c['size'] = 0
            if c.get('source_path'):
                c['out_name'] = os.path.basename(c['path'])
                c['roll'] = audio_roll(c['source_path'])
            elif c['kind'] == 'audio':
                c['roll'] = audio_roll(c['path'])
            else:
                c['roll'] = card_roll(c['path'])
            c['flags'] = []
            c['include'] = True
            if any(x.upper() == 'FALSETAKES' for x in os.path.normpath(c.get('source_path') or c['path']).split(os.sep)):
                c['false_take'] = True     # the recorder's False Take folder: shown, left out unless you add it
                c['include'] = False
            c['orig_start'] = c['start']
            c['sync'] = None   # waveform-sync result
            c['manual'] = None
            self.clips.append(c)
            new.append(c)
        self.chain_chapters(new)
        self.mark_duplicates(new)
        self.assign_days()
        self.analyze()
        return len(files)

    def mark_duplicates(self, new):
        """The same clip dropped twice (e.g. a card and its backup): keep the first, leave the rest out."""
        seen = {}
        for c in self.clips:
            if c.get('duplicate_of'):
                continue
            try:
                size = os.path.getsize(c.get('source_path') or c['path'])
            except OSError:
                size = None
            # a Finder / Photos copy is the same clip: "IMG_8602 2.MOV", "IMG_8602 copy.MOV", "IMG_8602 (1).MOV"
            base = re.sub(r'(?: \d+| copy(?: \d+)?| \(\d+\))(?=\.[^.]+$)', '', c['name']).lower()
            key = (base, size, c['orig_start'], c['length'])
            o = seen.get(key)
            if o is not None and o is not c:
                if c in new or o in new:
                    # keep the one with the plain name; the copy is the duplicate
                    keep, dup = (c, o) if c['name'].lower() == base and o['name'].lower() != base and o in new else (o, c)
                    dup['duplicate_of'] = keep['path']
                    dup['include'] = False
                    keep.pop('duplicate_of', None)
                    seen[key] = keep
            else:
                seen.setdefault(key, c)

    def assign_days(self):
        """Group clips into shoot days, and unwrap timecode that runs past midnight within a day."""
        H = TC.SR * 3600
        for c in self.clips:
            c.pop('day_alt', None)
            c.pop('day_forced', None)
            c['day'], c['day_trusted'] = shoot_day(c)
            c.pop('date_note', None)
        # the folder you dropped is the master date: "20250208", "UDP_261106_C80" or a dated folder under the drop.
        # Camera clocks can't be trusted (rental bodies, a C500 II set to 2019); the folder you made can.
        for c in self.clips:
            fd = self._folder_day(c)
            if not fd:
                continue
            day, folder = fd
            if c['day'] != day:
                c['date_note'] = (f"camera date says {c['day']}" if c['day'] and c['day_trusted'] and c['kind'] != 'audio'
                                  else f"recorder date says {c['day']}" if c['day'] and c['day_trusted']
                                  else f'dated by the folder {folder}')
            c['day'], c['day_trusted'], c['day_forced'] = day, True, folder
        strong = [c for c in self.clips if c['day'] and c['day_trusted']]
        days = sorted({c['day'] for c in strong})

        def span_of(d):
            st = [(c['orig_start'], c['orig_start'] + c['length']) for c in strong if c['day'] == d and c['orig_start'] is not None]
            return (min(a for a, _ in st), max(b for _, b in st)) if st else None

        def date_of(d):
            try:
                return dt.date.fromisoformat(d)
            except ValueError:
                return None
        # a date that is really part of another day:
        #   - a camera whose clock date is off (a C500 II that says 2019): only camera clips carry that date, no sound
        #     recorder agrees with it, and the date sits apart from the other days (not the day before or after,
        #     which could be a real second shoot day); or a single small roll alone on its date
        #   - sound recorded after midnight whose recorder kept the evening's date
        #   either way its timecode has to fall inside exactly one other day
        if len(days) > 1:
            for d in list(days):
                cl = [c for c in strong if c['day'] == d]
                if not cl or any(c.get('day_forced') for c in cl):
                    continue
                cams_only = all(c['kind'] != 'audio' for c in cl)
                night_audio = all(c['kind'] == 'audio' and c.get('day_alt') for c in cl)
                lone_roll = len({c['roll'] for c in cl}) == 1 and len(cl) <= 0.25 * len(strong)
                dd = date_of(d)
                apart = dd is not None and all(abs((date_of(e) - dd).days) > 1 for e in days if e != d and date_of(e))
                if not ((cams_only and (apart or lone_roll)) or night_audio or lone_roll):
                    continue
                st = [c['orig_start'] for c in cl if c['orig_start'] is not None]
                if not st:
                    continue
                hits = [e for e in days if e != d and span_of(e) and span_of(e)[0] - H <= min(st) and max(st) <= span_of(e)[1] + H]
                if night_audio and not hits:
                    hits = [e for e in days if e != d and e in {c.get('day_alt') for c in cl}]
                if len(hits) == 1:
                    for c in cl:
                        c['day'] = hits[0]
                        if c['kind'] == 'audio':
                            c['date_note'] = 'recorded after midnight'
                        else:
                            c['date_note'] = f'camera date says {d}'
                    days = sorted({c['day'] for c in strong})
            days = sorted({c['day'] for c in strong})
        # everything else: the trusted day whose timecode range it falls in; else its own date; else the biggest day
        if days:
            spans = {d: span_of(d) for d in days}
            big = Counter(c['day'] for c in strong).most_common(1)[0][0]
            for c in self.clips:
                if c['day'] and c['day_trusted']:
                    continue
                own = c['day']
                pick = None
                if c['orig_start'] is not None:
                    for d in ([own] if own in spans else []) + days:
                        sp = spans.get(d)
                        if sp and sp[0] - H / 2 <= c['orig_start'] <= sp[1] + H / 2:
                            pick = d
                            break
                if pick is None:
                    pick = own if own in spans else big
                if pick != own:
                    c['date_note'] = ('grouped by its timecode' if c['orig_start'] is not None
                                      else 'no recording date or timecode: grouped with the main day')
                c['day'] = pick
        # one drop with no date in its name = one day ("I put the whole day in one folder"): whatever the clocks say
        if len(self.dropped) == 1 and not any(c.get('day_forced') for c in self.clips):
            cnt = Counter(c['day'] for c in self.clips if c['day'] and c['day_trusted'])
            if len({c['day'] for c in self.clips if c['day']}) > 1 and cnt:
                main = cnt.most_common(1)[0][0]
                for c in self.clips:
                    if c['day'] != main:
                        c['date_note'] = c.get('date_note') or (f"camera date says {c['day']}" if c['day'] and c['kind'] != 'audio'
                                                                else 'grouped with the day you dropped')
                        c['day'] = main
        # midnight: a day with clips both late evening and early morning -> the early ones are after midnight
        for d in {c['day'] for c in self.clips}:
            cl = [c for c in self.clips if c['day'] == d and c['orig_start'] is not None]
            late = any(c['orig_start'] >= 18 * H for c in cl)
            early = any(c['orig_start'] < 6 * H and not c.get('after_midnight') for c in cl)
            if late and early:
                for c in cl:
                    if c['orig_start'] < 12 * H and not c.get('after_midnight'):
                        add = TC.day_samples(c.get('fps') or self.fps)
                        c['orig_start'] += add
                        if c['start'] is not None:
                            c['start'] += add
                        if c.get('manual'):
                            c['manual']['start'] += add
                        c['after_midnight'] = True

    def _folder_day(self, c):
        """(date, folder name) from the dropped folder holding this clip, or the shallowest dated folder between
        the drop and the file; None when no folder on that path carries a date."""
        path = os.path.normpath(c.get('source_path') or c['path'])
        roots = sorted((os.path.normpath(r) for r in self.dropped), key=len, reverse=True)
        root = next((r for r in roots if path == r or path.startswith(r + os.sep)), None)
        if root is None:
            return None
        if os.path.isfile(root):
            root = os.path.dirname(root)
        rel = os.path.dirname(os.path.relpath(path, root))
        chain = [os.path.basename(root)] + [x for x in rel.split(os.sep) if x and x != '.']
        for name in chain:
            d = folder_date(name)
            if d:
                return d, name
        return None

    def days(self):
        cnt = defaultdict(lambda: dict(n=0, rolls=set()))
        for c in self.clips:
            k = c.get('day') or 'unknown'
            cnt[k]['n'] += 1
            cnt[k]['rolls'].add(c['roll'])
        out = []
        for k in sorted(cnt):
            try:
                label = dt.date.fromisoformat(k).strftime('%a %b %-d')
            except ValueError:
                label = 'No date'
            out.append(dict(key=k, label=label, n=cnt[k]['n'], rolls=sorted(cnt[k]['rolls'])))
        return out

    def _ltc_follow_rate(self):
        """LTC can't tell 23.976 from 24 (or 29.97 from 30). Files that got their rate only from LTC follow the
        project rate, so their start is right when the project is 24 or 30."""
        from . import ltc as LTC
        for c in self.clips:
            l = c.get('ltc')
            if not l or l.get('rate_from') != 'guess' or not l.get('anchor') or not str(c.get('tc_source', '')).startswith('LTC'):
                continue
            lf = TC.parse_rate(l['fps'])
            if TC.nominal(lf) != TC.nominal(self.fps) or lf == self.fps:
                continue
            new = LTC.start_at_rate(l['anchor'], self.fps, bool(c.get('df'))) - c.get('_audio_offset', 0)
            shift = new - c['orig_start'] if c.get('orig_start') is not None else 0
            c['orig_start'] = new
            if c.get('start') is not None and not c.get('manual'):
                c['start'] += shift
            c['fps'] = self.fps
            l['fps'] = TC.rate_label(self.fps)

    def set_fps(self, choice):
        """'auto' (or None) = let the day decide; otherwise e.g. '25' or '29.97 DF'."""
        self.fps_override = None if choice in (None, '', 'auto') else choice
        self.analyze()

    def dropped_for(self, clips):
        """The dropped folders that hold these clips (so each shoot day's output sits with its own media)."""
        def under(path, root):
            return path == root or path.startswith(root.rstrip(os.sep) + os.sep)
        paths = [os.path.abspath(c.get('source_path') or c['path']) for c in clips]
        got = [r for r in self.dropped if any(under(x, r) for x in paths)]
        return got or list(self.dropped)

    def subset(self, day):
        """A project holding only one day's clips (the clip dicts are shared, so results show in the window)."""
        p = Project()
        p.clips = [c for c in self.clips if (c.get('day') or 'unknown') == day]
        p.unsupported = self.unsupported
        p.fps_override, p.reports = self.fps_override, self.reports
        p.dropped = self.dropped_for(p.clips)
        p.analyze()
        return p

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

    def lanes(self):
        """Timeline lanes like Tentacle Sync Studio: one lane per camera body (every card it shot: A CAM, B CAM...),
        then one per sound track folder. Camera letters come from the clip / card names (A001C003 -> A) when they
        have one, otherwise they're handed out by how much each camera shot (the reference camera first).
        -> [dict(key, label, sub, kind, rolls, role, channels, letter)] and c['lane'] on every clip."""
        rolls = self.rolls()
        ref = (self.default_reference() or {}).get('roll')
        bodies = {}
        for r in rolls.values():
            if r['kind'] != 'camera':
                continue
            letters = Counter(camera_letter(c) for c in r['clips'])
            letters.pop(None, None)
            letter = letters.most_common(1)[0][0] if letters else None
            serial = Counter(c.get('serial') for c in r['clips'] if c.get('serial')).most_common(1)
            model = r['camera'] or ''
            user = (getattr(self, 'lane_user', None) or {}).get(r['name'])
            if user:                           # you said which camera this card is
                letter = user
                key = ('user', user)
            elif serial:
                key = ('serial', model, serial[0][0])
            elif letter:
                key = ('letter', model, letter)
            else:
                key = ('folder', model, r['name'])
            spans = [(c['start'], c['start'] + c['length']) for c in r['clips'] if c['start'] is not None]
            k2, n = key, 1
            while k2 in bodies and any(a0 < b1 - TC.SR and b0 < a1 - TC.SR for a0, a1 in spans for b0, b1 in bodies[k2]['spans']):
                n += 1                         # one camera can't record two clips at once: another body
                k2 = key + (n,)
            key = k2
            b = bodies.setdefault(key, dict(rolls=[], letter=letter, model=model, dur=0, ref=False, spans=[], user=bool(user)))
            b['spans'] += spans
            b['rolls'].append(r['name'])
            b['dur'] += sum(c['length'] for c in r['clips'] if c['include'])
            b['ref'] = b['ref'] or r['name'] == ref
            if letter and not b['letter']:
                b['letter'] = letter
        # bodies with no letter anywhere are lettered by how much they shot: the camera that carried the day comes
        # first, a phone or GoPro with a few clips falls to the end (Nick's rule) — not by which one is the reference
        order = sorted(bodies.values(), key=lambda b: (b['letter'] is None, b['letter'] or '', -b['dur']))
        used, lanes = set(), []
        order.sort(key=lambda b: not b['user'])     # letters you gave win over letters read from names
        for b in order:            # explicit letters first (unless taken by another body), then the rest in order
            if b['letter'] and b['letter'] not in used:
                used.add(b['letter'])
            else:
                b['letter'] = None
        free = (ch for ch in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if ch not in used)
        for b in order:
            if not b['letter']:
                b['letter'] = next(free, '?')
                used.add(b['letter'])
        for b in sorted(order, key=lambda b: b['letter']):
            key = f"cam:{b['letter']}"
            n = len(b['rolls'])
            sub = b['model'] or b['rolls'][0]
            lanes.append(dict(key=key, label=f"{b['letter']} CAM", sub=sub + (f' · {n} cards' if n > 1 else ''),
                              kind='camera', rolls=sorted(b['rolls']), role='cam', channels=0, letter=b['letter'],
                              user=b['user']))
            for rn in b['rolls']:
                for c in rolls[rn]['clips']:
                    c['lane'] = key
        for r in self.audio_rolls_ordered():
            role = self._roll_role(r)
            names = Counter()
            for c in r['clips']:
                for k, v in (c.get('track_names') or {}).items():
                    names[(int(k), v)] += 1
            # one row per track position. A recorder's takes can have different layouts (633: 4, 6 and 8 tracks on
            # one card), so each row lists the names used at that position, and its role is the role of the
            # name most files use there (never a role counted separately from the name).
            chans = []
            layouts = {tuple((c.get('track_names') or {}).get(k + 1) or (c.get('track_names') or {}).get(str(k + 1)) or ''
                             for k in range(c.get('channels', 1))) for c in r['clips']}
            for i in range(r['channels']):
                here = [(v, n) for (k, v), n in names.most_common() if k == i + 1]
                nm = here[0][0] if here else None
                shown = nm
                if len(here) > 1:
                    shown = ' / '.join(v for v, _ in here[:3]) + (' …' if len(here) > 3 else '')
                ch = dict(name=shown or (r['name'] if r['channels'] == 1 else f"{r['name']} {i + 1}"),
                          role=role if r['channels'] == 1 else None)
                if r['channels'] > 1:
                    pick = next((c for c in r['clips'] if ((c.get('track_names') or {}).get(i + 1)
                                                            or (c.get('track_names') or {}).get(str(i + 1))) == nm), None)
                    if pick is None:
                        pick = next((c for c in r['clips'] if c.get('channels', 1) > i), None)
                    roles_here = self.clip_roles(pick) if pick else []
                    ch['role'] = roles_here[i] if i < len(roles_here) else 'lav'
                    seen = {self.clip_roles(c)[i] for c in r['clips'] if c.get('channels', 1) > i}
                    if len(seen) > 1:
                        ch['role'] = 'varies'          # a boom in one take, a mix in another
                chans.append(ch)
            label = chans[0]['name'] if r['channels'] == 1 and chans and chans[0]['name'] != r['name'] else r['name']
            sub = {'multi': f"{r['channels']} tracks", 'mix': 'mix', 'tc': 'timecode'}.get(role, role)
            if role == 'multi' and len(layouts) > 1:
                ns = sorted({len(l) for l in layouts})
                sub = (f"{ns[0]}–{ns[-1]} tracks" if ns[0] != ns[-1] else f"{ns[0]} tracks") + f" · {len(layouts)} layouts"
            if r['channels'] == 1 and label != r['name']:
                sub = f"{role} · {r['name']}"
            key = f"snd:{r['name']}"
            lanes.append(dict(key=key, label=label, sub=sub, kind='audio', rolls=[r['name']], role=role,
                              channels=r['channels'], tracks=chans))
            for c in r['clips']:
                c['lane'] = key
        return lanes

    def _roll_role(self, r):
        if r['kind'] == 'camera':
            return 'cam'
        roles = [self.clip_roles(c) for c in r['clips']]
        if any(len(x) > 1 for x in roles):
            return 'multi'
        flat = Counter(x[0] for x in roles)
        return flat.most_common(1)[0][0] if flat else 'lav'

    def audio_rolls_ordered(self):
        rs = [r for r in self.rolls().values() if r['kind'] == 'audio']
        return sorted(rs, key=lambda r: (r['track_no'] if r['track_no'] is not None else 999, r['name']))

    def camera_rolls(self):
        return sorted([r for r in self.rolls().values() if r['kind'] == 'camera'], key=lambda r: r['name'])

    def analyze(self):
        self.fps_info = FR.assess(self, self.reports, self.fps_override)
        self.fps, self.df = self.fps_info['fps'], self.fps_info['df']
        self._ltc_follow_rate()
        for c in self.clips:
            keep_sync = [f for f in c['flags'] if f.startswith('sync')]
            c['flags'] = keep_sync + (['false_take'] if c.get('false_take') else [])
            if c.get('manual'):
                c['start'] = c['manual']['start']
            if c['kind'] == 'audio' and not c.get('fps'):
                c['fps_assumed'] = True
        med_by_day = {}
        for d in {c.get('day') for c in self.clips}:
            st = [c['orig_start'] for c in self.clips if c.get('day') == d and c['orig_start'] is not None
                  and c['orig_start'] > TC.SR]
            med_by_day[d] = float(np.median(st)) if st else None
        main_ub = {}
        for d in med_by_day:
            ub = Counter(c.get('ubits') for c in self.clips if c.get('day') == d and c['kind'] == 'audio' and c.get('ubits'))
            main_ub[d] = ub.most_common(1)[0][0] if ub else None
        for c in self.clips:
            f = c['flags']
            if c['orig_start'] is None:
                f.append('no_tc')
            elif c['orig_start'] < TC.SR:
                f.append('zero_tc')
            elif med_by_day.get(c.get('day')) is not None and abs(c['orig_start'] - med_by_day[c.get('day')]) > 8 * 3600 * TC.SR:
                f.append('tc_far')
            if c['kind'] == 'audio' and c['sr'] != TC.SR:
                f.append('sample_rate')
            if c['kind'] == 'video' and c.get('raw_picture'):
                f.append('raw')
            if c['kind'] == 'video' and not c.get('audio_streams'):
                f.append('no_audio')
            if c.get('ltc'):
                f.append('ltc')
            if c.get('converted'):
                f.append('converted')
            if c.get('manual') and 'sync_manual' not in f:
                f.append('sync_manual')
            if c.get('duplicate_of'):
                f.append('duplicate')
            mu = main_ub.get(c.get('day'))
            if mu and c['kind'] == 'audio' and c.get('ubits') and c['ubits'] != mu:
                f.append('ubits')
        self._mark_slowmo()
        # overlaps within a roll (TC jump / reset), per shoot day
        for r in self.rolls().values():
            by_day = defaultdict(list)
            for c in r['clips']:
                if c['start'] is not None and not c.get('duplicate_of') and not c.get('slowmo'):
                    by_day[c.get('day')].append(c)
            for cl in by_day.values():
                self._overlaps(cl)

    def _mark_slowmo(self):
        """Slow motion and other frame rates never go on the sync timeline (their sound isn't used either);
        they get their own timeline. c['slowmo'] = dict(kind, why, speed) or None.
          timebase  : recorded at a higher rate than the project (C500 switched to 59.94): conform to slow down
          rate      : a different, normal-speed rate (29.97 on a 23.976 day)
          in_camera : the file says it was shot faster than it plays (Sony S&Q captureFps, sensor fps)
          clock     : shot faster than it plays, by the camera's own clock (see below)
          likely    : runs on past the start of the next clip on the same card
          marked    : you marked it"""
        pf = float(self.fps)
        for c in self.clips:
            c['slowmo'] = None
            if c['kind'] != 'video':
                continue
            user = c.get('slowmo_user')
            if user is False:
                continue
            fps = c.get('fps')
            cap = c.get('capture_fps')
            # anything within 2% of the day's rate (23.5 to 24.5 on a 23.976 day; 30 / 29.97; 60 / 59.94) is the same
            # speed for the edit: real time, not slow motion
            near = bool(fps) and abs(float(fps) / pf - 1) < 0.02     # also an iPhone's wandering 23.84 / 23.92
            if near and TC.rate_label(fps) != TC.rate_label(self.fps) and 'rate_near' not in c['flags']:
                c['flags'].append('rate_near')
            if fps and not near and TC.rate_label(fps) != TC.rate_label(self.fps):
                ratio = float(fps) / pf
                if ratio > 1.5:
                    c['slowmo'] = dict(kind='timebase', speed=round(pf / float(fps), 3), shot=TC.rate_label(fps),
                                       why=f'Shot at {TC.rate_label(fps)} fps. Interpret as {TC.rate_label(self.fps)} '
                                           f'and it plays at {pf / float(fps) * 100:.0f}% speed.')
                else:
                    c['slowmo'] = dict(kind='rate', speed=1.0,
                                       why=f'{TC.rate_label(fps)} fps on a {TC.rate_label(self.fps)} day.')
            elif cap and fps and float(cap) > float(fps) * 1.2:
                c['slowmo'] = dict(kind='in_camera', speed=round(float(fps) / float(cap), 3), shot=TC.rate_label(cap),
                                   why=f'Shot at {TC.rate_label(cap)} fps, plays back slowed down in camera '
                                       f'({float(fps) / float(cap) * 100:.0f}% speed).')
            if user and not c['slowmo']:
                c['slowmo'] = dict(kind='marked', speed=None, why='Marked as slow motion by you.')
        # Slow & fast motion played back at the project rate, with no capture rate in the file (Canon S&F, many
        # others). Two tells that need no camera-specific metadata. Sound is NOT one of them: B-roll, an unplugged
        # input or a timecode-only interview camera are silent and real time (Nick, 11/09).
        #   clock : the camera's own clock says how long the take really took (record start from the clip name or
        #           creation stamp -> end stamp / last write). A clip that plays much longer was shot faster.
        #   next  : the next clip on the same card started sooner (camera clock, else timecode) than this one would
        #           have ended in real time. One camera can't record two clips at once.
        # Both together = certain. One alone = shown as slow motion and asked once.
        rolls = defaultdict(list)
        for c in self.clips:
            if c['kind'] == 'video' and not c.get('duplicate_of'):
                rolls[(c['roll'], c.get('day'))].append(c)
        for cl in rolls.values():
            def key(c):
                st = _clock_start(c)
                return (0, st.timestamp()) if st else (1, c.get('orig_start') or 0)
            cl.sort(key=key)
            for i, c in enumerate(cl):
                if c['slowmo'] or c.get('slowmo_user') is False:
                    continue
                L = c['length'] / TC.SR
                tells, speeds = [], []
                span = _recording_span(c)
                if span is not None and span > 1.0 and L > span * 1.3 + 2:
                    tells.append(('clock', span))
                    speeds.append(span / L)
                nxt = cl[i + 1] if i + 1 < len(cl) else None
                gap = None
                if nxt is not None:
                    s0, s1 = _clock_start(c), _clock_start(nxt)
                    if s0 and s1:
                        gap = (s1 - s0).total_seconds()
                    elif c.get('orig_start') is not None and nxt.get('orig_start') is not None:
                        gap = (nxt['orig_start'] - c['orig_start']) / TC.SR
                if gap is not None and 0 < gap and L > gap + 2:
                    tells.append(('next', gap))
                    if not speeds:
                        speeds.append(gap / L)     # an upper bound: the gap includes the pause between takes
                if not tells:
                    continue
                speed = speeds[0]
                shot = _shot_rate(c.get('fps') or self.fps, speed)
                if tells[0][0] != 'clock':          # only the gap to the next clip: a lower bound on the rate
                    shot = shot.lstrip('~') + '+'
                parts = []
                for t, v in tells:
                    if t == 'clock':
                        parts.append(f'the camera took {_mmss(v)} to record it but it plays {_mmss(L)}')
                    else:
                        parts.append(f'the next clip on this card started {_mmss(v)} later'
                                     + ('' if any(tt == 'clock' for tt, _ in tells) else f', but this one plays {_mmss(L)}'))
                c['slowmo'] = dict(kind='clock' if tells[0][0] == 'clock' else 'likely', speed=round(speed, 3),
                                   shot=shot, tells=[t for t, _ in tells],
                                   why=(f"Shot at about {shot} fps" if not shot.endswith('+') else f"Shot at {shot[:-1]} fps or faster")
                                       + f", plays at {TC.rate_label(c.get('fps') or self.fps)}. "
                                       + '; '.join(parts)[:1].upper() + '; '.join(parts)[1:] + '.')
        # how sure: the file's own rate / capture rate / your word are certain; the clock and timecode tells are
        # strong but inferred, so the window asks you to confirm them once
        for c in self.clips:
            sm = c.get('slowmo')
            if sm:
                sm['sure'] = (bool(c.get('slowmo_user')) or sm['kind'] in ('timebase', 'in_camera', 'rate', 'marked')
                              or len(sm.get('tells') or []) >= 2)

    def sync_view(self):
        """This project without slow motion / other frame rates (the clip dicts are shared)."""
        p = Project()
        p.__dict__.update(self.__dict__)
        p.clips = [c for c in self.clips if not c.get('slowmo')]
        return p

    @staticmethod
    def _overlaps(cl):
        cl.sort(key=lambda c: c['start'])
        for a, b in zip(cl, cl[1:]):
            if b['start'] < a['start'] + a['length'] - TC.SR // 2:
                for c in (a, b):
                    if 'tc_overlap' not in c['flags']:
                        c['flags'].append('tc_overlap')

    def check_short_clips(self, max_len_s=20, quiet_db=-40.0):
        """Leave out accidental records, never delete or rename them. Two kinds:
        - false records: a recorder (Zaxcom set to record on boot) or a camera rolled for a moment before its
          timecode bit: a sound file shorter than max_len_s, or a camera clip under 3 s, with no timecode or 00:00:00:00;
        - short accidental audio that is under 2 s or silent.
        A clip you used or left out by hand keeps your choice."""
        for c in self.clips:
            if c.get('user_include') is not None or c.get('manual'):
                continue
            no_tc = c.get('orig_start') is None or c['orig_start'] < TC.SR
            short = c['length'] < max_len_s * TC.SR
            if c['kind'] == 'audio' and no_tc and short:      # sound only (Nick, 11/11): a short camera clip is a shot
                if 'false_record' not in c['flags']:
                    c['flags'].append('false_record')
                c['include'] = False
                continue
            if c['kind'] != 'audio' or c['length'] > max_len_s * TC.SR:
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

    def _clock_place(self, c):
        """Where a camera clip with no timecode roughly sits, from the time of day its own clock wrote
        (an iPhone's creation date with its time-zone offset). Only for showing it on the timeline before
        a sync; never used as timecode. Samples since midnight, or None."""
        if c['kind'] != 'video' or c.get('orig_start') is not None or c.get('start') is not None:
            return None
        cr = str(c.get('created') or '')
        m = re.match(r'^\d{4}-\d\d-\d\dT(\d\d):(\d\d):(\d\d)(?:\.\d+)?[+-]\d\d:?\d\d$', cr)
        if not m:                       # no local offset (plain UTC): the time of day can't be trusted
            return None
        # as a timecode label at the day's rate, so it sits beside cameras whose timecode reads the same time of day
        return TC.tc_to_samples('%s:%s:%s:00' % m.groups(), self.fps, False)

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

    @staticmethod
    def clip_roles(c):
        """Role of each channel from its name (boom / lav / plant / mix / tc). Cameras: 'cam'."""
        from .refplan import role_of
        if c['kind'] != 'audio':
            return ['cam']
        names = c.get('track_names') or {}
        n = c.get('channels', 1)
        if n == 1:
            nm = names.get(1)
            r = role_of(nm) if nm else role_of(c['roll'])
            return [r]
        roles = [role_of(names.get(k + 1, f"{c['roll']}_{k + 1}")) for k in range(n)]
        if c.get('ltc') and c['ltc']['channel'] < n:
            roles[c['ltc']['channel']] = 'tc'
        return roles

    def to_json(self):
        rolls = self.rolls()
        lanes = self.lanes()
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
                lane=c.get('lane'),
                slowmo=c.get('slowmo'), slowmo_user=c.get('slowmo_user'),
                capture_fps=TC.rate_label(c['capture_fps']) if c.get('capture_fps') else '',
                sync=c.get('sync'), fps=TC.rate_label(fps) if fps else '',
                tc_source=c.get('tc_source'), ltc=c.get('ltc'), manual=c.get('manual'), converted=c.get('converted'),
                codec=c.get('codec', ''), chapter=c.get('chapter') if c.get('chapter_of') else None,
                chapter_of=c.get('chapter_of'), audio_from=os.path.basename(c['audio_path']) if c.get('audio_path') else None,
                has_audio=media.total_audio_channels(c) > 0 and not (c.get('ltc') and c.get('listen_ch') is None),
                source_path=c.get('source_path'), opatom=c.get('opatom', False),
                day=c.get('day') or 'unknown', after_midnight=c.get('after_midnight', False), date_note=c.get('date_note'),
                day_forced=c.get('day_forced'), approx_start=self._clock_place(c),
                duplicate_of=os.path.basename(c['duplicate_of']) if c.get('duplicate_of') else None, user_include=c.get('user_include'),
                roles=self.clip_roles(c), track_names=c.get('track_names') or {}, scene=c.get('scene', ''), take=c.get('take', ''),
                size=c.get('size', 0), width=c.get('width', 0), height=c.get('height', 0), sr=c.get('sr') or c.get('source_sr'),
                bits=c.get('bits'), serial=c.get('serial'), model=c.get('model', ''),
            ))
        return dict(
            fps=TC.rate_label(self.fps), df=self.df, clips=out_clips,
            fps_info={k: v for k, v in self.fps_info.items() if k not in ('fps',)},
            rolls=[dict(name=r['name'], kind=r['kind'], camera=r['camera'], track_no=r['track_no'], n=len(r['clips']),
                        role=self._roll_role(r), channels=r.get('channels', 0))
                   for r in sorted(rolls.values(), key=lambda r: (r['kind'] != 'camera', r['track_no'] or 999, r['name']))],
            lanes=lanes,
            span=self.day_span(False), unsupported=self.unsupported, errors=self.errors,
            reference=self.default_reference(), days=self.days(),
        )
