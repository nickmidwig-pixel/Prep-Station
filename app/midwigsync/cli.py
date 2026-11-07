"""Headless runner:  python -m midwigsync.cli <folders...> [--out DIR] [--job NAME] [--no-aap] [--scan-only]"""
import argparse
import json
import sys

from .scan import Project
from .pipeline import Job, default_job_name, default_out_dir
from . import tc as TC


def main(argv=None):
    ap = argparse.ArgumentParser(prog='midwigsync')
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--out')
    ap.add_argument('--job')
    ap.add_argument('--ref-roll')
    ap.add_argument('--ref-channel', type=int, default=1)
    ap.add_argument('--no-aap', action='store_true', help='skip Auto-Align (unaligned exports only)')
    ap.add_argument('--no-waveform', action='store_true')
    ap.add_argument('--transcribe', action='store_true', help='also write a transcript of the day')
    ap.add_argument('--scan-only', action='store_true')
    ap.add_argument('--fps', help="project frame rate, e.g. 23.976, 25, '29.97 DF' (default: the day decides, 23.976 unless clearly not)")
    a = ap.parse_args(argv)
    p = Project()
    p.add_paths(a.paths, progress=lambda i, n, nm: print(f'\rreading {i}/{n}', end='', file=sys.stderr))
    print(file=sys.stderr)
    p.check_short_clips()
    if a.fps:
        p.set_fps(a.fps)
    fi = p.fps_info
    print(f"Frame rate {fi['label']} ({fi['status']}): {fi['reason']}", file=sys.stderr)
    if fi['status'] == 'ask' and not a.scan_only:
        print(f"Not guessing. Run again with --fps {fi['suggest']!r} or --fps 23.976.", file=sys.stderr)
        return 2
    if a.scan_only:
        # read-only summary: nothing is written anywhere
        for e in fi.get('evidence', []):
            print(f"  {'votes' if e['votes'] else 'ignored'}  {e['kind']:7} {e['name']}: {e['label']}  {e.get('note') or ''}")
        print('Days:', ', '.join(f"{d['label']} ({d['n']} files)" for d in p.days()))
        print('Output would go to:', default_out_dir(p, default_job_name(p)))
        lanes = p.lanes()
        for ln in lanes:
            if ln['kind'] == 'camera':
                print(f"\n{ln['label']}  {ln['sub']}")
            else:
                tracks = ', '.join(f"{t['name']}={t['role']}" for t in ln.get('tracks') or [])
                print(f"\n{ln['label']}  {ln['sub']}  {tracks}")
            for c in sorted((c for c in p.clips if c.get('lane') == ln['key']), key=lambda c: (c['start'] is None, c['start'] or 0)):
                tcs = TC.samples_to_tc(c['start'], p.fps) if c['start'] is not None else '--:--:--:--'
                extra = [c.get('camera') or '', f"SN {c['serial']}" if c.get('serial') else '', c.get('tc_source') or '',
                         'slow motion: ' + c['slowmo']['kind'] if c.get('slowmo') else '', '' if c['include'] else 'LEFT OUT']
                if c['kind'] == 'audio':
                    extra.append('roles ' + '/'.join(Project.clip_roles(c)))
                print(f"  {c['roll']:24} {c['name']:44} {tcs} {TC.fmt_dur(c['length']):>8}  "
                      f"{' · '.join(x for x in extra if x)}  {','.join(c['flags'])}")
        for u in p.unsupported:
            print('unsupported:', u['path'], '-', u['why'])
        for e in p.errors:
            print('error:', e)
        return 0
    job = a.job or default_job_name(p)
    ref = dict(roll=a.ref_roll, channel=a.ref_channel - 1) if a.ref_roll else None
    s = dict(job_name=job, out_dir=a.out or default_out_dir(p, job), reference=ref,
             aap_mode='skip' if a.no_aap else 'resolve', waveform_check=not a.no_waveform,
             transcribe=a.transcribe)

    def emit(kind, data):
        if kind == 'log':
            print(data)
        elif kind == 'aap_wait':
            print('Waiting for Auto-Align Post renders in', data['render_dir'], '- press Enter to continue anyway')
            import threading
            threading.Thread(target=lambda: (input(), j.user_continue.set()), daemon=True).start()
        elif kind == 'error':
            print(data['trace'], file=sys.stderr)
    j = Job(p, s, emit)
    j.run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
