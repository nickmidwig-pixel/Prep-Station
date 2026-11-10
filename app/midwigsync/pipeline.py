"""The whole day, start to finish."""
import datetime as dt
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
from collections import Counter

from . import tc as TC
from . import media
from . import safety as SAFE
from .bwf import StreamWavWriter, build_bext, build_ixml, patch_time_reference, WavInfo
from .scan import Project
from .wavesync import SyncAnalyzer
from . import stamp as ST
from . import resolve_aap as RA
from .assemble import Assembler
from .polywav import build_polywavs
from .xmlexport import write_day_xml, write_slowmo_xml
from .report import verify_against_ref, write_report
from . import refplan as RP
from . import exports as EX

SR = TC.SR
from .brand import ENV_CACHE, APP_NAME, OUT_SUFFIX, WORK_DIR, TIMELINES_DIR, TRANSCRIPT_DIR, SOUND_REPORT_DIR

DEFAULTS = dict(
    job_name='', out_dir='', reference=None,   # reference: dict(roll, channel, auto) - auto: picked by listening
    waveform_check=True,
    review_tc=False,               # pause after the timecode check so you can confirm the fixes (the window: on)
    aap_mode='resolve',            # 'resolve' | 'skip'
    aap_auto_press=True, render_dir='',
    iso_folder=True,               # aligned ISO files (each mic on its own, no mix) in ISO_ALIGNED/
    make_polys=True,               # polywavs in POLY_ALIGNED/ (off: ISOs only; the XML then uses the ISOs)
    chunk_mode='max',              # polywavs: largest whole TC minutes under 4 GB ('hour': whole hours)
    poly_layout='per_layout',      # day polywavs when takes use different track layouts: one set per layout, or
                                   # 'merged': one wide layout with every mic name of the day
    poly_style='auto',             # 'per_take': a mixer's per-take polywavs back as they came; 'day': day polywavs
                                   # of whole TC minutes under 4 GB; 'auto': per_take for short multitrack takes
    short_clip_s=20,
    mode='auto',                   # 'auto' | 'camera' | 'audio'  (auto: audio-only when no camera has sound)
    aligned_names='same',          # 'same' | 'suffix' (_ALN)
    export_xml=True, export_otio=True, export_aaf=True, export_ale=True, export_csv=True,
    transcribe=False, transcribe_language='en',   # 'auto' detects the language
)


class Cancelled(Exception):
    pass


class RefSink:
    """Writes the reference channel of a camera clip to a mono BWF while it is being listened to."""

    def __init__(self, clip, path, fps, df):
        self.clip, self.path, self.fps, self.df = clip, path, fps, df
        self.w = None

    def __call__(self, blk, sr):
        if self.w is None:
            tcs = TC.samples_to_tc(self.clip['start'], self.fps, self.df)
            name = f"REF_{self.clip['roll']}"
            desc = f'sTRK1={name}\r\ndTC={tcs}\r\ndFRAMERATE={TC.rate_label(self.fps)}ND\r\n'
            be = build_bext(desc, APP_NAME, self.clip['roll'], '', '', TC.wrap(self.clip['start'], self.fps),
                            f'A=PCM,F={sr},W=24,M=mono,T=ch1 of {self.clip["name"]}\r\n')
            fr = self.fps
            ix = build_ixml(self.clip['roll'], TC.rate_label(fr), f'{fr.numerator}/{fr.denominator}', self.df,
                            TC.wrap(self.clip['start'], self.fps), sr, 24, [name])
            self.w = StreamWavWriter(self.path, 1, sr, 24, 1, be, ix)
        self.w.write_float(blk)

    def close(self, off=0):
        if self.w:
            self.w.close(TC.wrap(self.clip['start'] + off, self.fps))

    def abort(self):
        if self.w:
            self.w.abort()


def default_job_name(project, date=''):
    """Just the date: the shoot day from the files (recording dates, user bits), today's date if they don't say."""
    if not date:
        days = Counter(c.get('day') for c in project.clips if c.get('day') and str(c.get('day'))[:4].isdigit())
        if days:
            date = days.most_common(1)[0][0].replace('-', '')
    if not date:
        ub = Counter(c.get('ubits') for c in project.clips if c.get('ubits'))
        u = ub.most_common(1)[0][0] if ub else ''
        if len(u) >= 8 and u[:8].isdigit():
            date = u[:8]
    return date or dt.date.today().strftime('%Y%m%d')


_GENERIC = re.compile(r'(?i)^(audio|sound|snd|isos?|wavs?|recorder|mixer|sound[_ -]?rolls?|audio[_ -]?files)$')


def _job_folder(path):
    """A folder called just SOUND or AUDIO belongs to the shoot folder around it, so names and output go there."""
    while path and _GENERIC.match(os.path.basename(path)) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return path


_MEDIA_EXT = None


def _holds_media(d):
    """A camera card or a roll folder: has media files right inside it, or a card structure."""
    global _MEDIA_EXT
    if _MEDIA_EXT is None:
        _MEDIA_EXT = media.VIDEO_EXT | media.AUDIO_EXT | media.AUDIO_OTHER_EXT | set(getattr(media, 'RAW_TRY_EXT', ()))
    try:
        names = os.listdir(d)
    except OSError:
        return False
    if any(n.upper() in SAFE.CARD_DIRS for n in names) or os.path.basename(d).upper() in SAFE.CARD_DIRS:
        return True
    return any(os.path.splitext(n)[1].lower() in _MEDIA_EXT for n in names if not n.startswith('.'))


def shoot_folder(project):
    """The top of what you dropped: the folder the output goes in (on the same drive as the media). Climbs out of
    camera cards, roll folders and folders just called SOUND/AUDIO; never above the drive or your home folder."""
    roots = [r for r in getattr(project, 'dropped', []) if r]
    if not roots:
        return ''
    dirs = [r if os.path.isdir(r) else os.path.dirname(r) for r in roots]
    try:
        base = os.path.commonpath(dirs)
    except ValueError:
        base = dirs[0]
    stop = {'/', '/Volumes', os.path.dirname(os.path.expanduser('~')), '/Users'}
    if base in stop or os.path.dirname(base) == '/Volumes' and len(dirs) > 1 and base != dirs[0]:
        base = dirs[0] if dirs[0] not in stop else base
    while (_holds_media(base) or _GENERIC.match(os.path.basename(base))) and os.path.dirname(base) not in stop \
            and base not in stop and os.path.dirname(base) != base:
        base = os.path.dirname(base)
    return base


def default_out_dir(project, job):
    top = shoot_folder(project)
    if top:
        return os.path.join(top, f'{job}{OUT_SUFFIX}')
    aud = [os.path.dirname(os.path.dirname(c.get('source_path') or c['path'])) for c in project.clips
           if c['kind'] == 'audio' and not c.get('converted', '').startswith(('M4A', 'MP3', 'AAC'))]
    if not aud:
        aud = [os.path.dirname(c.get('source_path') or c['path']) for c in project.clips]
    base = _job_folder(os.path.commonpath(aud)) if aud else os.path.expanduser('~/Desktop')
    return os.path.join(base, f'{job}{OUT_SUFFIX}')


class Job:
    def __init__(self, project: Project, settings, emit=None):
        self.full = project
        # slow motion / other frame rates: never synced, never a reference, their sound never used; own timeline
        self.slow = [c for c in project.clips if c.get('slowmo') and c['include']]
        self.lanes = project.lanes()                 # A CAM, B CAM... (sets c['lane'])
        lab = {l['key']: l['label'] for l in self.lanes}
        for c in project.clips:
            c['lane_label'] = lab.get(c.get('lane'), c['roll'])
        self.p = project.sync_view()
        self.s = dict(DEFAULTS, **{k: v for k, v in settings.items() if v is not None})
        self.emit = emit or (lambda kind, data: print(kind, data))
        self._cancel = threading.Event()
        self.user_continue = threading.Event()
        self.user_press = threading.Event()
        self.log_lines = []
        self.state = dict(step='', done=[], error=None)
        self.out = self.s['out_dir']
        self.mid = os.path.join(self.out, WORK_DIR)
        SAFE.install()
        for c in project.clips:
            SAFE.protect_clip(c)
        self.sync_report = None
        self.stamped = []
        self.refs = []
        self.aap_info = {}
        self.aligned = []
        self.verify = {}
        self.polys = []
        self.xmls = []
        self.ref_summary = []
        self._packs, self._passthru, self._ref_windows, self._out_names, self._names = [], [], {}, {}, 'suffix'

    # ------------------------------------------------------------ plumbing
    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        self.log_lines.append(line)
        self.emit('log', line)

    def progress(self, i, n, msg=''):
        self.emit('progress', dict(step=self.state['step'], frac=(i / n) if n else 0, msg=msg))

    def cancel(self):
        self._cancel.set()
        self.user_continue.set()

    def cancelled(self):
        return self._cancel.is_set()

    def check(self):
        if self.cancelled():
            raise Cancelled()

    def step(self, name):
        self.check()
        self.state['step'] = name
        self.emit('step', name)
        self.log(f'— {name}')

    # ------------------------------------------------------------ helpers
    def mode(self):
        m = self.s.get('mode') or 'auto'
        if m in ('camera', 'audio'):
            return m if (m == 'audio' or self.p.default_reference()) else 'audio'
        return 'camera' if self.p.default_reference() else 'audio'

    def ref(self):
        """The camera mic everything aligns to: one channel of one camera body, on every card that camera shot
        (until 1.6 only the clips of the one card that was picked counted, so takes shot on the camera's
        other cards had nothing to align to)."""
        if self.mode() == 'audio':
            return None
        r = self.s.get('reference') or self.p.default_reference()
        if r and r.get('roll'):
            rolls = self.body_rolls(r['roll'])
            if r.get('rolls') != rolls:
                r = dict(r, rolls=rolls)
        return r

    def body_rolls(self, roll):
        for l in self.lanes:
            if l.get('kind') == 'camera' and roll in (l.get('rolls') or []):
                return sorted(l['rolls'])
        return [roll]

    def ref_label(self, ref):
        rolls = ref.get('rolls') or [ref['roll']]
        if len(rolls) == 1:
            return ref['roll']
        lab = next((l['label'] for l in self.lanes if l.get('kind') == 'camera' and ref['roll'] in l['rolls']), ref['roll'])
        return f'{lab} ({len(rolls)} cards)'

    def names_mode(self, sources):
        return 'suffix' if self.s.get('aligned_names') == 'suffix' else 'same'

    def audio_sources(self, use_stamped=True):
        """Included audio clips with a position, in track order (numbered rolls first)."""
        order = [r['name'] for r in self.p.audio_rolls_ordered()]
        out = []
        for c in self.p.clips:
            if c['kind'] != 'audio' or not c['include'] or c['start'] is None or 'sync_failed' in c['flags']:
                continue
            if c['sr'] != SR:
                continue
            path = c.get('stamped_path') if use_stamped and c.get('stamped_path') else c['path']
            out.append(dict(path=path, name=os.path.basename(path), start=c.get('stamped_start', c['start']),
                            length=c['length'], roll=c['roll'], channels=c['channels'], clip=c))
        out.sort(key=lambda s: (order.index(s['roll']) if s['roll'] in order else 999, s['start']))
        return out

    def poly_style(self):
        st = self.s.get('poly_style', 'auto')
        return st if st in ('per_take', 'day') else RP.delivery_style(self.audio_sources())

    def channel_layout(self, sources):
        """Day-polywav tracks: one per (roll, track name) across the whole day, in order of first appearance, so a
        recorder whose takes use different layouts (633 on 2026-11-06: 4, 6 and 8 tracks) keeps every mixer label.
        Until 1.7 each roll got the first take's names and later takes were squeezed in by position, so a 6-track
        take's BOOM1 sat on a track called "BOOM -6db". A name used twice in one take (MIX, MIX) is numbered.
        Returns (names, map) with map[path] = [poly channel per file channel]."""
        names, slot, per_file = [], {}, {}
        rolls = []
        for s in sources:
            if s['roll'] not in rolls:
                rolls.append(s['roll'])
        for roll in rolls:
            for s in [x for x in sources if x['roll'] == roll]:
                ch = s['channels']
                tn = WavInfo(s['path']).track_names() if ch > 1 or s.get('named') else {}
                used = Counter()
                chans = []
                for k in range(ch):
                    nm = (tn.get(k + 1) or '').strip()
                    if not nm:
                        nm = roll if ch == 1 else f'{roll}_{k + 1}'
                    used[nm] += 1
                    key = (roll, nm, used[nm])
                    if key not in slot:
                        slot[key] = len(names)
                        names.append(nm if used[nm] == 1 else f'{nm} ({used[nm]})')
                    chans.append(slot[key])
                per_file[s['path']] = chans
        return names, per_file

    # ------------------------------------------------------------ steps
    def run(self):
        try:
            bad = SAFE.output_problem(self.out)
            if bad:
                raise RuntimeError(bad)
            SAFE.allow_output(self.out)
            os.makedirs(self.mid, exist_ok=True)
            self.log(f"Job {self.s['job_name']} → {self.out}")
            self.log(f"Originals are read-only: {SAFE.summary()['originals']} files protected.")
            if self.mode() == 'audio':
                self.log('Audio only: the reference mic is chosen for each stretch by listening (never a mix).')
            else:
                self.pick_reference()
            self.step('Checking timecode')
            self.do_sync()
            self.review_fixes()
            self.step('Writing corrected sound')
            self.do_sound_set()
            # the transcript only needs the sound and its timecode: start it now, it runs while Resolve renders
            tr = None
            if self.s.get('transcribe'):
                tr = threading.Thread(target=self._transcript_bg, name='transcript', daemon=True)
                tr.start()
            if self.s['aap_mode'] == 'resolve':
                self.step('Auto-Align Post')
                self.do_aap()
            else:
                self.log('Auto-Align skipped (settings).')
            self.step('Polywav')
            self.do_poly()
            self.step('XML')
            self.do_xml()
            if tr is not None:
                self.step('Transcript')
                if tr.is_alive():
                    self.log('Finishing the transcript (started while Resolve rendered)…')
                while tr.is_alive():
                    tr.join(0.5)
                    self.check()
            self.step('Report')
            from .transcribe import patient
            try:
                patient(self.do_report, 'writing the report', self.log)
            except Exception as e:  # noqa: BLE001
                self.log(f'Report could not be written: {e}')
                self.report_path = None
            if sys.platform == 'darwin' and self.s['aap_mode'] == 'resolve':
                try:                      # one last look for the backend's "reopen windows?" box
                    RA.dismiss_backend_dialog(self.log)
                except Exception:  # noqa: BLE001
                    pass
            self._sweep_dialogs = False
            self.emit('done', dict(out=self.out, report=self.report_path, camera_fixes=self.camera_fixes()))
        except Cancelled:
            self._sweep_dialogs = False
            self.log('Stopped.')
            self.emit('stopped', {})
        except Exception as e:  # noqa: BLE001
            self._sweep_dialogs = False
            tb = traceback.format_exc()
            self.log(f'ERROR: {e}')
            with open(os.path.join(self.mid, 'error.txt'), 'w') as f:
                f.write(tb)
            self.state['error'] = str(e)
            self.emit('error', dict(message=str(e), trace=tb[-2000:]))
        finally:
            self.save_state()

    def save_state(self):
        try:
            with open(os.path.join(self.mid, 'job.json'), 'w') as f:
                json.dump(dict(settings={k: v for k, v in self.s.items() if not k.startswith('_')}, sync=self.sync_report,
                               mode=self.mode(), reference_plan=self.ref_summary,
                               clips=[{k: v for k, v in c.items() if k in ('path', 'roll', 'start', 'orig_start', 'flags',
                                                                           'include', 'sync', 'stamped_path')}
                                      for c in self.p.clips],
                               aligned=self.aligned, polys=self.polys, xmls=self.xmls, aap=self.aap_info),
                          f, indent=1, default=str)
        except Exception:  # noqa: BLE001
            pass

    def do_sync(self):
        p = self.p
        p.check_short_clips(self.s['short_clip_s'])
        ref = self.ref()
        fps, df = p.fps, p.df
        ref_dir = os.path.join(self.mid, f"REF_{ref['roll']}") if ref else None     # working files
        sinks = {}

        def sink_factory(c):
            path = os.path.join(ref_dir, os.path.splitext(c['name'])[0] + '_REF.WAV')
            if os.path.exists(path):
                sinks[c['id']] = (path, None)
                return None
            s = RefSink(c, path, fps, df)
            sinks[c['id']] = (path, s)
            return s

        an = SyncAnalyzer(p, cache_dir=ENV_CACHE, log=self.log, progress=self.progress,
                          ref=ref, ref_sink_factory=sink_factory if ref else None, cancel=self.cancelled)
        if self.s['waveform_check']:
            try:
                self.sync_report = an.run()
            except KeyboardInterrupt:
                raise Cancelled()
        else:
            # still need the reference WAVs
            for c in p.clips:
                if ref and c['kind'] == 'video' and c['roll'] in ref['rolls'] and c['include'] and c['start'] is not None:
                    s = sink_factory(c)
                    if s:
                        from .wavesync import item_envelope
                        env, off = item_envelope(c, ref['channel'], None, None, sink=s)
                        s.close(off)
                        an.env[c['id']] = (env, off)
        n_fix = sum(1 for c in p.clips if {'sync_corrected', 'sync_found'} & set(c['flags']))
        n_man = sum(1 for c in p.clips if c.get('manual'))
        if n_man:
            self.log(f'{n_man} file(s) placed by hand, kept where you put them.')
        n_fail = sum(1 for c in p.clips if 'sync_failed' in c['flags'])
        self.log(f'Timecode check: {n_fix} file(s) corrected/placed, {n_fail} could not be placed.')
        # reference WAVs with final timecode
        self.refs = []
        if ref:
            for c in p.clips:
                if c['id'] not in sinks or c['start'] is None or not c['include']:
                    continue
                path = sinks[c['id']][0]
                if not os.path.exists(path):
                    continue
                off = an.env.get(c['id'], (None, 0))[1]
                start = c['start'] + off
                patch_time_reference(path, TC.wrap(start, fps), TC.samples_to_tc(start, fps, df))
                self.refs.append(dict(path=path, name=os.path.basename(path), start=start,
                                      length=WavInfo(path).nframes, clip=c['name']))
            self.log(f"Reference: {self.ref_label(ref)} channel {ref['channel'] + 1} → {len(self.refs)} WAV(s)")
        self.emit('project', p.to_json())

    def pick_reference(self):
        """Camera mode: align to an onboard camera mic. Unless you chose one, listen to every camera channel
        against the sound and pick the one that matches best (never the mixer's feed, LTC or a silent channel)."""
        r = self.s.get('reference')
        if r and not r.get('auto') and r.get('roll') and not r.get('listen'):
            self.log(f"Alignment track: {self.ref_label(self.ref())} ch {r['channel'] + 1} (chosen by you)")
            return
        self.step('Choosing the alignment track')
        from . import refpick
        only = None
        if r and not r.get('auto') and r.get('roll'):       # you chose the camera; the app picks its channel
            only = r.get('lane') or next((c.get('lane') for c in self.p.clips if c['roll'] == r['roll']), None) or r['roll']
        pick = refpick.pick_camera_reference(self.p, log=self.log, cancel=self.cancelled, only_lane=only)
        if pick:
            self.s['reference'] = dict(roll=pick['roll'], channel=pick['channel'], auto=only is None, why=pick['why'],
                                       **({'lane': only, 'listen': True} if only else {}))
            self.emit('ref_pick', self.s['reference'])
        r = self.ref()
        self.log(f"Alignment track: {self.ref_label(r)} ch {r['channel'] + 1}" + (f" ({pick['why']})" if pick else ''))

    def fixes(self):
        """Clips whose position differs from their own file timecode (found by waveform or set by you)."""
        out = []
        for c in self.p.clips:
            if not c['include'] or c['start'] is None:
                continue
            moved = bool({'sync_corrected', 'sync_found'} & set(c['flags']))
            if c.get('manual') and c['manual']['start'] != c['orig_start']:
                moved = True
            if moved:
                out.append(c)
        return out

    def review_fixes(self):
        """Stop and show the timecode fixes before anything is written (the window asks; headless: go on)."""
        fx = self.fixes()
        sr = self.sync_report or {}
        if not self.s.get('review_tc') or not fx:
            return
        rows = []
        for c in fx:
            s = c.get('sync') or {}
            rows.append(dict(id=c['id'], name=c['name'], roll=c['roll'], kind=c['kind'],
                             old=TC.samples_to_tc(c['orig_start'], self.p.fps, self.p.df) if c['orig_start'] is not None else None,
                             new=TC.samples_to_tc(c['start'], self.p.fps, self.p.df),
                             shift_s=round((c['start'] - c['orig_start']) / SR, 3) if c['orig_start'] is not None else None,
                             how='set by you' if c.get('manual') else (s.get('method') or 'waveform'),
                             score=s.get('score'), against=s.get('against')))
        self.decision = None
        self.user_decide = threading.Event()
        self.emit('review_tc', dict(fixes=rows, checked=sr.get('checked'), in_sync=sr.get('in_sync'),
                                    decision=sr.get('decision'), sources=sr.get('sources')))
        self.log(f'Waiting for you to confirm {len(rows)} timecode fix(es)…')
        while not self.user_decide.wait(0.5):
            self.check()
        if self.decision == 'stop':
            raise Cancelled()
        if self.decision == 'skip':
            n = 0
            for c in fx:
                if not c.get('manual'):
                    c['start'] = c['orig_start']
                    c['flags'] = [f for f in c['flags'] if f not in ('sync_corrected', 'sync_found')]
                    n += 1
            self.log(f'Kept the files\' own timecode for {n} clip(s), as you chose.')
        else:
            self.log('Fixes confirmed.')

    def decide(self, choice):
        self.decision = choice
        if getattr(self, 'user_decide', None):
            self.user_decide.set()

    def do_sound_set(self):
        """SOUND_TC_CORRECTED/ (top level, for the edit): only the sound files whose timecode was fixed, in their
        original format (bit depth, channels, track names, bext/iXML) with the new timecode, in the folders they came
        in (SOUND/03_LAV/…). WORK FILES/sound_for_align/ holds the full set Auto-Align works from (fixed files plus
        instant clones of the rest), so its renders never land beside the originals. Skipped when nothing changed."""
        p = self.p
        fps, df = p.fps, p.df
        aud = [c for c in p.clips if c['kind'] == 'audio' and c['include'] and c['start'] is not None]
        fixed = {c['id'] for c in self.fixes() if c['kind'] == 'audio'}
        conv = [c for c in aud if c.get('converted')]
        if not fixed and not conv:
            self.log('Sound timecode: nothing to fix, working from the originals (read-only).')
            return
        # The working set Auto-Align reads lives in WORK FILES (it writes its renders next to its sources, and those
        # must never land beside the originals). The editor-facing SOUND_TC_CORRECTED/ gets only the files whose
        # timecode changed, in the folders they came in (SOUND/03_LAV/...).
        root = os.path.join(self.mid, 'sound_for_align')
        shown = os.path.join(self.out, 'SOUND_TC_CORRECTED')
        n_fix = n_copy = 0
        for i, c in enumerate(aud):
            self.check()
            self.progress(i, len(aud), c['name'])
            out = os.path.join(root, self._mirror_dir(c), c.get('out_name', c['name']))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            if c['id'] in fixed or c.get('converted'):
                note = f"{APP_NAME}: {c['converted']}" if c.get('converted') and c['id'] not in fixed else None
                path, st = ST.stamp_wav(c, out, c['start'], fps, df, note=note)
                c['stamped_path'], c['stamped_start'] = path, st
                c['length'] = WavInfo(path).nframes * SR // max(1, WavInfo(path).sr)
                if c['id'] in fixed:
                    n_fix += 1
                    vis = os.path.join(shown, self._mirror_dir(c), os.path.basename(path))
                    os.makedirs(os.path.dirname(vis), exist_ok=True)
                    SAFE.clone_or_copy(path, vis)
                    self.stamped.append(dict(name=c['name'], out=os.path.relpath(vis, self.out),
                                             tc=TC.samples_to_tc(st, fps, df)))
            else:
                SAFE.clone_or_copy(c['path'], out)
                c['stamped_path'], c['stamped_start'] = out, c['start']
                n_copy += 1
        self.log(f'SOUND_TC_CORRECTED: {n_fix} file(s) with corrected timecode (the only files in that folder); '
                 f'{n_copy} unchanged file(s) cloned into WORK FILES for Auto-Align.')

    def _mirror_dir(self, c):
        """The sound file's folders below the folder that was dropped ('SOUND/01LAV'); its roll when that can't be
        worked out."""
        src = os.path.normpath(c.get('source_path') or c['path'])
        for r in sorted((os.path.normpath(x) for x in self.p.dropped), key=len, reverse=True):
            base = r if os.path.isdir(r) else os.path.dirname(r)
            if src.startswith(base + os.sep):
                rel = os.path.dirname(os.path.relpath(src, base))
                return rel if rel and rel != '.' else c['roll']
        return c['roll']

    def camera_fixes(self):
        """Camera clips whose timecode was fixed: offered after the run as full-quality copies."""
        return [dict(id=c['id'], name=c['name'], roll=c['roll'],
                     new=TC.samples_to_tc(c['start'], c.get('fps') or self.p.fps, bool(c.get('df'))))
                for c in self.fixes() if c['kind'] == 'video']

    def rewrite_cameras(self, ids=None):
        """After the run, when you ask: copies of the camera files whose timecode was fixed, with nothing changed
        but the timecode (the picture and sound are the original bytes). Then the timelines point at the copies."""
        todo = [c for c in self.fixes() if c['kind'] == 'video' and (ids is None or c['id'] in ids)]
        root = os.path.join(self.out, 'CAMERA_TC_CORRECTED')
        made = []
        for i, c in enumerate(todo):
            self.check()
            self.progress(i, len(todo), c['name'])
            try:
                path, how = ST.copy_with_tc(c, os.path.join(root, c['roll']), c['start'])
            except Exception as e:  # noqa: BLE001
                self.log(f"  {c['name']}: could not write a corrected copy ({e})")
                continue
            c['stamped_path'] = path
            c['stamped_start'] = c['start']
            made.append(path)
            self.log(f"  {c['name']} -> {os.path.relpath(path, self.out)} ({how})")
        if made:
            self.xmls = []
            self.do_xml()
        return made

    def _unique_sources(self, sources):
        """Auto-Align names its renders after the source file, so two recorders with the same file name
        (ZOOM0001.WAV on two Zooms, T001.WAV on two Sound Devices) would overwrite each other. Those get a
        uniquely named copy (an instant clone on APFS) for the Auto-Align pass; outputs keep the original names."""
        stems = Counter(os.path.splitext(s['name'])[0].lower() for s in sources)
        work = os.path.join(self.mid, 'aap_src')
        out_names, used = {}, set()
        for s in sources:
            stem = os.path.splitext(s['name'])[0]
            if stems[stem.lower()] > 1:
                uniq = f"{s['roll']}__{s['name']}"
                dst = os.path.join(work, uniq)
                os.makedirs(work, exist_ok=True)
                if not os.path.exists(dst):
                    SAFE.clone_or_copy(s['path'], dst)    # its own file, never a link to the original
                s['path'], s['name'] = dst, uniq
            ustem = os.path.splitext(s['name'])[0]
            want = stem + '_ALN'
            k = 2
            while (s['roll'], want.lower()) in used:
                want = f'{stem}_{k}_ALN'
                k += 1
            used.add((s['roll'], want.lower()))
            out_names[ustem] = want
        return out_names

    def do_aap(self):
        files = self.audio_sources()
        fps, df = self.p.fps, self.p.df
        self.log('Recorder tracks:')
        sources, self._packs, self._passthru = RP.split_sources(files, os.path.join(self.mid, 'split'), fps, df, log=self.log)
        self._names = self.names_mode(files)
        self._ref_windows = {}
        if self.mode() == 'audio':
            refs, summary = RP.plan_refs(sources, cache_dir=ENV_CACHE, log=self.log)
            self.ref_summary = summary
            self._ref_windows = RP.ref_windows(refs)
            for s in summary[:40]:
                self.log(f"  reference {TC.samples_to_tc(s['start'], fps, df)}–{TC.samples_to_tc(s['end'], fps, df)}: "
                         f"{s['track']} ({s['role']})")
            if len(summary) > 40:
                self.log(f'  … {len(summary) - 40} more stretches (all in job.json)')
            seen, self.refs = set(), []
            for s in sources:           # for the alignment check: every mono that served as a reference
                if s['path'] in self._ref_windows and s['path'] not in seen:
                    seen.add(s['path'])
                    self.refs.append(dict(path=s['path'], name=s['name'], start=s['start'], length=s['length']))
        else:
            refs = self.refs
        self._out_names = self._unique_sources(sources)
        if self._names == 'same':
            self._out_names = {k: v[:-4] if v.endswith('_ALN') else v for k, v in self._out_names.items()}
        if not refs:
            self.log('No reference to align to (no camera audio and no usable boom or lav). Skipping Auto-Align.')
            self.aap_info['status'] = 'skipped: nothing to use as a reference'
            return
        plan = RA.plan_timeline(refs, sources, self.p.fps)
        self._aap_plan = plan
        with open(os.path.join(self.mid, 'aap_plan.json'), 'w') as f:
            json.dump(plan, f, indent=1, default=str)
        n_items = sum(1 for i in plan['items'] if i['kind'] == 'source')
        self.log(f"Resolve timeline: {len(refs)} reference piece(s) on A1, {n_items} piece(s) to align on "
                 f"A2–A{len(plan['tracks'])}")
        render_dir = self.s.get('render_dir') or os.path.join(self.mid, 'AAP_RENDERS')
        os.makedirs(render_dir, exist_ok=True)
        since = time.time()
        watch_dirs = [(render_dir, True)] + [(d, False) for d in sorted({os.path.dirname(s['path']) for s in sources})]
        watcher = RA.RenderWatcher(plan['expected'], watch_dirs, since)
        info = {}
        old = RA.RenderWatcher(plan['expected'], [render_dir], 0).scan()
        per = {}
        for _p, (src, _sz) in old.items():
            per[src] = per.get(src, 0) + 1
        reuse = plan['expected'] and all(per.get(k, 0) >= v for k, v in plan['expected'].items())
        if reuse and not self.s.get('_fake_aap'):
            self.log(f'Using the {len(old)} Auto-Align renders already in {os.path.basename(render_dir)} '
                     f'(delete that folder to render again).')
            watcher.since = 0
            st = dict(files=sorted(old), done=sum(plan['expected'].values()), want=sum(plan['expected'].values()))
            self.aap_info.update(dict(renders='reused from an earlier run', render_dir=render_dir))
            return self._assemble(sources, st)
        bridge = None
        pressed = False
        if self.s.get('_fake_aap'):          # test hook
            self.s['_fake_aap'](plan, render_dir)
        else:
            bridge = RA.ResolveBridge(log=self.log)
            ok, msg = RA.backend_check(self.log)
            self.log('  ' + msg)
            if not ok:
                self.emit('toast', msg.split('\n')[0])
            try:
                bridge.connect()
                info = bridge.build(plan, f"{self.s['job_name']}_AAP", render_dir)
                self.log(f"Resolve project {info['project']} ready ({info['placed']}/{info['wanted']} clips).")
            except Exception as e:  # noqa: BLE001
                self.log(f'Resolve: {e}')
                info = dict(error=str(e))
            pressed = False
            if 'error' not in info and self.s['aap_auto_press'] and sys.platform == 'darwin':
                try:
                    if RA.ax_trusted(prompt=True):
                        RA.close_aap_panel()      # one left open from an earlier project shows that project's tracks
                        RA.open_aap_panel(self.log)
                        time.sleep(4)
                        self._aap_names = (info.get('ref_name', 'REF'), info.get('source_names') or
                                           [t['name'] for t in plan['tracks'][1:]])
                        pressed = RA.press_render(self.log, ref_name=self._aap_names[0],
                                                  source_names=self._aap_names[1])
                        if not pressed:
                            RA.dump_aap_tree(os.path.join(self.mid, 'aap_panel_tree.txt'))
                    else:
                        self.log(f'macOS has not given {APP_NAME} Accessibility access yet, so Render needs your click.')
                except Exception as e:  # noqa: BLE001
                    self.log(f'Could not press Render automatically: {e}')
            ask = dict(render_dir=render_dir, ref_track=f"REF (Audio {info.get('ref_track', 1)})",
                       sources=f"Audio {plan['tracks'][1]['index'] + (info.get('ref_track', 1) - 1)}–"
                               f"{plan['tracks'][-1]['index'] + (info.get('ref_track', 1) - 1)}" if len(plan['tracks']) > 1 else '',
                       project=info.get('project', ''), pressed=pressed, error=info.get('error'),
                       want=sum(plan['expected'].values()))
            self.emit('aap_wait', ask)
        # wait for renders. Auto-Align writes them where its own setting says ("Same as project" or a folder you
        # chose), so besides our folder, ask Resolve where the files in its "Aligned files for ..." bin live.
        self.user_continue.clear()
        last = None
        last_change = time.time()
        dialog_t = 0.0
        warned_stall = False
        asked = 0.0
        known = {d for d, _r in watcher.dirs}
        while True:
            self.check()
            if bridge is not None and info.get('timeline') and time.time() - asked > 8:
                asked = time.time()
                for d in bridge.render_dirs(info['timeline']):
                    if d not in known:
                        known.add(d)
                        watcher.dirs.append((d, False))
                        self.log(f'  Auto-Align is writing to {d}')
                        self.aap_info['render_dirs'] = sorted(known - {render_dir})
            st = watcher.status()
            if st != last:
                self.emit('aap_status', dict(done=st['done'], want=st['want'], seen=st['seen']))
                last = st
                last_change = time.time()
            # the backend's "reopen windows?" dialog can come up at any launch - the first one, or a "Press Render
            # again" after a crash with files already counted (2026-11-08: stuck at 28 of 69 because this only
            # looked while the count was 0). Look every 3 s whenever nothing has landed for a while.
            if pressed and sys.platform == 'darwin' and time.time() - dialog_t > 3 and time.time() - last_change > 5:
                dialog_t = time.time()
                RA.dismiss_backend_dialog(self.log)
            # nothing landed for a while: Auto-Align sits at "Converting... 0/N" when its backend hangs
            # (a hidden dialog, see RA.backend_check). Say so once, with what to do.
            if pressed and not warned_stall and time.time() - last_change > 240:
                warned_stall = True
                ok, msg = RA.backend_check(self.log, timeout=10)
                hint = (msg if not ok else
                        'Auto-Align has not written a file in 4 minutes. If its panel still says "Converting... 0", '
                        'press its X to cancel, then "Press Render again" here.')
                self.log('  ' + hint)
                self.emit('aap_hint', dict(text=hint))
            if st['complete'] and self.s.get('_fake_aap'):
                break
            if st['complete']:
                # give AAP a few seconds to finish its last file, then go on
                time.sleep(4)
                st = watcher.status()
                break
            if self.user_continue.wait(2.0):
                self.check()
                st = watcher.status()
                break
        self.aap_info.update(dict(project=info.get('project', ''), renders=f"{st['done']} of {st['want']} pieces",
                                  render_dir=render_dir))
        self.emit('aap_done', {})
        if pressed and st['complete'] and sys.platform == 'darwin' and not self.s.get('_fake_aap'):
            # Every render is in. The panel itself often never hears that its backend finished (the backend
            # crashes on exit), so it would sit at "Rendering…" with a spinner for good. Give it a moment to
            # file the renders into its bin, then close it.
            time.sleep(8)
            try:
                if RA.close_aap_panel():
                    self.log('  Closed the Auto-Align panel (all renders collected).')
            except Exception:  # noqa: BLE001
                pass
            # the backend crashes on exit and macOS may put up its "reopen windows?" box minutes later, when
            # nobody is watching the panel any more; keep answering it for the rest of the job
            self._sweep_dialogs = True
            threading.Thread(target=self._dialog_sweeper, name='aap-dialogs', daemon=True).start()
        st['files'] = self._tidy_renders(st['files'], render_dir)
        if not st['files']:
            self.log('No Auto-Align renders found; exporting unaligned audio.')
            self.aap_info['status'] = 'no renders'
            return
        self._assemble(sources, st)

    def _tidy_renders(self, files, render_dir):
        """Auto-Align writes its renders next to the source media (now SOUND_TC_CORRECTED/…, which the editor
        sees). They are working pieces, not deliverables: move them into WORK FILES/AAP_RENDERS. Only our own
        output folder is touched, never the originals."""
        out = []
        moved = 0
        for f in files:
            if os.path.dirname(os.path.abspath(f)) != os.path.abspath(render_dir) and \
                    os.path.abspath(f).startswith(os.path.abspath(self.out) + os.sep):
                dst = os.path.join(render_dir, os.path.basename(f))
                try:
                    SAFE.move(f, dst) if hasattr(SAFE, 'move') else os.replace(f, dst)
                    f = dst
                    moved += 1
                except OSError as e:
                    self.log(f'  could not move {os.path.basename(f)} into WORK FILES ({e})')
            out.append(f)
        if moved:
            self.log(f'  Moved {moved} Auto-Align render(s) out of the delivery folders into WORK FILES/AAP_RENDERS.')
        return sorted(out)

    def _dialog_sweeper(self):
        t0 = time.time()
        while getattr(self, '_sweep_dialogs', False) and time.time() - t0 < 3600:
            try:
                RA.dismiss_backend_dialog(self.log)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)

    def _assemble(self, sources, st):
        self.step('Building aligned ISOs')
        split_dir = os.path.join(self.mid, 'aligned_split')
        srcs = []
        for s in sources:
            d = dict(path=s['path'], name=s['name'], start=s['start'], roll=s['roll'])
            if s.get('split'):
                d['out_path'] = os.path.join(split_dir, s['name'])
            srcs.append(d)
        ref = self.ref()
        note = f"{self.ref_label(ref)} CH{ref['channel'] + 1}" if ref else 'chosen per stretch (audio only)'
        iso_root = os.path.join(self.out, 'ISO_ALIGNED') if self.s.get('iso_folder', True) else os.path.join(self.mid, 'aligned_iso')
        from .assemble import plan_hints
        asm = Assembler(srcs, st['files'], iso_root, log=self.log, progress=self.progress,
                        note=note, cancel=self.cancelled, ref_windows=self._ref_windows,
                        hints=plan_hints(getattr(self, '_aap_plan', None)))
        try:
            asm.plan_all()
            asm.save_plan(os.path.join(self.mid, 'assembly_plan.json'))
            done = asm.write_all(self._out_names)
            split_paths = {s['path'] for s in sources if s.get('split')}
            by_mono = {}
            for a in done:
                if a['source'] in split_paths:
                    by_mono[a['source']] = a['aligned']
            self.aligned = [a for a in done if a['source'] not in split_paths]
            if self._packs:
                self.log('Packing aligned tracks back into their polywav files (mix and timecode tracks as recorded):')

                def name_for(stem):
                    return stem if self._names == 'same' else stem + '_ALN'
                per_take = self.poly_style() == 'per_take'
                dest = (os.path.join(self.out, 'POLY_ALIGNED') if per_take and self.s.get('make_polys', True)
                        else os.path.join(self.mid, 'repacked'))
                packed = RP.repack(self._packs, by_mono, dest, name_for, note, log=self.log, cancel=self.cancelled)
                for r in packed:
                    r['repacked'] = per_take      # per-take polywavs stay as they are; on ISO days they join the day polywav
                self.aligned += packed
                if self.s.get('iso_folder', True):
                    self._split_isos(by_mono)
            for s in self._passthru:      # mix / timecode / silent files: kept as recorded
                self.aligned.append(dict(source=s['path'], aligned=s['path'], roll=s['roll'], start=s['start'],
                                         n=WavInfo(s['path']).nframes, sections=0, aligned_s=0.0,
                                         total_s=round(s['length'] / SR, 1), flags=[f"{s.get('role', 'mix')} track, not aligned"],
                                         passthrough=True, role=s.get('role', 'mix')))
        except KeyboardInterrupt:
            raise Cancelled()
        self.log(f'{len(self.aligned)} aligned file(s) written.')
        self.step('Checking alignment')
        self.verify = verify_against_ref([a for a in self.aligned if not a.get('passthrough')], self.refs)

    def _split_isos(self, by_mono):
        """Each mic of a sound mixer's polywav as its own aligned file in ISO_ALIGNED/ (never the mix or timecode
        tracks): <take>_<track name>.WAV, same timecode and metadata."""
        import re as _re
        n = 0
        for src, aligned in by_mono.items():
            m = _re.match(r'(.+)__(\d{2})_(.+)\.WAV$', os.path.basename(aligned), _re.I)
            if not m or not os.path.exists(aligned):
                continue
            stem, track = m.group(1), m.group(3)
            pack_roll = os.path.basename(os.path.dirname(src))
            dst = os.path.join(self.out, 'ISO_ALIGNED', pack_roll, f'{stem}_{track}.WAV')
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(aligned, dst)
            n += 1
        if n:
            self.log(f'ISO_ALIGNED: {n} mic track(s) as separate aligned files (mix tracks only in the polywavs).')

    def do_poly(self):
        """Aligned polywavs, always under 4 GB. A sound mixer's per-take polywavs come back as aligned per-take
        polywavs (same names and track layout, mix included). Long ISO days become polywavs of the largest whole
        TC minutes under 4 GB, mix tracks included. Without Auto-Align, the same from the (corrected) sound."""
        p = self.p
        packed = [a for a in self.aligned if a.get('repacked')]
        for a in packed:
            names = WavInfo(a['aligned']).track_names()
            w = WavInfo(a['aligned'])
            self.polys.append(dict(path=a['aligned'], start=a['start'], n=w.nframes, folder='POLY_ALIGNED',
                                   tc=TC.samples_to_tc(a['start'], self.p.fps, self.p.df),
                                   channels=[names.get(i + 1, f'{a["roll"]}_{i + 1}') for i in range(w.channels)],
                                   size=os.path.getsize(a['aligned'])))
        if packed:
            big = [x for x in self.polys if x['size'] >= 4 * 1024 ** 3]
            self.log(f'POLY_ALIGNED: {len(packed)} per-take polywav(s), aligned, same names and track layout'
                     + (f' ({len(big)} over 4 GB as recorded)' if big else '') + '.')
        ub = Counter(c.get('ubits') for c in p.clips if c.get('ubits'))
        ubits = ub.most_common(1)[0][0] if ub else ''
        sets = []
        loose = [a for a in self.aligned if not a.get('repacked') and a.get('role') != 'empty']
        if loose:
            srcs = [dict(path=a['aligned'], start=a['start'], roll=a['roll'],
                         channels=WavInfo(a['aligned']).channels) for a in loose]
            sets.append(('POLY_ALIGNED', srcs, 'ALN', 'Auto-Align Post aligned ISOs + mix'))
        elif not self.aligned:
            srcs = [dict(path=s['path'], start=s['start'], roll=s['roll'], channels=s['channels'])
                    for s in self.audio_sources()]
            if srcs and self.poly_style() == 'per_take':
                for s in srcs:
                    w = WavInfo(s['path'])
                    names = w.track_names()
                    self.polys.append(dict(path=s['path'], start=s['start'], n=w.nframes, folder='POLY_UNALIGNED',
                                           tc=TC.samples_to_tc(s['start'], self.p.fps, self.p.df),
                                           channels=[names.get(i + 1, f'{s["roll"]}_{i + 1}') for i in range(w.channels)],
                                           size=os.path.getsize(s['path'])))
                srcs = []
            sets.append(('POLY_UNALIGNED', srcs, 'RAW', 'unaligned ISOs'))
        if not self.s.get('make_polys', True):
            self.log('Polywavs: off (Settings). The sound goes out as ISOs only.')
            sets = []
        for folder, srcs, tag, note in sets:
            if not srcs:
                continue
            order = [r['name'] for r in p.audio_rolls_ordered()]
            srcs.sort(key=lambda s: (order.index(s['roll']) if s['roll'] in order else 999, s['start']))
            for sub, group, suffix in self.poly_groups(srcs):
                names, per_file = self.channel_layout(group)
                for s in group:
                    s['channels'] = per_file[s['path']]
                self.s['poly_channels'] = names
                if sub:
                    self.log(f'  layout {sub} ({len(names)} tracks): {", ".join(names)}')
                out = build_polywavs(group, names, os.path.join(self.out, folder),
                                     f"{self.s['job_name']}_POLY_{tag}{suffix}", p.fps, p.df, ubits, self.s['job_name'],
                                     note, self.s['chunk_mode'], log=self.log, progress=self.progress, cancel=self.cancelled)
                for o in out:
                    o['folder'] = folder
                    o['channels'] = names
                    o['layout'] = sub
                self.polys += out

    def poly_groups(self, srcs):
        """How a day's sources split into polywav sets. A recorder whose takes use different track layouts (the 633
        on 2026-11-06: 4, 6 and 8 tracks) gets one set per layout (A, B, C...), each file carrying exactly that
        layout's mic names, so no file is padded with empty tracks; poly_layout='merged' puts every mic name of the
        day in one wide layout instead (19 tracks there, 4 GB every 26 minutes). Mono ISO recorders are one set."""
        def layout_of(s):
            if s['channels'] <= 1:
                return ()
            tn = WavInfo(s['path']).track_names()
            return tuple((tn.get(i + 1) or '').strip() for i in range(s['channels']))
        lays = []
        for s in srcs:
            k = layout_of(s)
            if k not in lays:
                lays.append(k)
        multi = [k for k in lays if k]
        if len(multi) <= 1 or self.s.get('poly_layout', 'per_layout') == 'merged':
            return [('', srcs, '')]
        groups = []
        letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        for i, k in enumerate(lays):
            g = [s for s in srcs if layout_of(s) == k]
            groups.append((letters[i] if i < 26 else str(i + 1), g, f'_{letters[i] if i < 26 else i + 1}'))
        self.log(f'  {len(groups)} track layouts on this day -> one polywav set per layout')
        return groups

    def do_xml(self):
        p = self.p
        cams = []
        lane_rolls = [(l['label'], [r for r in p.camera_rolls() if r['name'] in l['rolls']])
                      for l in self.lanes if l['kind'] == 'camera']
        for label, rs in lane_rolls:
            clips = []
            for c in [c for r in rs for c in r['clips']]:
                if not c['include'] or c['start'] is None or 'sync_failed' in c['flags']:
                    continue
                path = c.get('stamped_path') or c['path']
                media_start = c.get('stamped_start') if c.get('stamped_path') else (c['orig_start'] if c['orig_start'] is not None else c['start'])
                cfps = c.get('fps') or p.fps
                clips.append(dict(path=path, start=c['start'], length=c['length'], kind='video',
                                  channels=media.total_audio_channels(c), width=c.get('width', 0),
                                  height=c.get('height', 0), reel=c['roll'], fps=cfps, raw=bool(c.get('raw_picture')),
                                  channels_streams=len(c.get('audio_streams') or []),
                                  media_start_frames=TC.samples_to_frames(media_start, cfps)))
            if clips:
                cams.append(dict(label=label, clips=clips))
        xml_dir = os.path.join(self.out, TIMELINES_DIR)
        job = self.s['job_name']
        tracks = []
        if self.polys:
            folder = 'POLY_ALIGNED' if any(x['folder'] == 'POLY_ALIGNED' for x in self.polys) else 'POLY_UNALIGNED'
            ps = [x for x in self.polys if x['folder'] == folder]
            layouts = []
            for x in ps:
                if tuple(x['channels']) not in layouts:
                    layouts.append(tuple(x['channels']))
            # one XML track per mic name: the same name in two polywav sets (BOOM1 in Conversat T01 and T02)
            # shares a track, since the sets never overlap in time; the XML writer splits any real overlap itself
            by_name = {}
            order = []
            for lay in layouts:
                for i, nm in enumerate(lay):
                    if nm not in by_name:
                        by_name[nm] = []
                        order.append(nm)
                    by_name[nm] += [dict(path=x['path'], start=x['start'], length=x['n'], kind='audio',
                                         channels=len(lay), src_track=i + 1, reel=nm,
                                         media_start_frames=TC.samples_to_frames(x['start'], p.fps))
                                    for x in ps if tuple(x['channels']) == lay]
            for nm in order:
                tracks.append(dict(label=nm, clips=sorted(by_name[nm], key=lambda c: c['start'])))
            tag = 'ALN' if folder == 'POLY_ALIGNED' else 'UNALIGNED'
            if self.s.get('export_xml', True):
                path = write_day_xml(os.path.join(xml_dir, f'{job}_SYNC_{tag}.xml'), f'{job} SYNC {tag}', p.fps,
                                     p.df, cams, tracks)
                self.xmls.append(os.path.relpath(path, self.out))
        elif self._aaf_tracks() and self.s.get('export_xml', True):
            tracks = self._aaf_tracks()                  # polywavs off: the editorial XML carries the aligned ISOs
            path = write_day_xml(os.path.join(xml_dir, f'{job}_SYNC_ISO.xml'), f'{job} SYNC ISO', p.fps, p.df,
                                 cams, tracks)
            self.xmls.append(os.path.relpath(path, self.out))
        elif cams and self.s.get('export_xml', True):
            path = write_day_xml(os.path.join(xml_dir, f'{job}_CAMERAS.xml'), f'{job} CAMERAS', p.fps, p.df, cams, [])
            self.xmls.append(os.path.relpath(path, self.out))
        if self.slow and self.s.get('export_xml', True):
            path = write_slowmo_xml(os.path.join(xml_dir, f'{job}_SLOW_MOTION.xml'),
                                    f'{job} SLOW MOTION + OTHER RATES', p.fps, p.df, self.slow)
            self.xmls.append(os.path.relpath(path, self.out))
            self.log(f'{len(self.slow)} slow-motion / other-rate clip(s) kept off the sync timeline, in their own XML.')
        for x in self.xmls:
            self.log(f'  wrote {x}')
        self._cams = cams
        self.do_exports(xml_dir, cams, tracks)

    def _aaf_tracks(self):
        """One track per mic from the aligned ISOs (Pro Tools sessions want ISOs), else None."""
        iso = os.path.join(self.out, 'ISO_ALIGNED')
        if not os.path.isdir(iso):
            return None
        by = {}
        for d, _, fs in os.walk(iso):
            for f in sorted(fs):
                if not f.upper().endswith('.WAV'):
                    continue
                path = os.path.join(d, f)
                w = WavInfo(path)
                if w.time_reference is None:
                    continue
                st = int(round(w.time_reference * SR / w.sr))
                lab = os.path.basename(d) if '_' not in f else f.rsplit('_', 1)[-1][:-4]
                by.setdefault(lab, []).append(dict(path=path, start=st, length=w.nframes * SR // w.sr, kind='audio',
                                                   channels=w.channels, src_track=1, reel=lab))
        return [dict(label=k, clips=v) for k, v in by.items()] or None

    def do_exports(self, xml_dir, cams, tracks):
        """OTIO / AAF / ALE / CSV. A failure here is logged, never fatal."""
        p, job = self.p, self.s['job_name']
        os.makedirs(xml_dir, exist_ok=True)
        by_src = {a['source']: a['aligned'] for a in self.aligned}
        if not tracks:
            by = {}
            for s in self.audio_sources():
                by.setdefault(s['roll'], []).append(s)
            tracks = [dict(label=roll if ss[0]['channels'] == 1 else f'{roll}_{ch + 1}', clips=[
                dict(path=s['path'], start=s['start'], length=s['length'], kind='audio', channels=s['channels'],
                     src_track=ch + 1, reel=roll) for s in ss]) for roll, ss in by.items() for ch in range(ss[0]['channels'])]
        jobs = [('export_otio', f'{job}.otio', 'OpenTimelineIO timeline',
                 lambda path: EX.write_otio(path, job, p.fps, cams, tracks)),
                ('export_aaf', f'{job}_AUDIO.aaf', 'AAF for Pro Tools / audio post (sound tracks, linked to the WAVs)',
                 lambda path: EX.write_aaf(path, job, p.fps, p.df, self._aaf_tracks() or tracks)),
                ('export_ale', f'{job}.ale', 'Avid Log Exchange', lambda path: EX.write_ale(path, p, by_src)),
                ('export_csv', f'{job}_CLIPS.csv', 'clip list', lambda path: EX.write_csv(path, p, by_src))]
        for key, fn, what, write in jobs:
            if not self.s.get(key):
                continue
            path = os.path.join(xml_dir, fn)
            try:
                if write(path):
                    self.xmls.append(os.path.relpath(path, self.out))
                    self.log(f'  wrote {os.path.relpath(path, self.out)} ({what})')
            except Exception as e:  # noqa: BLE001
                self.log(f'  could not write {fn}: {e}')

    def _transcript_bg(self):
        t0 = time.time()
        try:
            self.do_transcript(progress=self._side_progress)
        except Cancelled:
            pass
        except Exception as e:  # noqa: BLE001
            self.log(f'Transcript failed: {e}')
        if getattr(self, 'transcripts', None):
            self.log(f'Transcript done in {time.time() - t0:.0f} s (ran alongside the other steps)')
        self.emit('side_progress', dict(what='transcript', frac=1.0, msg='Transcript finished'))

    def _side_progress(self, i, n, msg=''):
        self.emit('side_progress', dict(what='transcript', frac=(i / n) if n else 0, msg=msg))

    def do_transcript(self, progress=None):
        """Transcript of the day from the sound files (local speech model; never fatal to the job)."""
        from . import transcribe as TR
        try:
            paths = TR.transcribe_day(self.p, os.path.join(self.out, TRANSCRIPT_DIR), self.s['job_name'], self.p.fps,
                                      self.p.df, language=self.s.get('transcribe_language', 'en'), log=self.log,
                                      progress=progress or self.progress, cancel=self.cancelled,
                                      cam_ref=self.ref() if self.mode() == 'camera' else None)
            for x in paths:
                self.log(f'  wrote {os.path.relpath(x, self.out)}')
            self.transcripts = [os.path.relpath(x, self.out) for x in paths]
        except KeyboardInterrupt:
            raise Cancelled()
        except Exception as e:  # noqa: BLE001
            self.log(f'Transcript failed: {e}')

    def copy_sound_reports(self):
        """The mixer's sound report(s) found next to the media (CSV/PDF/TXT), copied into SOUND REPORT/ so an
        editor finds them beside the polywavs. Copies only; the originals are read, never touched."""
        reps = [r.get('path') for r in (getattr(self.full, 'reports', None) or []) if r.get('path')]
        if not reps:
            return
        dest = os.path.join(self.out, SOUND_REPORT_DIR)
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as e:
            self.log(f'  could not make {SOUND_REPORT_DIR}: {e}')
            return
        n = 0
        for src in reps:
            try:
                to = os.path.join(dest, os.path.basename(src))
                if not os.path.exists(to):
                    shutil.copyfile(src, to)
                    n += 1
            except OSError as e:
                self.log(f'  could not copy sound report {os.path.basename(src)}: {e}')
        if n:
            self.log(f'{SOUND_REPORT_DIR}: {n} file(s) copied from the mixer.')

    def do_report(self):
        self.report_path = os.path.join(self.mid, f"{self.s['job_name']}_REPORT.txt")
        self.copy_sound_reports()
        rf = self.ref()
        st = dict(self.s, _mode=self.mode(), _ref=dict(rf, label=self.ref_label(rf)) if rf else rf,
                  _ref_summary=self.ref_summary, _slow=self.slow,
                  _fps_info=self.full.fps_info)
        write_report(self.report_path, self.s['job_name'], self.p, self.sync_report, self.stamped, self.aap_info,
                     self.aligned, self.verify, self.polys, self.xmls, st, self.log_lines)
        self.log(f'Report: {os.path.basename(self.report_path)}')
