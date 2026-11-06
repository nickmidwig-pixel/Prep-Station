"""Midwig Sync window (pywebview). Python owns the project and the job; the page just shows them."""
import json
import os
import subprocess
import sys
import threading
import traceback

from . import tc as TC
from .scan import Project
from .pipeline import Job, DEFAULTS, default_job_name, default_out_dir
from . import resolve_aap as RA
from .manual import ClipTools, parse_tc_input

UI = os.path.join(os.path.dirname(__file__), 'ui', 'index.html')
SETTINGS_FILE = os.path.expanduser('~/Library/Application Support/Midwig Sync/settings.json')


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


class Api:
    def __init__(self):
        self.window = None
        self.p = Project()
        prefs = load_prefs()
        self.settings = dict(DEFAULTS)
        for k in ('poly_unaligned', 'stamp_corrected_video', 'chunk_mode', 'aap_auto_press', 'waveform_check',
                  'xml_iso', 'xml_poly', 'poly_aligned', 'aap_mode'):
            if k in prefs:
                self.settings[k] = prefs[k]
        self.job = None
        self.lock = threading.Lock()
        self.scanning = False
        self.user_named = False
        self.user_out = False

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
        return dict(project=pj, settings=s, running=bool(self.job and self.job_thread and self.job_thread.is_alive()),
                    scanning=self.scanning, platform=sys.platform)

    job_thread = None

    # ------------------------------------------------------------ called from the page
    def get_state(self):
        return self._state()

    def add_paths(self, paths):
        paths = [p for p in (paths or []) if p and os.path.exists(p)]
        if not paths or self.scanning:
            return False

        def work():
            self.scanning = True
            self.emit('scan', dict(active=True, done=0, total=0, name=''))
            try:
                self.p.add_paths(paths, progress=lambda i, n, name: self.emit('scan', dict(active=True, done=i, total=n, name=name)))
                self.p.check_short_clips(self.settings.get('short_clip_s', 20))
                self._refresh_defaults()
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
            self.settings['reference'] = self.p.default_reference()

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
            self.settings['out_dir'] = os.path.join(base, f"{self.settings.get('job_name') or 'MIDWIG'}_MIDWIG")
            self.user_out = True
        return self._state()

    def set_setting(self, key, value):
        if key == 'job_name':
            value = ''.join(ch for ch in str(value) if ch.isalnum() or ch in '_-. ').strip().replace(' ', '_')
            self.user_named = True
            if not self.user_out and self.p.clips:
                self.settings['job_name'] = value
                self.settings['out_dir'] = default_out_dir(self.p, value)
        self.settings[key] = value
        prefs = load_prefs()
        if key in ('poly_unaligned', 'stamp_corrected_video', 'chunk_mode', 'aap_auto_press', 'waveform_check',
                   'xml_iso', 'xml_poly', 'poly_aligned', 'aap_mode'):
            prefs[key] = value
            save_prefs(prefs)
        return self._state()

    def set_reference(self, roll, channel):
        self.settings['reference'] = dict(roll=roll, channel=int(channel))
        return self._state()

    def set_include(self, cid, include):
        c = self.p.by_id(int(cid))
        if c:
            c['include'] = bool(include)
        return self._state()

    def remove_clips(self, ids):
        self.p.remove([int(i) for i in ids])
        self._refresh_defaults()
        return self._state()

    def clear(self):
        if self.job_thread and self.job_thread.is_alive():
            return self._state()
        self.p = Project()
        self.settings['job_name'] = ''
        self.settings['out_dir'] = ''
        self.settings['reference'] = None
        self.user_named = self.user_out = False
        return self._state()

    def run(self):
        if self.job_thread and self.job_thread.is_alive():
            return False
        if not self.p.clips:
            return False
        # a re-run starts from the files' own timecode again
        for c in self.p.clips:
            c['start'] = c['orig_start']
            c['flags'] = [f for f in c['flags'] if not f.startswith('sync')]
            for k in ('stamped_path', 'stamped_start', 'sync'):
                c.pop(k, None)
        self.p.analyze()
        self.p.check_short_clips(self.settings.get('short_clip_s', 20))
        self.job = Job(self.p, dict(self.settings), self.emit)
        self.job_thread = threading.Thread(target=self.job.run, daemon=True)
        self.job_thread.start()
        return True

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
                    self.emit('log', 'Give Midwig Sync Accessibility access in System Settings › Privacy & Security, then try again.')
                    return
                RA.open_aap_panel(lambda m: self.emit('log', m))
                ok = RA.press_render(lambda m: self.emit('log', m), wait_s=20)
                if self.job:
                    RA.dump_aap_tree(os.path.join(self.job.mid, 'aap_panel_tree.txt'))
                self.emit('aap_pressed', dict(ok=ok))
            except Exception as e:  # noqa: BLE001
                self.emit('log', f'Could not press Render: {e}')
                self.emit('aap_pressed', dict(ok=False))
        threading.Thread(target=work, daemon=True).start()
        return True

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
        c = self.p.by_id(int(cid))
        if not c or self._busy():
            return self._state()
        self._tools().set_tc(c, int(start), 'waveform', note)
        return self._with_msg(f"{c['name']} placed at {TC.samples_to_tc(int(start), self.p.fps, self.p.df)}.")

    def clip_reset(self, cid):
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
        try:
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
    """Show 'Midwig Sync' + its icon in the Dock and menu bar (we run inside a plain Python process)."""
    if sys.platform != 'darwin':
        return
    try:
        from Foundation import NSBundle
        info = NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info['CFBundleName'] = 'Midwig Sync'
        from AppKit import NSApplication, NSImage
        icon = os.environ.get('MIDWIG_ICON')
        if icon and os.path.exists(icon):
            NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(icon))
    except Exception:  # noqa: BLE001
        pass


def main():
    import webview
    from webview.dom import DOMEventHandler
    _mac_identity()

    api = Api()
    window = webview.create_window('Midwig Sync', UI, js_api=api, width=1440, height=900, min_size=(1100, 700),
                                   background_color='#1E2328')
    api.window = window

    def on_drop(e):
        files = (e.get('dataTransfer') or {}).get('files') or []
        paths = [f.get('pywebviewFullPath') for f in files if f.get('pywebviewFullPath')]
        if paths:
            api.add_paths(paths)

    def setup(win):
        win.dom.document.events.drop += DOMEventHandler(on_drop, True, True)

    webview.start(setup, window, debug='--debug' in sys.argv)


if __name__ == '__main__':
    main()
