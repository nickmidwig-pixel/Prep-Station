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
    ap.add_argument('--unaligned-poly', action='store_true')
    ap.add_argument('--scan-only', action='store_true')
    a = ap.parse_args(argv)
    p = Project()
    p.add_paths(a.paths, progress=lambda i, n, nm: print(f'\rreading {i}/{n}', end='', file=sys.stderr))
    print(file=sys.stderr)
    p.check_short_clips()
    if a.scan_only:
        for c in sorted(p.clips, key=lambda c: (c['roll'], c['name'])):
            tcs = TC.samples_to_tc(c['start'], p.fps) if c['start'] is not None else '--'
            print(f"{c['roll']:14} {c['name']:40} {tcs}  {TC.fmt_dur(c['length']):>8}  {','.join(c['flags'])}")
        return 0
    job = a.job or default_job_name(p)
    ref = dict(roll=a.ref_roll, channel=a.ref_channel - 1) if a.ref_roll else None
    s = dict(job_name=job, out_dir=a.out or default_out_dir(p, job), reference=ref,
             aap_mode='skip' if a.no_aap else 'resolve', waveform_check=not a.no_waveform,
             poly_unaligned=a.unaligned_poly)

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
