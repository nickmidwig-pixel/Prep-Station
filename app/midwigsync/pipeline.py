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
from .bwf import StreamWavWriter, build_bext, build_ixml, patch_time_reference, WavInfo
from .scan import Project
from .wavesync import SyncAnalyzer
from . import stamp as ST
from . import resolve_aap as RA
from .assemble import Assembler
from .polywav import build_polywavs
from .xmlexport import write_day_xml
from .report import verify_against_ref, write_report
from . import refplan as RP
from . import exports as EX

SR = TC.SR
from .brand import ENV_CACHE, APP_NAME, OUT_SUFFIX, WORK_DIR

DEFAULTS = dict(
    job_name='', out_dir='', reference=None,
    waveform_check=True, stamp_no_tc=True, stamp_corrected_audio=True, stamp_corrected_video=False,
    aap_mode='resolve',            # 'resolve' | 'skip'
    aap_auto_press=True, render_dir='',
    poly_aligned=True, poly_unaligned=False, chunk_mode='max',
    xml_iso=True, xml_poly=True, short_clip_s=20,
    mode='auto',                   # 'auto' | 'camera' | 'audio'  (auto: audio-only when no camera has sound)
    aligned_names='auto',          # 'auto' | 'same' | 'suffix'   (auto: same names for per-take deliveries)
    poly_per_take=False,           # also make day-long polywavs from a sound mixer's per-take files
    export_otio=True, export_aaf=True, export_ale=True, export_csv=True,
    synced_clips=False,            # camera clips re-wrapped with the aligned sound (copies the picture)
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
    ub = Counter(c.get('ubits') for c in project.clips if c.get('ubits'))
    if ub and not date:
        u = ub.most_common(1)[0][0]
        if len(u) >= 8 and u[:8].isdigit():
            date = u[:8]
    if not date:
        times = [c.get('created', '') for c in project.clips if c.get('created')]
        date = (sorted(times)[0][:10].replace('-', '') if times else dt.date.today().strftime('%Y%m%d'))
    # common parent of the audio folders
    aud = [os.path.dirname(os.path.dirname(c.get('source_path') or c['path'])) for c in project.clips if c['kind'] == 'audio']
    top = _job_folder(os.path.commonpath(aud)) if aud else ''
    base = os.path.basename(top)
    base = re.sub(r'(?i)^\d{8}_?', '', base)
    base = re.sub(r'(?i)[_ -]*(audio|sound|snd|iso|isos)$', '', base)
    return f'{date}_{base}'.strip('_') if base else date


_GENERIC = re.compile(r'(?i)^(audio|sound|snd|isos?|wavs?|recorder|mixer|sound[_ -]?rolls?|audio[_ -]?files)$')


def _job_folder(path):
    """A folder called just SOUND or AUDIO belongs to the shoot folder around it, so names and output go there."""
    while path and _GENERIC.match(os.path.basename(path)) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return path


def default_out_dir(project, job):
    aud = [os.path.dirname(os.path.dirname(c.get('source_path') or c['path'])) for c in project.clips
           if c['kind'] == 'audio' and not c.get('converted', '').startswith(('M4A', 'MP3', 'AAC'))]
    if not aud:
        aud = [os.path.dirname(c.get('source_path') or c['path']) for c in project.clips]
    base = _job_folder(os.path.commonpath(aud)) if aud else os.path.expanduser('~/Desktop')
    return os.path.join(base, f'{job}{OUT_SUFFIX}')


class Job:
    def __init__(self, project: Project, settings, emit=None):
        self.p = project
        self.s = dict(DEFAULTS, **{k: v for k, v in settings.items() if v is not None})
        self.emit = emit or (lambda kind, data: print(kind, data))
        self._cancel = threading.Event()
        self.user_continue = threading.Event()
        self.user_press = threading.Event()
        self.log_lines = []
        self.state = dict(step='', done=[], error=None)
        self.out = self.s['out_dir']
        self.mid = os.path.join(self.out, WORK_DIR)
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
        if self.mode() == 'audio':
            return None
        return self.s.get('reference') or self.p.default_reference()

    def names_mode(self, sources):
        m = self.s.get('aligned_names') or 'auto'
        if m in ('same', 'suffix'):
            return m
        return 'same' if (self.mode() == 'audio' and RP.delivery_style(sources) == 'per_take') else 'suffix'

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

    def channel_layout(self, sources):
        names, idx = [], {}
        for s in sources:
            if s['roll'] in idx:
                continue
            ch = max(x['channels'] for x in sources if x['roll'] == s['roll'])
            names_r = WavInfo(s['path']).track_names()
            idx[s['roll']] = list(range(len(names), len(names) + ch))
            for k in range(ch):
                names.append(s['roll'] if ch == 1 else (names_r.get(k + 1) or f"{s['roll']}_{k + 1}"))
        return names, idx

    # ------------------------------------------------------------ steps
    def run(self):
        try:
            os.makedirs(self.mid, exist_ok=True)
            self.log(f"Job {self.s['job_name']} → {self.out}")
            if self.mode() == 'audio':
                self.log('Audio only: the reference mic is chosen for each stretch (boom when there is one).')
            else:
                r = self.ref()
                self.log(f"Camera reference: {r['roll']} ch {r['channel'] + 1}")
            self.step('Checking timecode')
            self.do_sync()
            self.step('Writing timecode to copies')
            self.do_stamp()
            if self.s['aap_mode'] == 'resolve':
                self.step('Auto-Align Post')
                self.do_aap()
            else:
                self.log('Auto-Align skipped (settings).')
            self.step('Polywav')
            self.do_poly()
            self.step('XML')
            self.do_xml()
            self.step('Report')
            self.do_report()
            self.emit('done', dict(out=self.out, report=self.report_path))
        except Cancelled:
            self.log('Stopped.')
            self.emit('stopped', {})
        except Exception as e:  # noqa: BLE001
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
        ref_dir = os.path.join(self.out, f"REF_{ref['roll']}") if ref else None
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
                if ref and c['kind'] == 'video' and c['roll'] == ref['roll'] and c['include'] and c['start'] is not None:
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
            self.log(f"Reference: {ref['roll']} channel {ref['channel'] + 1} → {len(self.refs)} WAV(s)")
        self.emit('project', p.to_json())

    def do_stamp(self):
        p = self.p
        fps, df = p.fps, p.df
        fixed = os.path.join(self.out, 'TC_FIXED')
        for c in p.clips:
            self.check()
            if not c['include'] or c['start'] is None:
                continue
            moved = {'sync_corrected', 'sync_found'} & set(c['flags'])
            if c.get('manual') and c['manual']['start'] != c['orig_start']:
                moved = True
            if not moved and c['kind'] == 'audio' and c.get('converted'):
                # converted (resampled / non-WAV) audio: put the 48 kHz copy with the job, not in a cache
                out = os.path.join(self.out, 'CONVERTED_48K', c['roll'], c.get('out_name', c['name']))
                path, st = ST.stamp_wav(c, out, c['start'], fps, df, note=f"{APP_NAME}: {c['converted']}") \
                    if c['start'] is not None else (None, None)
                if path:
                    c['stamped_path'], c['stamped_start'] = path, st
                    c['length'] = WavInfo(path).nframes
                    self.stamped.append(dict(name=c['name'], out=os.path.relpath(path, self.out),
                                             tc=TC.samples_to_tc(st, fps, df)))
                continue
            if not moved:
                continue
            notc = c['orig_start'] is None
            if c['kind'] == 'audio' and (self.s['stamp_corrected_audio'] or (notc and self.s['stamp_no_tc'])):
                out = os.path.join(fixed, c['roll'], c.get('out_name', c['name']))
                path, st = ST.stamp_wav(c, out, c['start'], fps, df)
                c['stamped_path'], c['stamped_start'] = path, st
                c['length'] = WavInfo(path).nframes
                self.stamped.append(dict(name=c['name'], out=os.path.relpath(path, self.out),
                                         tc=TC.samples_to_tc(st, fps, df)))
            elif c['kind'] == 'video' and ((notc and self.s['stamp_no_tc']) or (not notc and self.s['stamp_corrected_video'])):
                self.log(f"  re-wrapping {c['name']} with timecode")
                out = os.path.join(fixed, c['roll'], c['name'])
                try:
                    path, frames = ST.stamp_video(c, out, c['start'], fps, df)
                except RuntimeError as e:
                    self.log(f"  could not re-wrap {c['name']}: {e}")
                    continue
                c['stamped_path'] = path
                c['stamped_start'] = TC.frames_to_samples(frames, fps)
                self.stamped.append(dict(name=c['name'], out=os.path.relpath(path, self.out),
                                         tc=TC.frames_to_tc(frames, fps, df)))
        self.log(f'{len(self.stamped)} file(s) written to TC_FIXED.')

    def _unique_sources(self, sources):
        """Auto-Align names its renders after the source file, so two recorders with the same file name
        (ZOOM0001.WAV on two Zooms, T001.WAV on two Sound Devices) would overwrite each other. Those get a
        uniquely named link (or copy) for the Auto-Align pass; outputs keep the original names."""
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
                    try:
                        os.link(s['path'], dst)
                    except OSError:
                        shutil.copyfile(s['path'], dst)
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
        with open(os.path.join(self.mid, 'aap_plan.json'), 'w') as f:
            json.dump(plan, f, indent=1, default=str)
        n_items = sum(1 for i in plan['items'] if i['kind'] == 'source')
        self.log(f"Resolve timeline: {len(refs)} reference piece(s) on A1, {n_items} piece(s) to align on "
                 f"A2–A{len(plan['tracks'])}")
        render_dir = self.s.get('render_dir') or os.path.join(self.out, 'AAP_RENDERS')
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
        if self.s.get('_fake_aap'):          # test hook
            self.s['_fake_aap'](plan, render_dir)
        else:
            bridge = RA.ResolveBridge(log=self.log)
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
                        RA.open_aap_panel(self.log)
                        time.sleep(4)
                        RA.dump_aap_tree(os.path.join(self.mid, 'aap_panel_tree.txt'))
                        pressed = RA.press_render(self.log)
                    else:
                        self.log(f'macOS has not given {APP_NAME} Accessibility access yet, so Render needs your click.')
                except Exception as e:  # noqa: BLE001
                    self.log(f'Could not press Render automatically: {e}')
            ask = dict(render_dir=render_dir, ref_track=f"Audio {info.get('ref_track', 1)}",
                       sources=f"Audio {plan['tracks'][1]['index'] + (info.get('ref_track', 1) - 1)}–"
                               f"{plan['tracks'][-1]['index'] + (info.get('ref_track', 1) - 1)}" if len(plan['tracks']) > 1 else '',
                       project=info.get('project', ''), pressed=pressed, error=info.get('error'),
                       want=sum(plan['expected'].values()))
            self.emit('aap_wait', ask)
        # wait for renders
        self.user_continue.clear()
        last = None
        while True:
            self.check()
            st = watcher.status()
            if st != last:
                self.emit('aap_status', dict(done=st['done'], want=st['want'], seen=st['seen']))
                last = st
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
        if not st['files']:
            self.log('No Auto-Align renders found; exporting unaligned audio.')
            self.aap_info['status'] = 'no renders'
            return
        self._assemble(sources, st)

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
        note = f"{ref['roll']} CH{ref['channel'] + 1}" if ref else 'chosen per stretch (audio only)'
        asm = Assembler(srcs, st['files'], os.path.join(self.out, 'ISO_ALIGNED'), log=self.log, progress=self.progress,
                        note=note, cancel=self.cancelled, ref_windows=self._ref_windows)
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
                self.log('Packing aligned tracks back into their multichannel files:')

                def name_for(stem):
                    return stem if self._names == 'same' else stem + '_ALN'
                self.aligned += RP.repack(self._packs, by_mono, os.path.join(self.out, 'ISO_ALIGNED'), name_for, note,
                                          log=self.log, cancel=self.cancelled)
            for s in self._passthru:      # mix / timecode / silent files: kept as recorded
                self.aligned.append(dict(source=s['path'], aligned=s['path'], roll=s['roll'], start=s['start'],
                                         n=WavInfo(s['path']).nframes, sections=0, aligned_s=0.0,
                                         total_s=round(s['length'] / SR, 1), flags=[f"{s.get('role', 'mix')} track, not aligned"],
                                         passthrough=True))
        except KeyboardInterrupt:
            raise Cancelled()
        self.log(f'{len(self.aligned)} aligned file(s) written.')
        self.step('Checking alignment')
        self.verify = verify_against_ref([a for a in self.aligned if not a.get('passthrough')], self.refs)

    def do_poly(self):
        p = self.p
        if self.mode() == 'audio' and RP.delivery_style(self.audio_sources()) == 'per_take' and not self.s.get('poly_per_take'):
            self.log('Per-take recorder files: the aligned takes are the polywavs (same names and track layout), '
                     'so no day-long polywav. Turn on "Day-long polywavs" to make them anyway.')
            return
        ub = Counter(c.get('ubits') for c in p.clips if c.get('ubits'))
        ubits = ub.most_common(1)[0][0] if ub else ''
        sets = []
        if self.s['poly_aligned'] and self.aligned:
            srcs = [dict(path=a['aligned'], start=a['start'], roll=a['roll'],
                         channels=WavInfo(a['aligned']).channels) for a in self.aligned]
            sets.append(('POLY_ALIGNED', srcs, 'ALN', 'Auto-Align Post aligned ISOs'))
        if self.s['poly_unaligned'] or (self.s['poly_aligned'] and not self.aligned):
            srcs = [dict(path=s['path'], start=s['start'], roll=s['roll'], channels=s['channels'])
                    for s in self.audio_sources()]
            sets.append(('POLY_UNALIGNED', srcs, 'RAW', 'unaligned ISOs'))
        for folder, srcs, tag, note in sets:
            if not srcs:
                continue
            order = [r['name'] for r in p.audio_rolls_ordered()]
            srcs.sort(key=lambda s: (order.index(s['roll']) if s['roll'] in order else 999, s['start']))
            names, idx = self.channel_layout(srcs)
            for s in srcs:
                s['channels'] = idx[s['roll']][:s['channels']]
            self.s['poly_channels'] = names
            out = build_polywavs(srcs, names, os.path.join(self.out, folder), f"{self.s['job_name']}_POLY_{tag}",
                                 p.fps, p.df, ubits, self.s['job_name'], note, self.s['chunk_mode'], log=self.log,
                                 progress=self.progress, cancel=self.cancelled)
            for o in out:
                o['folder'] = folder
                o['channels'] = names
            self.polys += out

    def do_xml(self):
        p = self.p
        cams = []
        for r in p.camera_rolls():
            clips = []
            for c in r['clips']:
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
                label = f"{r['camera']} {r['name']}".strip() if r['camera'] and r['camera'] not in r['name'] else r['name']
                cams.append(dict(label=label, clips=clips))
        xml_dir = os.path.join(self.out, 'EDITORIAL')
        job = self.s['job_name']
        tracks = []
        if self.s['xml_iso']:
            tracks = []
            if self.aligned:
                by = {}
                for a in self.aligned:
                    by.setdefault(a['roll'], []).append(a)
                order = [r['name'] for r in p.audio_rolls_ordered()]
                for roll in sorted(by, key=lambda r: order.index(r) if r in order else 999):
                    w0 = WavInfo(by[roll][0]['aligned'])
                    for ch in range(w0.channels):
                        tracks.append(dict(label=roll if w0.channels == 1 else f'{roll}_{ch + 1}', clips=[
                            dict(path=a['aligned'], start=a['start'], length=a['n'], kind='audio', channels=w0.channels,
                                 src_track=ch + 1, reel=roll, media_start_frames=TC.samples_to_frames(a['start'], p.fps))
                            for a in by[roll]]))
            else:
                by = {}
                for s in self.audio_sources():
                    by.setdefault(s['roll'], []).append(s)
                for roll, ss in by.items():
                    for ch in range(ss[0]['channels']):
                        tracks.append(dict(label=roll if ss[0]['channels'] == 1 else f'{roll}_{ch + 1}', clips=[
                            dict(path=s['path'], start=s['start'], length=s['length'], kind='audio', channels=s['channels'],
                                 src_track=ch + 1, reel=roll, media_start_frames=TC.samples_to_frames(s['start'], p.fps))
                            for s in ss]))
            suffix = 'ISO_ALN' if self.aligned else 'ISO'
            path = write_day_xml(os.path.join(xml_dir, f'{job}_SYNC_{suffix}.xml'), f'{job} SYNC {suffix}', p.fps, p.df,
                                 cams, tracks)
            self.xmls.append(os.path.relpath(path, self.out))
        iso_tracks = tracks
        if self.s['xml_poly'] and self.polys:
            folder = 'POLY_ALIGNED' if any(x['folder'] == 'POLY_ALIGNED' for x in self.polys) else 'POLY_UNALIGNED'
            ps = [x for x in self.polys if x['folder'] == folder]
            names = ps[0]['channels']
            tracks = [dict(label=nm, clips=[dict(path=x['path'], start=x['start'], length=x['n'], kind='audio',
                                                 channels=len(names), src_track=i + 1, reel=nm,
                                                 media_start_frames=TC.samples_to_frames(x['start'], p.fps)) for x in ps])
                      for i, nm in enumerate(names)]
            path = write_day_xml(os.path.join(xml_dir, f'{job}_SYNC_{folder}.xml'), f'{job} SYNC {folder}', p.fps,
                                 p.df, cams, tracks)
            self.xmls.append(os.path.relpath(path, self.out))
        for x in self.xmls:
            self.log(f'  wrote {x}')
        self._cams = cams
        self.do_exports(xml_dir, cams, iso_tracks if self.s['xml_iso'] else [])

    def do_exports(self, xml_dir, cams, tracks):
        """OTIO / AAF / ALE / CSV / synced clips. A failure here is logged, never fatal."""
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
                 lambda path: EX.write_aaf(path, job, p.fps, p.df, tracks)),
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
        if self.s.get('synced_clips') and cams:
            self.step('Synced camera clips')
            try:
                made = EX.synced_clips(os.path.join(self.out, 'SYNCED_CLIPS'), cams, tracks, p.fps, p.df, ST.ffmpeg_exe(),
                                       log=self.log, progress=self.progress, cancel=self.cancelled)
                self.log(f'  {len(made)} synced camera clip(s) in SYNCED_CLIPS/')
            except KeyboardInterrupt:
                raise Cancelled()
            except Exception as e:  # noqa: BLE001
                self.log(f'  synced clips failed: {e}')

    def do_report(self):
        self.report_path = os.path.join(self.out, f"{self.s['job_name']}_REPORT.txt")
        st = dict(self.s, _mode=self.mode(), _ref=self.ref(), _ref_summary=self.ref_summary)
        try:
            files = []
            for d in sorted(os.listdir(self.out)):
                full = os.path.join(self.out, d)
                if os.path.isdir(full) and not d.startswith('.'):
                    n = sum(len(fs) for _, _, fs in os.walk(full))
                    files.append((d + '/', f'{n} file(s)'))
            EX.write_readme(os.path.join(self.out, 'READ ME.txt'), self.s['job_name'], self.mode(), files)
        except Exception as e:  # noqa: BLE001
            self.log(f'  could not write READ ME: {e}')
        write_report(self.report_path, self.s['job_name'], self.p, self.sync_report, self.stamped, self.aap_info,
                     self.aligned, self.verify, self.polys, self.xmls, st, self.log_lines)
        self.log(f'Report: {os.path.basename(self.report_path)}')
