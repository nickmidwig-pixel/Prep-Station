"""App window (pywebview). Python owns the project and the job; the page just shows them."""
import copy
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback

from . import tc as TC
from .scan import Project
from .pipeline import Job, DEFAULTS, default_job_name, default_out_dir
from . import resolve_aap as RA
from .manual import ClipTools, parse_tc_input
from . import preflight as PF

UI = os.path.join(os.path.dirname(__file__), 'ui', 'index.html')
from . import safety as SAFE
from . import media
from .brand import CACHE, APP_NAME, APP_VERSION, SETTINGS_FILE, OUT_SUFFIX


PREF_KEYS = ('chunk_mode', 'poly_style', 'poly_layout', 'aap_auto_press', 'waveform_check', 'iso_folder', 'make_polys', 'review_tc', 'aap_mode', 'mode', 'export_xml', 'export_otio',
             'export_aaf', 'export_ale', 'export_csv', 'aligned_names', 'transcribe', 'transcribe_language')
OLD_PREFS = ('poly_unaligned', 'stamp_corrected_video', 'xml_iso', 'xml_poly', 'poly_aligned', 'synced_clips',
             'poly_per_take')


def load_prefs():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_prefs(d):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(d, f, indent=1)
    except Exception:  # noqa: BLE001
        pass


def _data_uri(data):
    """data: URI with the right type (Quick Look hands back PNG, everything else is JPEG)."""
    import base64
    mime = 'image/png' if data[:8] == b'\x89PNG\r\n\x1a\n' else 'image/jpeg'
    return f'data:{mime};base64,' + base64.b64encode(data).decode()


from .brand import LOGS  # noqa: E402


class Api:
    def __init__(self):
        self.window = None
        self.p = Project()
        prefs = load_prefs()
        self.settings = dict(DEFAULTS)
        self.settings['review_tc'] = True      # the window shows the timecode fixes before writing (you can switch it off)
        for k in PREF_KEYS:
            if k in prefs:
                self.settings[k] = prefs[k]
        self.jobs = []
        self.job = None
        self.lock = threading.Lock()
        self.scanning = False
        self.user_named = False
        self.user_out = False
        self.undo_stack = []       # [(label, snapshot)] newest last
        self.redo_stack = []

    # ------------------------------------------------------------ undo / redo
    # Every edit you make in the window (which clips are used, timecode you set or picked, slow-motion marks,
    # cameras grouped or relettered, frame rate, reference mic, files added, removed or cleared) is one step.
    # Snapshots hold the edit fields of every clip plus the project-level choices; files on disk are never involved.
    EDIT_KEYS = ('include', 'manual', 'start', 'flags', 'slowmo_user', 'sync')
    UNDO_DEPTH = 200

    def _snap(self):
        p = self.p
        return dict(
            clips=list(p.clips), vals=[(c, copy.deepcopy({k: c.get(k) for k in self.EDIT_KEYS})) for c in p.clips],
            proj=copy.deepcopy(dict(fps_override=p.fps_override, dropped=p.dropped, reports=p.reports,
                                    unsupported=p.unsupported, errors=p.errors, lane_user=getattr(p, 'lane_user', {}))),
            settings=copy.deepcopy({k: self.settings.get(k) for k in ('reference', 'mode', 'job_name', 'out_dir')}),
            flags=(self.user_named, self.user_out))

    def _diff(self, a, b):
        """Only what one edit changed (so undoing it never touches what a sync run did in between)."""
        d = dict(clips=None, vals=[], proj={}, settings={}, flags=None)
        if not (len(a['clips']) == len(b['clips']) and all(x is y for x, y in zip(a['clips'], b['clips']))):
            d['clips'] = (a['clips'], b['clips'])
        av = {id(c): (c, v) for c, v in a['vals']}
        bv = {id(c): v for c, v in b['vals']}
        for k, (c, va) in av.items():
            vb = bv.get(k)
            if vb is None:
                continue                       # removed: it comes back with the clip list, as it was
            ch = {f: (va[f], vb[f]) for f in self.EDIT_KEYS if va[f] != vb[f]}
            if ch:
                d['vals'].append((c, ch))
        for part in ('proj', 'settings'):
            for k in a[part]:
                if a[part][k] != b[part][k]:
                    d[part][k] = (a[part][k], b[part][k])
        if a['flags'] != b['flags']:
            d['flags'] = (a['flags'], b['flags'])
        return d if (d['clips'] or d['vals'] or d['proj'] or d['settings'] or d['flags']) else None

    def _apply(self, d, which):
        """which 0 = as before the edit (undo), 1 = as after it (redo)."""
        p = self.p
        if d['clips']:
            p.clips = list(d['clips'][which])
        for c, ch in d['vals']:
            for k, pair in ch.items():
                c[k] = copy.deepcopy(pair[which])
        for k, pair in d['proj'].items():
            setattr(p, k, copy.deepcopy(pair[which]))
        for k, pair in d['settings'].items():
            self.settings[k] = copy.deepcopy(pair[which])
        if d['flags']:
            self.user_named, self.user_out = d['flags'][which]
        if p.clips:
            p.assign_days()
        p.analyze()

    def _push(self, label, before):
        d = self._diff(before, self._snap())
        if d:
            self.undo_stack.append((label, d))
            del self.undo_stack[:-self.UNDO_DEPTH]
            self.redo_stack.clear()
        return bool(d)

    def _edit(self, label, fn):
        """Run one edit; if it changed anything, it becomes one undo step."""
        before = self._snap()
        res = fn()
        if self._push(label, before) and isinstance(res, dict) and 'project' in res:
            res.update(undo=label, redo=None)
        return res

    def undo(self):
        if self._busy() or self.scanning or not self.undo_stack:
            return self._state()
        label, d = self.undo_stack.pop()
        self._apply(d, 0)
        self.redo_stack.append((label, d))
        st = self._state()
        st['toast'] = f'Undid {label}'
        return st

    def redo(self):
        if self._busy() or self.scanning or not self.redo_stack:
            return self._state()
        label, d = self.redo_stack.pop()
        self._apply(d, 1)
        self.undo_stack.append((label, d))
        st = self._state()
        st['toast'] = f'Redid {label}'
        return st

    # ------------------------------------------------------------ push to page
    def emit(self, kind, data=None):
        if not self.window:
            return
        try:
            self.window.evaluate_js(f'window.MS && window.MS.event({json.dumps(kind)}, {json.dumps(data, default=str)})')
        except Exception:  # noqa: BLE001
            pass

    def _state(self):
        pj = self.p.to_json()
        s = {k: v for k, v in self.settings.items() if not k.startswith('_')}
        day_names = {}
        if len(pj.get('days') or []) > 1:
            for d in pj['days']:
                try:
                    day_names[d['key']] = self._day_names(d['key'])
                except Exception:  # noqa: BLE001
                    pass
        return dict(project=pj, settings=s,
                    running=bool(self.job and self.job_thread and self.job_thread.is_alive()),
                    scanning=self.scanning, platform=sys.platform, app=dict(name=APP_NAME, version=APP_VERSION),
                    day_names=day_names, user_named=self.user_named,
                    undo=self.undo_stack[-1][0] if self.undo_stack else None,
                    redo=self.redo_stack[-1][0] if self.redo_stack else None)

    job_thread = None

    # ------------------------------------------------------------ called from the page
    def get_state(self):
        return self._state()

    def add_paths(self, paths):
        paths = [p for p in (paths or []) if p and os.path.exists(p)]
        if not paths or self.scanning:
            return False
        if self._busy():
            self.emit('notice', dict(text='Wait for the run to finish before adding files.', ok=False))
            return False

        before = self._snap()

        def work():
            self.scanning = True
            self.emit('scan', dict(active=True, done=0, total=0, name=''))
            try:
                n0 = len(self.p.clips)
                self.p.add_paths(paths, progress=lambda i, n, name: self.emit('scan', dict(active=True, done=i, total=n, name=name)))
                self.p.check_short_clips(self.settings.get('short_clip_s', 20))
                self._refresh_defaults()
                added = len(self.p.clips) - n0
                if added:
                    self._push(f"adding {added} file{'s' if added != 1 else ''}", before)
            except Exception as e:  # noqa: BLE001
                self.emit('error', dict(message=f'Could not read the files: {e}', trace=traceback.format_exc()[-1500:]))
            finally:
                self.scanning = False
                self.emit('scan', dict(active=False))
                self.emit('state', self._state())
        threading.Thread(target=work, daemon=True).start()
        return True

    def _refresh_defaults(self):
        if not self.p.clips:
            return
        if not self.user_named or not self.settings.get('job_name'):
            self.settings['job_name'] = default_job_name(self.p)
        if not self.user_out or not self.settings.get('out_dir'):
            self.settings['out_dir'] = default_out_dir(self.p, self.settings['job_name'])
        ref = self.settings.get('reference')
        rolls = {r['name'] for r in self.p.camera_rolls()}
        if not ref or ref.get('roll') not in rolls:
            dr = self.p.default_reference()
            self.settings['reference'] = dict(dr, auto=True) if dr else None

    @staticmethod
    def _dlg(kind):
        import webview
        fd = getattr(webview, 'FileDialog', None)
        if fd is not None:
            return getattr(fd, kind)
        return getattr(webview, f'{kind}_DIALOG')

    def choose_folders(self):
        res = self.window.create_file_dialog(self._dlg('FOLDER'), allow_multiple=True)
        if res:
            self.add_paths(list(res))
        return bool(res)

    def choose_files(self):
        res = self.window.create_file_dialog(self._dlg('OPEN'), allow_multiple=True)
        if res:
            self.add_paths(list(res))
        return bool(res)

    def choose_output(self):
        res = self.window.create_file_dialog(self._dlg('FOLDER'), allow_multiple=False)
        if res:
            base = res[0] if isinstance(res, (list, tuple)) else res
            job = self.settings.get('job_name') or (default_job_name(self.p) if self.p.clips else '')
            if not job:
                import datetime as _dt
                job = _dt.date.today().strftime('%Y%m%d')
            self.settings['out_dir'] = os.path.join(base, f"{job}{OUT_SUFFIX}")
            self.user_out = True
        return self._state()

    def set_setting(self, key, value):
        if key in ('mode', 'job_name') and not getattr(self, '_in_edit', False):
            self._in_edit = True
            try:
                return self._edit({'mode': 'the sync-to choice', 'job_name': 'the job name'}[key],
                                  lambda: self.set_setting(key, value))
            finally:
                self._in_edit = False
        if key == 'job_name':
            value = ''.join(ch for ch in str(value) if ch.isalnum() or ch in '_-. ').strip().replace(' ', '_')
            self.user_named = True
            if not self.user_out and self.p.clips:
                self.settings['job_name'] = value
                self.settings['out_dir'] = default_out_dir(self.p, value)
        self.settings[key] = value
        prefs = load_prefs()
        if key in PREF_KEYS:
            prefs[key] = value
            save_prefs(prefs)
        return self._state()

    def set_fps(self, choice):
        return self._edit('the frame rate', lambda: self._set_fps_raw(choice))

    def _set_fps_raw(self, choice):
        """Project frame rate: 'auto' lets the day decide (23.976 unless it clearly isn't); anything else is yours."""
        if self._busy():
            return self._state()
        self.p.set_fps(choice)
        return self._state()

    def clip_slowmo(self, cid, value):
        return self._edit(f'the slow-motion mark on {self._cname(cid)}', lambda: self._clip_slowmo_raw(cid, value))

    def _clip_slowmo_raw(self, cid, value):
        """Mark (True), unmark (False) or let the app decide (None) whether a clip is slow motion."""
        c = self.p.by_id(int(cid))
        if c and not self._busy():
            c['slowmo_user'] = None if value is None else bool(value)
            self.p.analyze()
        return self._state()

    def clips_slowmo(self, cids, value):
        """Mark (True) or unmark (False) several clips as slow motion at once."""
        return self._edit('slow motion', lambda: self._clips_slowmo_raw(cids, value))

    def _clips_slowmo_raw(self, cids, value):
        if self._busy():
            return self._state()
        for cid in cids or []:
            c = self.p.by_id(int(cid))
            if c and c['kind'] == 'video':
                c['slowmo_user'] = None if value is None else bool(value)
        self.p.analyze()
        return self._state()

    # ------------------------------------------------------------ listening and looking (read-only previews)
    def _covering(self, at, kinds=('audio', 'video')):
        return [c for c in self.p.clips if c['kind'] in kinds and c['include'] and c['start'] is not None
                and c['start'] <= at < c['start'] + c['length'] and media.total_audio_channels(c) + (c['kind'] == 'video') > 0]

    def play(self, at, cid=None, ms=None, seconds=180, speed=1.0, reverse=False):
        """Play from the playhead: the selected clip if it's under the playhead, else the recorder's mix (or boom)
        there, else the reference camera's sound. A preview copy goes to the app's cache; afplay plays it.
        With mute / solo set on lanes (ms = {mute: [lane keys], solo: [...]}) every audible lane under the playhead
        is mixed instead (its best channel), 60 s at a time.
        speed (1, 2, 4, 8) and reverse are JKL shuttling: the window is rendered backwards / faster first, so the
        returned dur is real seconds and the picture side advances the playhead by speed * dur.
        Requests are serialised by a generation number: a request that was overtaken (Space again, K, a newer J/L,
        or the window closing) while it was still rendering never starts a player."""
        gen = self._play_begin()
        at = int(at)
        speed = max(1.0, min(16.0, float(speed or 1.0)))
        if ms and (ms.get('mute') or ms.get('solo')):
            return self._play_mix(at, set(ms.get('mute') or []), set(ms.get('solo') or []), speed=speed, reverse=reverse, gen=gen)
        c = self.p.by_id(int(cid)) if cid is not None else None
        if c and not (c['start'] is not None and c['start'] <= at < c['start'] + c['length']):
            c = None
        if c is None:
            snd = self._covering(at, ('audio',))
            if snd:
                def rank(x):
                    roles = Project.clip_roles(x)
                    return (0 if 'mix' in roles else 1 if 'boom' in roles else 2, x['roll'])
                c = sorted(snd, key=rank)[0]
            else:
                cams = [x for x in self._covering(at, ('video',)) if media.total_audio_channels(x)]
                ref = (self.settings.get('reference') or {}).get('roll')
                c = sorted(cams, key=lambda x: x['roll'] != ref)[0] if cams else None
        if c is None:
            nxt = sorted([x for x in self.p.clips if x['include'] and x['start'] is not None and x['start'] > at
                          and media.total_audio_channels(x)], key=lambda x: x['start'])
            if not nxt:
                return dict(ok=False, why='No sound at the playhead.')
            c = nxt[0]
            at = c['start']
        if not media.total_audio_channels(c):
            return dict(ok=False, why=f"{c['name']} has no sound.")
        if reverse:
            dur = min(seconds * speed, max(0.5, (at - c['start']) / TC.SR))
            if dur < 0.5:
                return dict(ok=False, why='Nothing before the playhead in that clip.')
            off = max(0, at - c['start']) / TC.SR - dur
        else:
            off = max(0, at - c['start']) / TC.SR
            dur = min(seconds * speed, max(0.5, (c['start'] + c['length'] - at) / TC.SR))
        roles = Project.clip_roles(c) if c['kind'] == 'audio' else ['cam'] * media.total_audio_channels(c)
        n = len(roles)
        pick = [i for i, r in enumerate(roles) if r == 'mix'][:2] or [i for i, r in enumerate(roles) if r == 'boom'][:1] \
            or [i for i, r in enumerate(roles) if r not in ('tc', 'empty')][:2] or [0]
        if c['kind'] == 'video':
            ltc = (c.get('ltc') or {}).get('channel')
            pick = [i for i in range(min(n, 2)) if i != ltc] or [0]
        pan = f"pan=stereo|c0=c{pick[0]}|c1=c{pick[-1]}"
        src = c['path']
        streams = c.get('audio_streams') or []
        if c['kind'] == 'video' and streams and streams[0].get('path'):
            src = streams[0]['path']
        prev = os.path.join(CACHE, 'preview')
        os.makedirs(prev, exist_ok=True)
        out = os.path.join(prev, f'play_{gen % 4}.wav')
        cmd = [media.ffmpeg_exe() if hasattr(media, 'ffmpeg_exe') else __import__('midwigsync.convert', fromlist=['x']).ffmpeg_exe(),
               '-y', '-v', 'error', '-ss', f'{off:.3f}', '-t', f'{dur:.3f}', '-i', src, '-map', '0:a:0', '-af', pan,
               '-ar', '48000', '-c:a', 'pcm_s16le', out]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out):
            return dict(ok=False, why='Could not read the sound there.')
        out = self._shuttle(out, speed, reverse)
        if sys.platform != 'darwin':
            return dict(ok=False, why='Playback works on the Mac.')
        if not self._play_start(gen, out):
            return dict(ok=False, cancelled=True, why='')
        names = c.get('track_names') or {}
        what = ' + '.join(dict.fromkeys(str(names.get(i + 1) or names.get(str(i + 1)) or f'ch {i + 1}') for i in pick)) \
            if c['kind'] == 'audio' else f"ch {pick[0] + 1}" + (f"–{pick[-1] + 1}" if len(pick) > 1 else '')
        return dict(ok=True, from_=at, dur=dur / speed, speed=speed, reverse=bool(reverse),
                    what=f"{c['roll']} · {c['name']} ({what})", **{'from': at})

    @staticmethod
    def _shuttle(wav, speed, reverse):
        """Render a preview wav backwards and/or faster (ffmpeg areverse / atempo) for JKL shuttling."""
        if speed == 1.0 and not reverse:
            return wav
        filt = []
        if reverse:
            filt.append('areverse')
        sp = speed
        while sp > 1.0001:
            step = min(2.0, sp)
            filt.append(f'atempo={step:.4f}')
            sp /= step
        out = os.path.splitext(wav)[0] + '_shuttle.wav'
        ff = media.ffmpeg_exe() if hasattr(media, 'ffmpeg_exe') else __import__('midwigsync.convert', fromlist=['x']).ffmpeg_exe()
        r = subprocess.run([ff, '-y', '-v', 'error', '-i', wav, '-af', ','.join(filt), '-c:a', 'pcm_s16le', out],
                           capture_output=True, text=True)
        return out if r.returncode == 0 and os.path.exists(out) else wav

    def _play_mix(self, at, mute, solo, seconds=60, speed=1.0, reverse=False, gen=None):
        import numpy as np
        from .refpick import _read as rd
        from .bwf import WavWriter
        lanes = self.p.lanes()
        audible = [l for l in lanes if l['key'] not in mute and (not solo or l['key'] in solo)]
        if not audible:
            return dict(ok=False, why='Every lane is muted.')
        keys = {l['key'] for l in audible}
        here = [c for c in self._covering(at) if (c.get('lane') or c['roll']) in keys]
        if not here:
            nxt = sorted([c for c in self.p.clips if c['include'] and c['start'] is not None and c['start'] > at
                          and (c.get('lane') or c['roll']) in keys and media.total_audio_channels(c)], key=lambda c: c['start'])
            if not nxt:
                return dict(ok=False, why='No sound at the playhead on the lanes you have on.')
            at = nxt[0]['start']
            here = [c for c in self._covering(at) if (c.get('lane') or c['roll']) in keys]
        if reverse:
            first = min(c['start'] for c in here)
            n = int(min(seconds * speed * TC.SR, max(TC.SR // 2, at - first)))
            if at - first < TC.SR // 2:
                return dict(ok=False, why='Nothing before the playhead on the lanes you have on.')
            at = at - n
        else:
            end = max(c['start'] + c['length'] for c in here)
            n = int(min(seconds * speed * TC.SR, max(TC.SR // 2, end - at)))
        mix = np.zeros(n, np.float32)
        names = []
        ref = self.settings.get('reference') or {}
        for c in here:
            if c['kind'] == 'video':
                ch = int(ref.get('channel') or 0) if c['roll'] in (ref.get('rolls') or [ref.get('roll')]) else 0
                ltc = (c.get('ltc') or {}).get('channel')
                if ch == ltc:
                    ch = 1 if media.total_audio_channels(c) > 1 else 0
                picks = [ch]
            else:
                roles = Project.clip_roles(c)
                picks = [i for i, r in enumerate(roles) if r == 'mix'][:1] or [i for i, r in enumerate(roles) if r == 'boom'][:1] \
                    or [i for i, r in enumerate(roles) if r not in ('tc', 'empty')][:1] or [0]
            try:
                x = rd(c, picks[0], at, n)
            except Exception:  # noqa: BLE001
                continue
            pk = float(np.abs(x).max())
            if pk > 1e-5:
                mix += x / pk * 0.5
                names.append(c['roll'] if c['kind'] == 'video' else c['name'])
        if not names:
            return dict(ok=False, why='Silence there on the lanes you have on.')
        mix = mix / (float(np.abs(mix).max()) or 1.0) * 0.9
        prev = os.path.join(CACHE, 'preview')
        os.makedirs(prev, exist_ok=True)
        out = os.path.join(prev, f'mix_{(gen or 0) % 4}.wav')
        ww = WavWriter(out, n, 1, TC.SR, 16)
        ww.write_float(mix)
        ww.close()
        out = self._shuttle(out, speed, reverse)
        if sys.platform != 'darwin':
            return dict(ok=False, why='Playback works on the Mac.')
        if not self._play_start(gen, out):
            return dict(ok=False, cancelled=True, why='')
        # from = where the sound starts in timeline terms: the playhead going forward, the window's end going back
        return dict(ok=True, dur=n / TC.SR / speed, speed=speed, reverse=bool(reverse), what=' + '.join(names),
                    **{'from': at + n if reverse else at})

    # -- one player at a time ---------------------------------------------------------------------------------
    _play_gen = 0
    _play_lock = threading.Lock()
    _player = None

    def _play_begin(self):
        """Claim a new playback generation: whatever was playing or rendering is over."""
        with self._play_lock:
            self._play_gen += 1
            gen = self._play_gen
            self._kill_player()
        return gen

    def _play_start(self, gen, path):
        """Start afplay for this generation unless a newer request or a stop overtook it while it rendered."""
        with self._play_lock:
            if gen is not None and gen != self._play_gen:
                return False
            self._kill_player()
            self._player = subprocess.Popen(['afplay', path])
            return True

    def _kill_player(self):
        p = self._player
        self._player = None
        if p and p.poll() is None:
            try:
                p.terminate()
                try:
                    p.wait(0.3)
                except subprocess.TimeoutExpired:
                    p.kill()
            except OSError:
                pass

    def stop_play(self):
        with self._play_lock:
            self._play_gen += 1        # a render still in flight must not start its player
            self._kill_player()
        return True

    def frame(self, at, cid=None):
        """A still of the picture at the playhead (the selected camera clip, else A camera, else any camera)."""
        at = int(at)
        cams = [x for x in self.p.clips if x['kind'] == 'video' and x['include'] and x['start'] is not None
                and x['start'] <= at < x['start'] + x['length']]
        c = self.p.by_id(int(cid)) if cid is not None else None
        if c and c['kind'] == 'video' and c['start'] is not None and c['include'] and c not in cams:
            # the selected clip always wins, even when the playhead sits just outside it: show its first frame
            cams.insert(0, c)
            # never ask for a frame past the clip's last one (decoders give nothing there): stay two frames inside
            at = min(max(at, c['start']), c['start'] + max(0, c['length'] - int(2 * TC.SR / float(self.p.fps))))
        if not (c and c in cams):
            lanes = {l['key']: l for l in self.p.lanes()}
            cams.sort(key=lambda x: (lanes.get(x.get('lane'), {}).get('letter') or 'Z', x['name']))
            c = cams[0] if cams else None
        if c is None:
            return dict(ok=False, why='No camera rolling here.')
        lane = next((l for l in self.p.lanes() if l['key'] == c.get('lane')), None)
        who = f"{lane['label'] if lane else c['roll']}{' · slow motion' if c.get('slowmo') else ''} · {c['name']}"
        src = c['path']
        off = max(0, at - c['start']) / TC.SR
        if c.get('raw_picture'):
            key = (src, round(off, 1))
            cache = self.__dict__.setdefault('_frames', {})
            if key not in cache:
                data = self._any_frame(c, off, 608)    # the real frame: video library, macOS, then the maker's own tool
                if data:
                    if len(cache) > 200:
                        cache.clear()
                    cache[key] = _data_uri(data)
            if key in cache:
                return dict(ok=True, img=cache[key], label=who)
            img = self._still(c, 608)
            if img:                    # Canon RAW: the picture the camera saved with the clip (its first frame)
                return dict(ok=True, img=img, label=who, preview=True)
            need = __import__('midwigsync.rawtools', fromlist=['x']).tool_for(src, 'arriraw' if src.lower().endswith('.mxf') else '')
            return dict(ok=False, why=f"{c['name']}: RAW picture. " + (
                f"To see it here, install {need[0]}." if need and not need[1] else "This Mac couldn't decode it.")
                + " Sound and timecode work as normal.")
        key = (src, round(off, 1))
        cache = self.__dict__.setdefault('_frames', {})
        if key not in cache:
            ff = __import__('midwigsync.convert', fromlist=['x']).ffmpeg_exe()
            r = subprocess.run([ff, '-v', 'error', '-ss', f'{off:.3f}', '-i', src, '-frames:v', '1',
                                '-vf', 'scale=480:-2', '-q:v', '5', '-f', 'image2', '-c:v', 'mjpeg', 'pipe:1'],
                               capture_output=True)
            data = r.stdout if r.returncode == 0 and r.stdout else None
            if not data:               # ProRes RAW (iPhone, Blackmagic app) and the like
                data = self._any_frame(c, off, 480)
            if not data:
                return dict(ok=False, why=f"{c['name']}: this Mac can't show this file's picture here "
                                          f"({c.get('codec') or 'unknown format'}). Its sound still plays.")
            if len(cache) > 200:
                cache.clear()
            cache[key] = _data_uri(data)
        return dict(ok=True, img=cache[key], label=who)

    def _any_frame(self, c, off, width):
        """Picture bytes of a clip's frame, by whatever can decode it, in order: the app's video library (PyAV: has
        ProRes RAW), macOS, the maker's tool. Every miss is written to Logs/Prep Station/picture.log."""
        src, tried = c['path'], []
        if src.lower().endswith('.crm'):
            return None                 # Canon RAW: only Canon's app decodes it; the stored preview is used instead
        for name, fn in (('video library', lambda: __import__('midwigsync.avframe', fromlist=['x']).frame_jpeg(src, off, width)),
                         ('macOS / maker tool', lambda: self._raw_frame(c, off, width))):
            try:
                data = fn()
            except Exception as e:  # noqa: BLE001
                data = None
                tried.append(f'{name}: {type(e).__name__}: {e}')
            if data:
                return data
            tried.append(f'{name}: nothing') if not tried or not tried[-1].startswith(name) else None
        try:
            import time
            os.makedirs(LOGS, exist_ok=True)
            with open(os.path.join(LOGS, 'picture.log'), 'a') as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {os.path.basename(src)} codec={c.get('codec')} "
                        f"at={off:.2f}s -> no picture ({'; '.join(tried)})\n")
        except OSError:
            pass
        return None

    def _raw_frame(self, c, off, width):
        """JPEG/PNG bytes of a RAW clip's frame `off` seconds in, or None: macOS first (ProRes RAW natively; Blackmagic
        RAW, RED and others when their media extension is installed), then the maker's command-line tool."""
        src = c['path']
        if src.lower().endswith('.crm'):
            return None                 # Canon RAW: nothing on the Mac decodes it outside Canon's app; use the stored preview
        data = __import__('midwigsync.macframe', fromlist=['x']).frame_jpeg(src, off, width)
        if data:
            return data
        rt = __import__('midwigsync.rawtools', fromlist=['x'])
        codec = 'arriraw' if src.lower().endswith('.mxf') else (c.get('codec') or '')    # a RAW picture in MXF is ARRI's
        fp, d = rt.frame_file(src, int(off * float(c.get('fps') or self.p.fps)), width, codec)
        try:
            return self._jpeg_scaled(fp, width) if fp else None
        finally:
            rt.cleanup(d)

    # ------------------------------------------------------------ thumbnails (read-only; cached in the app's cache)
    def _jpeg_scaled(self, data_or_path, width, seek=None):
        ff = __import__('midwigsync.stamp', fromlist=['x']).ffmpeg_exe()
        if isinstance(data_or_path, bytes):
            cmd = [ff, '-v', 'error', '-f', 'image2pipe', '-c:v', 'mjpeg', '-i', 'pipe:0']
            inp = data_or_path
        else:
            cmd = [ff, '-v', 'error'] + (['-ss', f'{seek:.3f}'] if seek else []) + ['-i', data_or_path, '-frames:v', '1']
            inp = None
        cmd += ['-vf', f'scale={width}:-2', '-q:v', '4', '-f', 'image2', '-c:v', 'mjpeg', 'pipe:1']
        try:
            r = subprocess.run(cmd, input=inp, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 and r.stdout else None

    def _still(self, c, width):
        """A small JPEG of a camera clip as a data: URI, or None. Canon RAW: the preview the camera stored in the
        file. Anything ffmpeg can decode: a frame one second in. Cached on disk by file, size and date."""
        import base64
        import hashlib
        try:
            st = os.stat(c['path'])
        except OSError:
            return None
        key = hashlib.sha1(f"{c['path']}|{st.st_size}|{st.st_mtime_ns}|{width}".encode()).hexdigest()
        d = os.path.join(CACHE, 'thumbs')
        fp = os.path.join(d, key + '.jpg')
        data = None
        if os.path.exists(fp):
            with open(fp, 'rb') as f:
                data = f.read()
        else:
            src = media.embedded_preview(c['path']) or media.embedded_preview(c['path'], 'thmb')
            if src:
                data = self._jpeg_scaled(src, width)
            elif c.get('raw_picture'):
                data = self._any_frame(c, min(1.0, c['length'] / TC.SR / 2), width)
            elif not c.get('raw_picture'):
                data = self._jpeg_scaled(c['path'], width, seek=min(1.0, c['length'] / TC.SR / 2))
                if not data:
                    data = self._any_frame(c, min(1.0, c['length'] / TC.SR / 2), width)
            if data:
                os.makedirs(d, exist_ok=True)
                with open(fp, 'wb') as f:
                    f.write(data)
        return _data_uri(data) if data else None

    # ------------------------------------------------------------ RAW viewing copies (Resolve renders; app cache only)
    def thumbs(self, ids):
        """{id: data URI or None} for camera clips, for the clip list (like Tentacle's)."""
        from concurrent.futures import ThreadPoolExecutor
        cl = [c for c in (self.p.by_id(int(i)) for i in ids or []) if c and c['kind'] == 'video']
        with ThreadPoolExecutor(max_workers=4) as ex:
            got = list(ex.map(lambda c: self._still(c, 192), cl))
        return {str(c['id']): g for c, g in zip(cl, got)}

    def open_in_app(self, cid):
        """Open a clip in its own app: Canon RAW in Canon's Cinema RAW Development, anything else in its
        default app. Prep Station only asks macOS to open it; it writes nothing."""
        c = self.p.by_id(int(cid)) if cid is not None else None
        if not c:
            return dict(ok=False, text='Select a clip first.')
        path = c.get('source_path') or c['path']
        if sys.platform != 'darwin':
            return dict(ok=False, text='Opening clips in other apps works on the Mac.')
        crm = path.lower().endswith('.crm')
        cmd = ['open', '-a', 'Cinema RAW Development', path] if crm else ['open', path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 and crm:
            return dict(ok=False, text='Cinema RAW Development isn’t installed. It’s free from Canon’s support site.')
        if r.returncode != 0:
            return dict(ok=False, text=f"macOS couldn't open {c['name']}.")
        return dict(ok=True, text=f"Opened {c['name']} in {'Cinema RAW Development' if crm else 'its app'}.")

    def set_reference(self, roll, channel):
        return self._edit('the reference mic', lambda: self._set_reference_raw(roll, channel))

    def set_sync_to(self, choice):
        """The one Sync-to control (2.0): 'auto' (the app picks), 'audio' (sound only), or a camera lane key
        (align to that camera body; the app still listens for which of its channels is the onboard mic)."""
        return self._edit('what to sync to', lambda: self._set_sync_to_raw(choice))

    def _set_sync_to_raw(self, choice):
        if choice in (None, '', 'auto'):
            self.settings['mode'] = 'auto'
            return self._set_reference_raw('auto', 0)
        if choice == 'audio':
            self.settings['mode'] = 'audio'
            return self._state()
        lane = next((l for l in self.p.lanes() if l['key'] == choice and l['kind'] == 'camera'), None)
        if not lane or not lane.get('rolls'):
            return self._state()
        self.settings['mode'] = 'camera'
        self.settings['reference'] = dict(roll=sorted(lane['rolls'])[0], channel=0, auto=False, listen=True, lane=lane['key'])
        return self._state()

    def _set_reference_raw(self, roll, channel):
        if roll in (None, '', 'auto'):
            dr = self.p.default_reference()
            self.settings['reference'] = dict(dr, auto=True) if dr else None
        else:
            self.settings['reference'] = dict(roll=roll, channel=int(channel), auto=False)
        return self._state()

    def set_include(self, cid, include):
        return self._edit(f"{'using' if include else 'leaving out'} {self._cname(cid)}", lambda: self._set_include_raw(cid, include))

    def _set_include_raw(self, cid, include):
        c = self.p.by_id(int(cid))
        if c:
            c['include'] = bool(include)
            c['user_include'] = bool(include)      # your choice outlasts the false-record check on the next scan
        return self._state()

    def _cname(self, cid):
        try:
            c = self.p.by_id(int(cid))
        except (TypeError, ValueError):
            c = None
        return c['name'] if c else 'a clip'

    def set_include_many(self, ids, include):
        """Use or leave out several clips at once (one undo step)."""
        ids = [int(i) for i in ids or []]
        label = (f"{'using' if include else 'leaving out'} "
                 + (self._cname(ids[0]) if len(ids) == 1 else f'{len(ids)} clips'))

        def go():
            if self._busy():
                return self._state()
            for i in ids:
                c = self.p.by_id(i)
                if c:
                    c['include'] = bool(include)
                    c['user_include'] = bool(include)
            return self._state()
        return self._edit(label, go)

    def group_camera(self, ids, letter=None):
        """Like Tentacle's Create Sync Group (⌘G): the cards these clips came from are one camera. Their lane takes
        `letter`, or the letter of the first selected clip's camera. Only camera clips count."""
        def go():
            if self._busy():
                return self._state()
            cl = [c for c in (self.p.by_id(int(i)) for i in ids or []) if c and c['kind'] == 'video']
            if not cl:
                return self._with_msg('Select camera clips to group them as one camera.', False)
            lanes = {l['key']: l for l in self.p.lanes()}
            first = lanes.get(cl[0].get('lane')) or {}
            L = (letter or first.get('letter') or 'A').upper()[:1]
            for c in cl:
                self.p.lane_user[c['roll']] = L
            self.p.analyze()
            return self._state()
        return self._edit('grouping cameras', go)

    def set_lane_letter(self, lane_key, letter):
        """Give a camera lane another letter (every card on it moves together). Taking a letter another camera has
        swaps them."""
        def go():
            if self._busy():
                return self._state()
            lanes = self.p.lanes()
            ln = next((l for l in lanes if l['key'] == lane_key and l['kind'] == 'camera'), None)
            L = str(letter or '').upper()[:1]
            if not ln or not L.isalpha() or L == ln['letter']:
                return self._state()
            other = next((l for l in lanes if l['kind'] == 'camera' and l['letter'] == L), None)
            if other:
                for r in other['rolls']:
                    self.p.lane_user[r] = ln['letter']
            for r in ln['rolls']:
                self.p.lane_user[r] = L
            self.p.analyze()
            return self._state()
        return self._edit('the camera letter', go)

    def remove_clips(self, ids):
        return self._edit(f"removing {len(ids)} clip{'s' if len(ids) != 1 else ''}", lambda: self._remove_clips_raw(ids))

    def _remove_clips_raw(self, ids):
        if self._busy():
            return self._state()
        self.p.remove([int(i) for i in ids])
        self._refresh_defaults()
        return self._state()

    def clear(self):
        return self._edit('clearing the day', lambda: self._clear_raw())

    def _clear_raw(self):
        if self.job_thread and self.job_thread.is_alive():
            return self._state()
        self.p = Project()
        self.settings['job_name'] = ''
        self.settings['out_dir'] = ''
        self.settings['reference'] = None
        self.user_named = self.user_out = False
        return self._state()

    def run(self, days=None):
        """Run one shoot day, several, or all of them (each day becomes its own job and output folder)."""
        if self._busy() or self.scanning or not self.p.clips:
            return False
        all_days = [d['key'] for d in self.p.days()]
        if days in (None, '', 'all'):
            days = [d for d in all_days if any(c['include'] and (c.get('day') or 'unknown') == d for c in self.p.clips)]
        elif isinstance(days, str):
            days = [days]
        days = [d for d in days if d in all_days]
        if not days:
            return False
        if self.p.fps_info.get('status') == 'ask':
            self.emit('notice', dict(ok=False, text=f"Confirm the frame rate first: {self.p.fps_info['reason']}"))
            return False
        # a re-run starts from the files' own timecode again (positions set by hand are kept)
        for c in self.p.clips:
            c['start'] = c['orig_start']
            c['flags'] = [f for f in c['flags'] if not f.startswith('sync')]
            for k in ('stamped_path', 'stamped_start', 'sync'):
                c.pop(k, None)
        self.p.analyze()
        self.p.check_short_clips(self.settings.get('short_clip_s', 20))
        multi = len(all_days) > 1
        plans = []
        for d in days:
            sub = self.p.subset(d) if multi else self.p
            s = dict(self.settings)
            if multi:
                s.update(self._day_names(d, sub))
            ref = s.get('reference')
            if not ref or ref.get('roll') not in {r['name'] for r in sub.camera_rolls()}:
                s['reference'] = dict(sub.default_reference() or {}, auto=True) if sub.default_reference() else None
            plans.append((d, sub, s))
        self.jobs = []
        outs = []

        def work():
            for i, (d, sub, s) in enumerate(plans):
                last = i == len(plans) - 1
                if multi:
                    self.emit('day', dict(i=i + 1, n=len(plans), key=d, label=next(x['label'] for x in self.p.days() if x['key'] == d)))

                def emit(kind, data, last=last):
                    if kind == 'done':
                        outs.append(data)
                        if not last:
                            return
                        data = dict(data, all=list(outs))
                    if kind == 'project':
                        data = self.p.to_json()
                    if kind == 'review_tc':
                        PF.notify(APP_NAME, f"Timecode check done: {len(data.get('fixes', []))} fix(es) to confirm.")
                    self.emit(kind, data)
                self.job = Job(sub, s, emit)
                self.jobs.append(self.job)
                self.job.run()
                if self.job.cancelled() or self.job.state.get('error'):
                    break
            if self.job and self.job.state.get('error'):
                PF.notify(APP_NAME, f"Stopped with an error: {self.job.state['error'][:120]}", 'Basso')
            elif self.job and not self.job.cancelled():
                PF.notify(APP_NAME, f"Done: {len(outs)} day(s) synced." if len(outs) > 1 else
                          f"Done: {os.path.basename(outs[0]['out']) if outs else 'finished'}")
            # this thread is the job, and it is over: say so (is_alive() is still true in here)
            self.emit('state', dict(self._state(), running=False))
        self.job_thread = threading.Thread(target=work, daemon=True)
        self.job_thread.start()
        return True

    def review_tc(self, choice):
        """Your answer to the timecode fixes: 'write' (corrected copies), 'skip' (files' own timecode), 'stop'."""
        if self.job and choice in ('write', 'skip', 'stop'):
            self.job.decide(choice)
            return True
        return False

    def rewrite_cameras(self):
        """After a run: full-quality copies of camera files whose timecode was fixed (only the timecode changes)."""
        if self._busy() or not self.jobs:
            return False
        jobs = [j for j in self.jobs if j.camera_fixes()]

        def work():
            made = []
            try:
                for j in jobs:
                    j.emit = lambda k, d: self.emit(k, self.p.to_json() if k == 'project' else d)
                    j._cancel.clear()
                    j.step('Writing corrected camera copies')
                    made += j.rewrite_cameras()
                self.emit('cameras_done', dict(made=made, out=jobs[0].out if jobs else ''))
            except Exception as e:  # noqa: BLE001
                self.emit('error', dict(message=f'Camera copies: {e}', trace=traceback.format_exc()[-1500:]))
            # this thread is the job, and it is over: say so (is_alive() is still true in here)
            self.emit('state', dict(self._state(), running=False))
        self.job_thread = threading.Thread(target=work, daemon=True)
        self.job_thread.start()
        return True

    def _day_names(self, d, sub=None):
        """Job name and output folder one shoot day gets when several days are loaded together."""
        if sub is None:
            sub = Project()
            sub.clips = [c for c in self.p.clips if (c.get('day') or 'unknown') == d]
            sub.dropped = self.p.dropped_for(sub.clips)
        date = d.replace('-', '') if d[:4].isdigit() else ''
        if self.user_named and self.settings.get('job_name'):
            base = re.sub(r'^\d{8}_?', '', self.settings['job_name'])
            job = f'{date}_{base}'.strip('_') if date else f"{self.settings['job_name']}_{d}"
        else:
            job = default_job_name(sub, date)
        if self.user_out and self.settings.get('out_dir'):
            out = os.path.join(os.path.dirname(self.settings['out_dir']), f'{job}{OUT_SUFFIX}')
        else:
            out = default_out_dir(sub, job)
        return dict(job_name=job, out_dir=out)

    def cancel(self):
        if self.job:
            self.job.cancel()
        return True

    def aap_continue(self):
        if self.job:
            self.job.user_continue.set()
        return True

    def aap_press_again(self):
        def work():
            try:
                if not RA.ax_trusted(prompt=True):
                    self.emit('log', f'Give {APP_NAME} Accessibility access in System Settings › Privacy & Security, then try again.')
                    return
                RA.dismiss_backend_dialog(lambda m: self.emit('log', m))
                ok, msg = RA.backend_check(lambda m: self.emit('log', m), timeout=20)
                self.emit('log', '  ' + msg)
                if not ok:
                    self.emit('aap_hint', dict(text=msg))
                    return dict(ok=False, why=msg.split('\n')[0])
                RA.close_aap_panel()
                RA.open_aap_panel(lambda m: self.emit('log', m))
                time.sleep(4)
                names = getattr(self.job, '_aap_names', None) if self.job else None
                ref_name, src_names = names if names else ('REF', None)
                if not src_names and self.job:
                    try:
                        with open(os.path.join(self.job.mid, 'aap_plan.json')) as f:
                            src_names = [t['name'] for t in json.load(f)['tracks'][1:]]
                    except Exception:  # noqa: BLE001
                        src_names = None
                ok = RA.press_render(lambda m: self.emit('log', m), wait_s=20, ref_name=ref_name,
                                     source_names=src_names)
                if not ok and self.job:
                    RA.dump_aap_tree(os.path.join(self.job.mid, 'aap_panel_tree.txt'))
                self.emit('aap_pressed', dict(ok=ok))
            except Exception as e:  # noqa: BLE001
                self.emit('log', f'Could not press Render: {e}')
                self.emit('aap_pressed', dict(ok=False))
        threading.Thread(target=work, daemon=True).start()
        return True

    def preflight(self):
        try:
            return PF.run_checks(self.p, self.settings)
        except Exception as e:  # noqa: BLE001
            return [dict(id='err', label='Checks', status='warn', detail=str(e))]

    # ------------------------------------------------------------ one clip, by hand
    def _busy(self):
        return bool(self.job_thread and self.job_thread.is_alive())

    def _tools(self):
        return ClipTools(self.p, ref=self.settings.get('reference') or self.p.default_reference(),
                         log=lambda m: self.emit('log', m),
                         progress=lambda i, n, msg='': self.emit('clip_progress', dict(frac=(i / n) if n else 0, msg=msg)))

    def _with_msg(self, msg=None, ok=True):
        st = self._state()
        st['clip_msg'] = dict(text=msg, ok=ok) if msg else None
        return st

    def clip_set_tc(self, cid, text):
        return self._edit(f'the timecode of {self._cname(cid)}', lambda: self._clip_set_tc_raw(cid, text))

    def _clip_set_tc_raw(self, cid, text):
        c = self.p.by_id(int(cid))
        if not c or self._busy():
            return self._with_msg('Wait for the run to finish.', False) if c else self._state()
        try:
            start = parse_tc_input(text, self.p.fps, self.p.df)
        except (ValueError, TypeError) as e:
            return self._with_msg(str(e) if 'valid' in str(e) else f'“{text}” isn’t a timecode. Use HH:MM:SS:FF.', False)
        self._tools().set_tc(c, start, 'typed')
        return self._with_msg(f"{c['name']} now starts at {TC.samples_to_tc(start, self.p.fps, self.p.df)}.")

    def clip_nudge(self, cid, frames):
        return self._edit(f'the nudge on {self._cname(cid)}', lambda: self._clip_nudge_raw(cid, frames))

    def _clip_nudge_raw(self, cid, frames):
        c = self.p.by_id(int(cid))
        if not c or self._busy():
            return self._state()
        if c['start'] is None:
            return self._with_msg('Type a timecode or find it by waveform first.', False)
        start = int(round(c['start'] + int(frames) * float(TC.samples_per_frame(self.p.fps))))
        how = 'nudged' if not c.get('manual') or c['manual']['how'] in ('nudged', 'waveform', 'typed') else c['manual']['how']
        note = c['manual']['note'] if c.get('manual') else ''
        self._tools().set_tc(c, start, how, note)
        return self._state()

    def clip_accept(self, cid, start, note=''):
        return self._edit(f'placing {self._cname(cid)}', lambda: self._clip_accept_raw(cid, start, note))

    def _clip_accept_raw(self, cid, start, note=''):
        c = self.p.by_id(int(cid))
        if not c or self._busy():
            return self._state()
        self._tools().set_tc(c, int(start), 'waveform', note)
        return self._with_msg(f"{c['name']} placed at {TC.samples_to_tc(int(start), self.p.fps, self.p.df)}.")

    def clip_reset(self, cid):
        return self._edit(f'the timecode of {self._cname(cid)}', lambda: self._clip_reset_raw(cid))

    def _clip_reset_raw(self, cid):
        c = self.p.by_id(int(cid))
        if not c or self._busy():
            return self._state()
        self._tools().clear(c)
        return self._with_msg('Back to the file’s own timecode.' if c['orig_start'] is not None else
                              'Cleared. Sync will look for it by waveform.')

    def clip_find(self, cid, against=None):
        c = self.p.by_id(int(cid))
        if not c or self._busy() or self.scanning:
            return False

        def work():
            try:
                res = self._tools().find(c, against or None)
            except Exception as e:  # noqa: BLE001
                res = dict(error=f'Waveform search failed: {e}', trace=traceback.format_exc()[-1500:])
            res['cid'] = c['id']
            self.emit('clip_find', res)
        threading.Thread(target=work, daemon=True).start()
        return True

    def clip_stamp(self, cid):
        c = self.p.by_id(int(cid))
        if not c:
            return self._state()
        out = self.settings.get('out_dir')
        if not out:
            return self._with_msg('Choose an output folder first.', False)
        bad = SAFE.output_problem(out)
        if bad:
            return self._with_msg(bad, False)
        try:
            SAFE.allow_output(out)
            path = self._tools().stamp_now(c, out)
        except Exception as e:  # noqa: BLE001
            return self._with_msg(str(e), False)
        self.reveal(path)
        return self._with_msg(f'Wrote {os.path.relpath(path, out)}')

    def open_path(self, path):
        if not path:
            return False
        if not os.path.isabs(path) and self.settings.get('out_dir'):
            path = os.path.join(self.settings['out_dir'], path)
        if not os.path.exists(path):
            return False
        if sys.platform == 'darwin':
            subprocess.run(['open', path], check=False)
        return True

    def reveal(self, path):
        if path and os.path.exists(path) and sys.platform == 'darwin':
            subprocess.run(['open', '-R', path], check=False)
        return True

    def copy_text(self, text):
        if sys.platform == 'darwin':
            subprocess.run(['pbcopy'], input=str(text).encode(), check=False)
        return True


def _mac_identity():
    """Show the app's name + icon in the Dock and menu bar (we run inside a plain Python process)."""
    if sys.platform != 'darwin':
        return
    try:
        from Foundation import NSBundle
        info = NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info['CFBundleName'] = APP_NAME
        from AppKit import NSApplication, NSImage
        icon = os.environ.get('APP_ICON')
        if icon and os.path.exists(icon):
            NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(icon))
    except Exception:  # noqa: BLE001
        pass


def main():
    import webview
    from webview.dom import DOMEventHandler
    _mac_identity()

    api = Api()
    window = webview.create_window(APP_NAME, UI, js_api=api, width=1440, height=900, min_size=(1100, 700),
                                   background_color='#1E2328')
    api.window = window

    def on_drop(e):
        files = (e.get('dataTransfer') or {}).get('files') or []
        paths = [f.get('pywebviewFullPath') for f in files if f.get('pywebviewFullPath')]
        if paths:
            api.add_paths(paths)

    def setup(win):
        win.dom.document.events.drop += DOMEventHandler(on_drop, True, True)

    import atexit
    atexit.register(api.stop_play)                 # no preview keeps playing after the app is gone
    window.events.closing += lambda *a, **k: api.stop_play()

    webview.start(setup, window, debug='--debug' in sys.argv)


if __name__ == '__main__':
    main()
