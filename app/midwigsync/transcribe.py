"""Transcript of the day from the sound recorder files, for producers.

Runs on the Mac (nothing is uploaded): Whisper large-v3-turbo through MLX on Apple Silicon, faster-whisper on
Intel. The engine (~300 MB) installs the first time it's used and the model (~1.6 GB) downloads once into the app's
cache; both need internet that one time.

What gets transcribed: each stretch of recorded sound (a take, or a run of overlapping files), from the mixer's mix
when there is one, else the boom, else the plant, else all the lavs together. Speakers come from the lav tracks:
whoever's lav is loudest while a word is spoken, named from the track names the mixer typed (JANE, BOB...).
Without lavs there are no speaker names, just paragraphs.

Output, in TRANSCRIPT/: a Word document laid out for producers, plain text, and a CSV (one row per paragraph).
Timecode is printed at every speaker change or pause and every whole minute, and it matches the camera/sound TC.
"""
import csv
import datetime as dt
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys

import numpy as np

from . import tc as TC
from .brand import APP_NAME, APP_VERSION, CACHE, SUPPORT

SR = TC.SR
SR16 = 16000
MODEL_MLX = 'mlx-community/whisper-large-v3-turbo'
MODEL_FW = 'large-v3-turbo'
CHUNK_S = 600
SPEECH_ROLES = ('boom', 'plant', 'lav')
HALLUCINATIONS = re.compile(r'^\W*(thank you( so much)?( for watching)?|thanks for watching|subtitles by.*|'
                            r'please subscribe.*|you|bye\W*|\W*)\W*$', re.I)


# ------------------------------------------------------------------ engine
def engine_kind():
    if os.environ.get('PREP_FAKE_ASR'):
        return 'fake'
    if sys.platform == 'darwin' and platform.machine() == 'arm64':
        return 'mlx'
    return 'faster'


def engine_ready():
    k = engine_kind()
    if k == 'fake':
        return True
    return importlib.util.find_spec('mlx_whisper' if k == 'mlx' else 'faster_whisper') is not None


def _uv():
    for c in (os.path.join(SUPPORT, 'uv', 'uv'), os.path.join(SUPPORT, 'uv', 'bin', 'uv'), shutil.which('uv')):
        if c and os.path.exists(c):
            return c
    return None


def install_engine(log=print):
    """One-time install of the speech engine into the app's own Python. Returns True when ready."""
    if engine_ready():
        return True
    pkg = 'mlx-whisper' if engine_kind() == 'mlx' else 'faster-whisper'
    uv = _uv()
    log(f'Installing the speech engine ({pkg}), one time, about 300 MB…')
    if uv:
        cmd = [uv, 'pip', 'install', '--python', sys.executable, pkg]
    else:
        cmd = [sys.executable, '-m', 'pip', 'install', pkg]
    r = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, VIRTUAL_ENV=sys.prefix))
    if r.returncode != 0:
        log('  could not install it: ' + (r.stderr or r.stdout).strip()[-300:])
        return False
    importlib.invalidate_caches()
    return engine_ready()


def load_engine(log=print):
    """-> fn(audio16k float32, language or None) -> [dict(start, end, text, words=[dict(start, end, word, prob)],
    no_speech, logprob)] with times in seconds."""
    os.environ.setdefault('HF_HOME', os.path.join(CACHE, 'models'))
    k = engine_kind()
    if k == 'fake':
        return _fake_engine
    if k == 'mlx':
        import mlx_whisper

        def run(x, language):
            r = mlx_whisper.transcribe(x, path_or_hf_repo=MODEL_MLX, word_timestamps=True, language=language,
                                       condition_on_previous_text=False, hallucination_silence_threshold=2.0,
                                       no_speech_threshold=0.6, logprob_threshold=-1.0, verbose=None)
            return [dict(start=s['start'], end=s['end'], text=s['text'],
                         words=[dict(start=w['start'], end=w['end'], word=w['word'], prob=w.get('probability', 1.0))
                                for w in s.get('words', [])],
                         no_speech=s.get('no_speech_prob', 0.0), logprob=s.get('avg_logprob', 0.0))
                    for s in r.get('segments', [])]
        return run
    from faster_whisper import WhisperModel
    model = WhisperModel(MODEL_FW, device='cpu', compute_type='int8')

    def run(x, language):
        segs, _info = model.transcribe(x, language=language, word_timestamps=True, vad_filter=True,
                                       condition_on_previous_text=False, beam_size=5)
        return [dict(start=s.start, end=s.end, text=s.text,
                     words=[dict(start=w.start, end=w.end, word=w.word, prob=w.probability) for w in (s.words or [])],
                     no_speech=s.no_speech_prob, logprob=s.avg_logprob) for s in segs]
    return run


_FAKE_WORDS = ('the', 'light', 'was', 'better', 'over', 'there', 'so', 'we', 'moved', 'camera', 'and', 'started',
               'again', 'right', 'after', 'lunch')


def _fake_engine(x, language):
    """Stand-in for tests: one 'word' per voiced 0.25-0.5 s, sentences at pauses. Timing is real, text is not."""
    hop = SR16 // 50
    n = len(x) // hop
    if n == 0:
        return []
    e = 10 * np.log10(np.mean(x[:n * hop].reshape(n, hop) ** 2, 1) + 1e-12)
    floor = np.percentile(e, 20)
    v = e > max(floor + 12, -60)
    segs, words, k, i = [], [], 0, 0
    while i < n:
        if v[i]:
            j = i
            while j < n and v[j] and j - i < 25:
                j += 1
            words.append(dict(start=i / 50, end=j / 50, word=' ' + _FAKE_WORDS[k % len(_FAKE_WORDS)], prob=0.9))
            k += 1
            i = j
        else:
            if words and (i - int(words[-1]['end'] * 50)) > 30:
                segs.append(words)
                words = []
            i += 1
    if words:
        segs.append(words)
    out = []
    for ws in segs:
        ws[-1]['word'] += '.'
        out.append(dict(start=ws[0]['start'], end=ws[-1]['end'], text=''.join(w['word'] for w in ws), words=ws,
                        no_speech=0.0, logprob=-0.2))
    return out


# ------------------------------------------------------------------ audio
def _lowpass_taps(cut, sr, n=63):
    t = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * cut / sr * t) * np.hamming(n)
    return (h / h.sum()).astype(np.float32)


_TAPS = _lowpass_taps(7000, SR)


def to16k(x48):
    return np.convolve(x48, _TAPS, mode='same')[::3].astype(np.float32)


def _read(item, ch, a, n):
    from .refpick import _read as rd
    return rd(item, ch, a, n)


def _db(x):
    return 10 * np.log10(float(np.mean(x.astype(np.float64) ** 2)) + 1e-12)


# ------------------------------------------------------------------ plan
def speaker_name(name, roll):
    """Who a lav belongs to, for the transcript: 'Lav1 Alex' / 'Lav 5 Clara' / 'Alex Lav' / '03LAV_ALEX' -> 'ALEX'.
    A lav with no name ('LAV1', '02LAV') stays as it is ('LAV 1')."""
    n = re.sub(r'[_]+', ' ', (name or '').strip() or roll)
    who = re.sub(r'\b\d*\s*(lav(alier)?|rf|wl|wireless|radio|mic|iso|track|ch)\s*\d*\b', ' ', n, flags=re.I)
    who = re.sub(r'^[\s\d:()\[\]#-]+|[\s\d:()\[\]#-]+$', '', who)
    who = re.sub(r'\s+', ' ', who).strip()
    if re.search(r'[A-Za-z]{2}', who):
        return who.upper()
    n = re.sub(r'^(\d+)\s*([A-Za-z])', r'\2', n)                       # 02LAV -> LAV
    n = re.sub(r'([A-Za-z])(\d)', r'\1 \2', n)                         # LAV1 -> LAV 1
    return re.sub(r'\s+', ' ', n).strip().upper()


def plan(clips, cam_ref=None):
    """Group included sound files into stretches of recorded sound and pick what to listen to in each.
    No sound files: the reference camera's mic (cam_ref = dict(roll, channel))."""
    cl = sorted([c for c in clips if c['kind'] == 'audio' and c['include'] and c['start'] is not None
                 and not c.get('slowmo')], key=lambda c: c['start'])
    if not cl and cam_ref:
        cams = sorted([c for c in clips if c['kind'] == 'video' and c['roll'] in (cam_ref.get('rolls') or [cam_ref['roll']])
                       and c['include']
                       and c['start'] is not None and not c.get('slowmo')], key=lambda c: c['start'])
        return [dict(a=c['start'], b=c['start'] + c['length'], members=[c], speakers=[], files=[c['name']],
                     scene='', take='', listen_what='camera mic',
                     listen=[dict(clip=c, ch=cam_ref.get('channel', 0), role='cam', name=c['roll'])]) for c in cams]
    groups = []
    for c in cl:
        if groups and c['start'] <= groups[-1]['b'] + 2 * SR:
            g = groups[-1]
            g['members'].append(c)
            g['b'] = max(g['b'], c['start'] + c['length'])
        else:
            groups.append(dict(a=c['start'], b=c['start'] + c['length'], members=[c]))
    for g in groups:
        chans = []
        for c in g['members']:
            from .scan import Project
            roles = c.get('roles') or Project.clip_roles(c)
            names = c.get('track_names') or {}
            for i, r in enumerate(roles):
                nm = names.get(i + 1) or names.get(str(i + 1)) or (c['roll'] if len(roles) == 1 else f"{c['roll']} {i + 1}")
                chans.append(dict(clip=c, ch=i, role=r, name=nm))
        cover = lambda x: min(g['b'], x['clip']['start'] + x['clip']['length']) - max(g['a'], x['clip']['start'])
        for want in ('mix', 'boom', 'plant'):
            pick = [x for x in chans if x['role'] == want]
            if pick:
                best = max(pick, key=cover)
                g['listen'] = [x for x in pick if x['clip'] is best['clip']] if want == 'mix' else [best]
                g['listen_what'] = want
                break
        else:
            g['listen'] = [x for x in chans if x['role'] == 'lav']
            g['listen_what'] = 'lavs'
        g['speakers'] = [x for x in chans if x['role'] == 'lav']
        names = [speaker_name(x['name'], x['clip']['roll']) for x in g['speakers']]
        # two lavs called the same thing on different files are the same person
        for x, nm in zip(g['speakers'], names):
            x['label'] = nm
        g['scene'] = next((c.get('scene') for c in g['members'] if c.get('scene')), '')
        g['take'] = next((c.get('take') for c in g['members'] if c.get('take')), '')
        g['files'] = sorted({c['name'] for c in g['members']})
    return groups


# ------------------------------------------------------------------ run
def transcribe_day(project, out_dir, job, fps, df, language='en', log=print, progress=None, cancel=None, engine=None,
                   cam_ref=None):
    groups = [g for g in plan(project.clips, cam_ref) if g['listen']]
    if not groups:
        log('Transcript: no sound files to transcribe.')
        return []
    if engine is None:
        if not engine_ready() and not install_engine(log):
            log('Transcript skipped: the speech engine could not be installed (needs internet the first time).')
            return []
        log('Loading the speech model (the first time it downloads, about 1.6 GB)…')
        engine = load_engine(log)
    lang = None if language in (None, '', 'auto') else language
    total = sum(g['b'] - g['a'] for g in groups) or 1
    done = 0
    words = []
    for gi, g in enumerate(groups):
        a = g['a']
        while a < g['b']:
            if cancel and cancel():
                raise KeyboardInterrupt
            n = int(min(CHUNK_S * SR, g['b'] - a))
            x = sum(_read(l['clip'], l['ch'], a, n) for l in g['listen'])
            if a + n < g['b'] and n > 60 * SR:          # end the chunk at the quietest second of its last minute
                tail = x[-60 * SR:]
                k = 60
                e = tail[:k * SR].reshape(k, SR)
                q = int(np.argmin(np.mean(e ** 2, 1)))
                n = n - 60 * SR + q * SR + SR // 2
                x = x[:n]
            if _db(x) > -60:
                pk = float(np.abs(x).max()) or 1.0
                segs = engine(to16k(x / pk * 0.7), lang)
                spk_env = []
                for s in g['speakers']:
                    y = _read(s['clip'], s['ch'], a, n)
                    m = len(y) // (SR // 50)
                    spk_env.append(10 * np.log10(np.mean(y[:m * (SR // 50)].reshape(m, SR // 50) ** 2, 1) + 1e-12))
                for s in segs:
                    t = (s['text'] or '').strip()
                    if not t or (s.get('no_speech', 0) > 0.6 and s.get('logprob', 0) < -1.0) or HALLUCINATIONS.match(t):
                        continue
                    for w in s['words'] or [dict(start=s['start'], end=s['end'], word=t, prob=1.0)]:
                        t0 = a + int(w['start'] * SR)
                        t1 = a + int(max(w['end'], w['start'] + 0.05) * SR)
                        spk = None
                        if spk_env:
                            i0, i1 = int(w['start'] * 50), max(int(w['start'] * 50) + 1, int(w['end'] * 50))
                            lv = [float(np.mean(e[i0:i1])) if i1 <= len(e) and i1 > i0 else -120.0 for e in spk_env]
                            o = np.argsort(lv)[::-1]
                            if lv[o[0]] > -65 and (len(lv) == 1 or lv[o[0]] - lv[o[1]] > 3):
                                spk = g['speakers'][o[0]]['label']
                        words.append(dict(t0=t0, t1=t1, w=w['word'], p=w.get('prob', 1.0), spk=spk, g=gi))
            done += n
            a += n
            if progress:
                progress(done, total, f'{len(words)} words so far')
    _smooth_speakers(words)
    paras = paragraphs(words, groups)
    os.makedirs(out_dir, exist_ok=True)
    paths = [write_txt(os.path.join(out_dir, f'{job}_TRANSCRIPT.txt'), job, paras, groups, fps, df),
             write_csv(os.path.join(out_dir, f'{job}_TRANSCRIPT.csv'), paras, groups, fps, df)]
    try:
        paths.insert(0, write_docx(os.path.join(out_dir, f'{job}_TRANSCRIPT.docx'), job, paras, groups, fps, df))
    except ImportError:
        log('  (Word file skipped: python-docx is not installed; text and CSV written.)')
    log(f'Transcript: {len(words)} words, {len(paras)} paragraphs, {len(groups)} recording(s).')
    return paths


def _smooth_speakers(words):
    """Fill unknown speakers from their neighbours and drop one-word flips inside someone's sentence."""
    for i, w in enumerate(words):
        if w['spk'] is None:
            prev = next((x['spk'] for x in reversed(words[:i]) if x['spk'] and x['g'] == w['g']), None)
            w['spk'] = prev
    for i in range(1, len(words) - 1):
        a, b, c = words[i - 1], words[i], words[i + 1]
        if a['spk'] and a['spk'] == c['spk'] and b['spk'] != a['spk'] and b['t1'] - b['t0'] < SR // 2:
            b['spk'] = a['spk']


def paragraphs(words, groups):
    """New paragraph at a speaker change, a pause of 1.5 s, or after ~60 s at the end of a sentence."""
    paras = []
    cur = None
    for w in words:
        new = (cur is None or w['g'] != cur['g'] or w['spk'] != cur['spk'] or w['t0'] - cur['t1'] > 1.5 * SR
               or (w['t0'] - cur['t0'] > 60 * SR and re.search(r'[.?!]$', cur['words'][-1]['w'].strip()))
               or w['t0'] - cur['t0'] > 120 * SR)
        if new:
            cur = dict(g=w['g'], spk=w['spk'], t0=w['t0'], t1=w['t1'], words=[w])
            paras.append(cur)
        else:
            cur['words'].append(w)
            cur['t1'] = w['t1']
    return paras


def _runs(p, fps):
    """Text runs of a paragraph: ('text', s, sure) and ('tc', tc_string) at each whole minute it crosses."""
    out = []
    last_min = TC.samples_to_frames(p['t0'], fps) // (TC.nominal(fps) * 60)
    for w in p['words']:
        m = TC.samples_to_frames(w['t0'], fps) // (TC.nominal(fps) * 60)
        if m > last_min:
            out.append(('tc', TC.frames_to_tc(m * TC.nominal(fps) * 60, fps)))
            last_min = m
        out.append(('text', w['w'], w['p'] >= 0.4))
    return out


def _gap(a, b):
    s = int((b - a) / SR)
    return f'{s // 60}:{s % 60:02d}'


def _head(g, fps, df):
    bits = [f"{TC.samples_to_tc(g['a'], fps, df)} – {TC.samples_to_tc(g['b'], fps, df)}", ', '.join(g['files'][:3])
            + (f" +{len(g['files']) - 3}" if len(g['files']) > 3 else '')]
    if g.get('scene') or g.get('take'):
        bits.append(f"Scene {g['scene'] or '–'} / Take {g['take'] or '–'}")
    bits.append(f"from the {g['listen_what']}")
    return ' · '.join(bits)


def write_txt(path, job, paras, groups, fps, df):
    L = [f'{job} — TRANSCRIPT', f'{dt.date.today():%B %d, %Y} · {TC.rate_label(fps)} fps · {APP_NAME} {APP_VERSION}',
         'Timecode at each speaker change or pause, and every whole minute [in brackets]. It matches the camera and',
         'sound timecode. Machine transcript: check names, spellings and anything marked (?) against the audio.', '']
    spk = sorted({p['spk'] for p in paras if p['spk']})
    if spk:
        L.append('Speakers (from the lav track names): ' + ', '.join(spk))
        L.append('')
    last_g = None
    for p in paras:
        if p['g'] != last_g:
            if last_g is not None:
                L.append('')
            L.append('— ' + _head(groups[p['g']], fps, df) + ' —')
            L.append('')
            last_g = p['g']
        txt = ''
        for r in _runs(p, fps):
            if r[0] == 'tc':
                txt += f' [{r[1]}]'
            else:
                txt += r[1] + ('' if r[2] else '(?)')
        tc = TC.samples_to_tc(p['t0'], fps, df)
        L.append(f"{tc}   {p['spk'] + ':  ' if p['spk'] else ''}{txt.strip()}")
        L.append('')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L).rstrip() + '\n')
    return path


def write_csv(path, paras, groups, fps, df):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['TC In', 'TC Out', 'Speaker', 'Text', 'Files', 'Scene', 'Take'])
        for p in paras:
            g = groups[p['g']]
            w.writerow([TC.samples_to_tc(p['t0'], fps, df), TC.samples_to_tc(p['t1'], fps, df), p['spk'] or '',
                        ''.join(x['w'] for x in p['words']).strip(), ' '.join(g['files']), g.get('scene', ''),
                        g.get('take', '')])
    return path


def write_docx(path, job, paras, groups, fps, df):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor, Cm

    GREY, DARK, TCCOL = RGBColor(0x80, 0x80, 0x80), RGBColor(0x22, 0x22, 0x22), RGBColor(0x5A, 0x6B, 0x7D)
    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(2.0)
    sec.top_margin = sec.bottom_margin = Cm(1.8)
    st = doc.styles['Normal']
    st.font.name = 'Helvetica Neue'
    st.font.size = Pt(10.5)
    st.paragraph_format.space_after = Pt(5)
    st.paragraph_format.line_spacing = 1.15

    def run(par, text, bold=False, size=None, color=None, mono=False, italic=False):
        r = par.add_run(text)
        r.bold, r.italic = bold, italic
        if size:
            r.font.size = Pt(size)
        if color is not None:
            r.font.color.rgb = color
        if mono:
            r.font.name = 'Menlo'
            r._element.rPr.rFonts.set(qn('w:eastAsia'), 'Menlo')
        return r

    # header / footer
    hp = sec.header.paragraphs[0]
    run(hp, f'{job} — Transcript', size=8, color=GREY)
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run(fp, 'Page ', size=8, color=GREY)
    r = run(fp, '', size=8, color=GREY)
    for kind, text in (('begin', None), (None, 'PAGE'), ('end', None)):
        if kind:
            el = OxmlElement('w:fldChar')
            el.set(qn('w:fldCharType'), kind)
        else:
            el = OxmlElement('w:instrText')
            el.set(qn('xml:space'), 'preserve')
            el.text = text
        r._r.append(el)

    t = doc.add_paragraph()
    run(t, f'{job}', bold=True, size=20, color=DARK)
    t.paragraph_format.space_after = Pt(0)
    t2 = doc.add_paragraph()
    run(t2, 'Transcript', size=13, color=GREY)
    meta = doc.add_paragraph()
    first, last = groups[0]['a'], groups[-1]['b']
    run(meta, f'{dt.date.today():%B %d, %Y}   ·   {TC.rate_label(fps)} fps   ·   '
              f'{TC.samples_to_tc(first, fps, df)} – {TC.samples_to_tc(last, fps, df)}   ·   '
              f'{len(groups)} recording{"s" if len(groups) != 1 else ""}', size=9, color=GREY)
    spk = sorted({p['spk'] for p in paras if p['spk']})
    if spk:
        sp = doc.add_paragraph()
        run(sp, 'Speakers  ', bold=True, size=9, color=DARK)
        run(sp, ', '.join(spk) + '   (from the lav track names)', size=9, color=GREY)
    note = doc.add_paragraph()
    run(note, 'Timecode at each speaker change or pause, and every whole minute in grey. It matches the camera and '
              'sound timecode. Machine transcript: check names, spellings and words in grey italics against the audio.',
        size=8.5, color=GREY, italic=True)
    note.paragraph_format.space_after = Pt(14)

    last_g = None
    for p in paras:
        if p['g'] != last_g:
            h = doc.add_paragraph()
            h.paragraph_format.space_before = Pt(14)
            h.paragraph_format.space_after = Pt(6)
            pPr = h._p.get_or_add_pPr()
            bd = OxmlElement('w:pBdr')
            b = OxmlElement('w:bottom')
            for k, v in (('w:val', 'single'), ('w:sz', '4'), ('w:space', '2'), ('w:color', 'BBBBBB')):
                b.set(qn(k), v)
            bd.append(b)
            pPr.append(bd)
            run(h, _head(groups[p['g']], fps, df), bold=True, size=8.5, color=TCCOL)
            last_g = p['g']
        para = doc.add_paragraph()
        para.paragraph_format.left_indent = Cm(3.1)
        para.paragraph_format.first_line_indent = Cm(-3.1)
        para.paragraph_format.tab_stops.add_tab_stop(Cm(3.1))
        run(para, TC.samples_to_tc(p['t0'], fps, df), bold=True, size=9, color=TCCOL, mono=True)
        para.add_run('\t')
        if p['spk']:
            run(para, p['spk'] + '   ', bold=True, size=9.5, color=DARK)
        txt_first = True
        for r_ in _runs(p, fps):
            if r_[0] == 'tc':
                run(para, f' [{r_[1]}] ', size=8, color=GREY, mono=True)
                continue
            s = r_[1].lstrip() if txt_first else r_[1]
            txt_first = False
            if r_[2]:
                run(para, s)
            else:
                run(para, s, color=GREY, italic=True)
    doc.core_properties.title = f'{job} transcript'
    doc.core_properties.author = APP_NAME
    doc.save(path)
    return path
