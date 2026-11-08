"""Project frame rate: 23.976 unless the day clearly says otherwise, and then ask rather than guess.

Evidence, weighed by how far it can be trusted:
  - cameras: the recording rate of the footage, by duration. High-speed rates (over 30) are slow motion inside
    a normal-rate project, so they're noted but don't vote unless every camera shot that way;
  - sound reports from the mixer (CSV/TXT/PDF found in the dropped folders): what the cart was set to;
  - sound file metadata (iXML TIMECODE_RATE, bext sSPEED/dFRAMERATE). Zaxcom recorders often leave this at a
    default that isn't the real rate, so Zaxcom files are listed but never vote.
If nothing points away from 23.976, it's 23.976. If cameras, a report or the sound files point to another rate,
the project stays at 23.976 and the window asks.
"""
import os
import re
import zlib
from collections import Counter, defaultdict
from fractions import Fraction

from . import tc as TC

DEFAULT = '23.976'
CHOICES = ['23.976', '24', '25', '29.97', '29.97 DF', '30', '50', '59.94', '59.94 DF', '60']
ZAX = re.compile(r'zaxcom|nomad|\bdeva\b|\bnova\b|\bmaxx\b|\boasis\b|\bzfr|zmt', re.I)
REPORT_EXT = {'.csv', '.txt', '.pdf', '.tsv', '.xml', '.html', '.htm', '.rtf'}
SKIP_DIRS = ('_SYNCED', '_SYNC_WORK', '_MIDWIG', 'WORK FILES (not for the edit)')


def label(fps, df=False):
    return TC.rate_label(fps) + (' DF' if df else '')


def parse_choice(s):
    """'29.97 DF' -> (Fraction, True)"""
    s = (s or '').strip()
    df = bool(re.search(r'\bDF\b', s, re.I)) and not re.search(r'NDF', s, re.I)
    return TC.parse_rate(re.sub(r'(?i)\s*n?df', '', s)), df


def is_zaxcom(c):
    blob = ' '.join(str(c.get(k) or '') for k in ('originator', 'tape', 'path'))
    return bool(ZAX.search(c.get('originator') or '') or ZAX.search(c.get('ixml_maker') or '') or
                (c.get('originator') is None and ZAX.search(blob)))


# ------------------------------------------------------------------ sound reports
_RATE_RE = re.compile(
    r'(?:frame\s*rate|framerate|fps|tc\s*rate|t/?c\s*fps|timecode\s*rate|time\s*code\s*rate|tc\s*format|speed)'
    r'[^0-9\n]{0,24}?(2[345](?:\.9[78]6?)?|29\.97|30|47\.95|48|50|59\.94|60)(?:\s*p)?\s*([nN]?[dD][fF]?|NDF|ND)?\b', re.I)


def find_reports(paths, limit=40):
    """Sound reports in or next to the dropped folders (name says report, or a CSV/PDF beside the WAVs)."""
    out = []
    seen = set()
    for p in paths:
        p = os.path.abspath(p)
        dirs = [p] if os.path.isdir(p) else [os.path.dirname(p)]
        for top in dirs:
            for d, sub, files in os.walk(top):
                sub[:] = [x for x in sub if not x.endswith(SKIP_DIRS) and not x.startswith('.')]
                has_wav = any(f.lower().endswith(('.wav', '.bwf')) for f in files)
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in REPORT_EXT or f.startswith('.'):
                        continue
                    if not (re.search(r'report|sound|audio|log|metadata', f, re.I) or (has_wav and ext in ('.csv', '.pdf'))):
                        continue
                    fp = os.path.join(d, f)
                    if fp in seen:
                        continue
                    try:
                        if os.path.getsize(fp) > 8 << 20:
                            continue
                    except OSError:
                        continue
                    seen.add(fp)
                    out.append(fp)
                    if len(out) >= limit:
                        return out
    return out


def _pdf_text(path):
    try:
        from pypdf import PdfReader  # optional
        return '\n'.join((pg.extract_text() or '') for pg in PdfReader(path).pages[:30])
    except Exception:  # noqa: BLE001
        pass
    raw = open(path, 'rb').read()
    chunks = []
    for m in re.finditer(rb'stream\r?\n(.*?)\r?\nendstream', raw, re.S):
        data = m.group(1)
        try:
            data = zlib.decompress(data)
        except zlib.error:
            pass
        chunks += [t.decode('latin-1') for t in re.findall(rb'\(((?:[^()\\]|\\.)*)\)', data)]
    return ' '.join(chunks)


def read_report(path):
    """-> dict(path, rates=Counter{(label): n}) or None"""
    try:
        if path.lower().endswith('.pdf'):
            text = _pdf_text(path)
        else:
            text = open(path, 'rb').read().decode('utf-8', 'replace')
    except OSError:
        return None
    rates = Counter()
    for m in _RATE_RE.finditer(text):
        num, flag = m.group(1), (m.group(2) or '').upper()
        if num in ('23.98', '23.976', '23.97'):
            num = '23.976'
        fps = TC.parse_rate(num)
        df = flag in ('DF', 'D') and TC.nominal(fps) in (30, 60)
        rates[label(fps, df)] += 1
    # Sound Devices style column: a "Frame Rate" header with values in that column
    if not rates and path.lower().endswith(('.csv', '.tsv')):
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            cells = [c.strip().strip('"') for c in re.split(r'[,\t;]', ln)]
            idx = next((j for j, c in enumerate(cells) if re.fullmatch(r'(?i)(frame\s*rate|fps|tc\s*rate|speed)', c)), None)
            if idx is None:
                continue
            for ln2 in lines[i + 1:i + 400]:
                cs = [c.strip().strip('"') for c in re.split(r'[,\t;]', ln2)]
                if idx < len(cs):
                    mm = re.match(r'(2[345](?:\.9[78]6?)?|29\.97|30|50|59\.94|60)\s*([nN]?[dD][fF]?)?', cs[idx])
                    if mm:
                        fps = TC.parse_rate('23.976' if mm.group(1).startswith('23.9') else mm.group(1))
                        df = (mm.group(2) or '').upper() in ('DF', 'D') and TC.nominal(fps) in (30, 60)
                        rates[label(fps, df)] += 1
            break
    return dict(path=path, rates=rates) if rates else None


def w_is_single(name):
    return name.endswith('report')


# ------------------------------------------------------------------ assessment
def assess(project, reports=None, override=None):
    """-> dict(label, fps, df, status ok|ask|set, suggest, reason, evidence=[...])"""
    ev = []
    cams = defaultdict(Counter)        # roll -> {label: samples}
    hfr = defaultdict(Counter)
    def guessed(c):
        l = c.get('ltc') or {}
        return l.get('rate_from') == 'guess' and TC.nominal(c['fps']) not in (25, 50)

    for c in project.clips:
        if c['kind'] != 'video' or not c.get('fps') or not c.get('include', True) or guessed(c):
            continue
        lab = label(c['fps'], bool(c.get('df')))
        (hfr if float(c['fps']) > 31 else cams)[c['roll']][lab] += max(1, c.get('length') or 1)
    cam_total = Counter()
    for roll, cnt in cams.items():
        top = cnt.most_common(1)[0][0]
        ev.append(dict(kind='camera', name=roll, label=top, votes=True,
                       note='' if len(cnt) == 1 else 'mostly'))
        for k, v in cnt.items():
            cam_total[k] += v
    for roll, cnt in hfr.items():
        top = cnt.most_common(1)[0][0]
        ev.append(dict(kind='camera', name=roll, label=top, votes=False, note='high speed, counts as slow motion'))
    if not cams and hfr:
        for cnt in hfr.values():
            for k, v in cnt.items():
                cam_total[k] += v
    snd = defaultdict(Counter)
    zax = defaultdict(Counter)
    for c in project.clips:
        if c['kind'] != 'audio' or not c.get('fps') or guessed(c):
            continue
        lab = label(c['fps'], bool(c.get('df')))
        (zax if is_zaxcom(c) else snd)[c['roll']][lab] += 1
    snd_total = Counter()
    for roll, cnt in snd.items():
        top = cnt.most_common(1)[0][0]
        ev.append(dict(kind='sound', name=roll, label=top, votes=True, note=''))
        snd_total[top] += 1
    for roll, cnt in zax.items():
        top = cnt.most_common(1)[0][0]
        ev.append(dict(kind='sound', name=roll, label=top, votes=False,
                       note='Zaxcom files often don’t carry the real rate, so not counted'))
    rep_total = Counter()
    for r in reports or []:
        top = r['rates'].most_common(1)[0][0]
        ev.append(dict(kind='report', name=os.path.basename(r['path']), label=top, votes=True,
                       note='' if len(r['rates']) == 1 else 'mostly'))
        rep_total[top] += 1

    def share(cnt, lab):
        tot = sum(cnt.values())
        return cnt[lab] / tot if tot else 0.0

    others = Counter()
    for cnt, why in ((cam_total, 'cameras'), (snd_total, 'sound files'), (rep_total, 'sound report')):
        if not cnt:
            continue
        top, _ = cnt.most_common(1)[0]
        if top != DEFAULT and share(cnt, top) >= 0.6:
            others[top] += 1
            others[(top, why)] += 1
    suggest = None
    if others:
        cands = [k for k in others if isinstance(k, str)]
        suggest = max(cands, key=lambda k: others[k])
        whys = [w for (k, w) in [x for x in others if isinstance(x, tuple)] if k == suggest]
    agree = [w for cnt, w in ((cam_total, 'cameras'), (snd_total, 'sound files'), (rep_total, 'sound report'))
             if cnt and cnt.most_common(1)[0][0] == DEFAULT]

    if override:
        fps, df = parse_choice(override)
        lab = label(fps, df)
        reason = 'Set by you.'
        if suggest and suggest != lab:
            reason += f' (The day looks like {suggest}.)'
        status = 'set'
    elif suggest:
        fps, df = parse_choice(DEFAULT)
        lab = DEFAULT
        status = 'ask'
        names = [{'cameras': 'the cameras', 'sound files': 'the sound files', 'sound report': 'the sound report'}[w] for w in whys]
        said = names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]
        reason = f"{said[0].upper() + said[1:]} {'says' if len(names) == 1 and w_is_single(names[0]) else 'say'} {suggest}."
    else:
        fps, df = parse_choice(DEFAULT)
        lab = DEFAULT
        status = 'ok'
        reason = (f"Checked against {', '.join(agree)}." if agree else 'Nothing in the files says otherwise, so 23.976.')
    return dict(label=lab, fps=fps, df=df, status=status, suggest=suggest, reason=reason, evidence=ev,
                choices=CHOICES)
