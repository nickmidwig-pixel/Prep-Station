"""A recorder whose takes use different track layouts, the way the 633 did on 2026-11-06 (4, 6 and 8 tracks, with
a MIX, MIX pair), on an "ISO day" (takes long enough that the delivery becomes day polywavs). Checks the day
polywavs carry one track per mic name across the day, every take's audio on its own named track, silence
elsewhere, and that the names reach iXML, the bext sTRK list and the XML. Audio-only mode, Auto-Align faked
(renders = the sources, unchanged), so what's checked is the layout, not the alignment."""
import glob
import os
import re
import shutil
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app'))
from midwigsync import bwf, tc as TC  # noqa: E402
from midwigsync.bwf import WavInfo, WavWriter  # noqa: E402
from midwigsync.scan import Project  # noqa: E402
from midwigsync.pipeline import Job  # noqa: E402

SR = 48000
FPS = TC.parse_rate('23.976')
DAY = '/home/claude/layoutday'
OUT = '/home/claude/layoutday_SYNCED'
rng = np.random.default_rng(5)

# (name, start TC, length s, track names); poly_style='day' asks for day polywavs as a long day would get
TAKES = [
    ('GeorgiaT01', '10:48:56:00', 4 * 60, ['BOOM ', 'LAV', 'BOOM -6db', 'LAV -6db']),
    ('ConversatT01', '10:55:00:00', 3 * 60, ['MIX', 'MIX', 'BOOM1', 'BOOM2', 'LAV1', 'LAV2']),
    ('FriendsT01', '11:00:00:00', 5 * 60, ['Boom', 'Lavs', 'BOOM', 'Lav1 Alex', 'Lav2 Ellie', 'Lav3 Emma']),
    ('GeorgiaT02', '11:07:00:00', 3 * 60, ['BOOM ', 'LAV', 'BOOM -6db', 'LAV -6db']),
]
# every (take, track) gets its own tone so the polywav can be checked track by track
TONES = {}


def make_day():
    d = os.path.join(DAY, 'SOUND', '633 SD', '11-6-UDP')
    have = os.path.isdir(d) and len(os.listdir(d)) == len(TAKES)
    if not have:
        shutil.rmtree(DAY, ignore_errors=True)
        os.makedirs(d)
    f0 = 200
    for name, tcs, length, names in TAKES:
        n = length * SR
        start = TC.tc_to_samples(tcs, FPS)
        freqs = []
        for k, nm in enumerate(names):
            f0 += 37
            TONES[(name, k)] = f0
            freqs.append(f0)
        if have:
            continue
        desc = ''.join(f'sTRK{i + 1}={nm}\r\n' for i, nm in enumerate(names)) + \
            f'dTC={tcs}\r\ndUBITS=$26110600\r\ndFRAMERATE=23.976ND\r\n'
        be = bwf.build_bext(desc, 'Sound Devices', '633', '2026-11-06', tcs[:8], start, f'A=PCM,F=48000,W=24,M={len(names)}\r\n')
        ix = bwf.build_ixml('UDP', '23.976', '24000/1001', False, start, SR, 24, names, scene=name[:-3], take=name[-2:])
        w = WavWriter(os.path.join(d, name + '.WAV'), n, len(names), SR, 24, bwf.FMT_PCM, be, ix)
        for s0 in range(0, n, 60 * SR):
            t = np.arange(s0, min(n, s0 + 60 * SR)) / SR
            w.write_float(np.stack([(0.2 * np.sin(2 * np.pi * f * t)).astype(np.float32) for f in freqs], 1))
        w.close()


def fake_aap(plan, render_dir):
    """Renders = the source pieces unchanged (layout test, not alignment)."""
    for k, it in enumerate(i for i in plan['items'] if i['kind'] == 'source'):
        ws = WavInfo(it['path'])
        a = TC.frames_to_samples(it['abs_in'], FPS) - ws.time_reference
        b = TC.frames_to_samples(it['abs_out'], FPS) - ws.time_reference
        out = ws.read(a, b - a, 0)
        stem = os.path.splitext(os.path.basename(it['path']))[0]
        w = WavWriter(os.path.join(render_dir, f'{stem}_si0ch0off{a}-AtAlgn-{k:04x}beef.wav'), len(out), 1, SR, 24)
        w.write_float(out)
        w.close()


def tone_of(x):
    """Dominant frequency of a signal, or None when silent."""
    if np.abs(x).max() < 1e-4:
        return None
    sp = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.argmax(sp) * SR / len(x))


def run(poly_layout):
    p = Project()
    p.add_paths([DAY])
    p.analyze()
    ev = []
    j = Job(p, dict(job_name='LAYOUT', out_dir=OUT, _fake_aap=fake_aap, transcribe=False, export_otio=False,
                    poly_style='day', poly_layout=poly_layout, export_aaf=False, export_ale=False),
            lambda k, d: ev.append((k, d)))
    shutil.rmtree(OUT, ignore_errors=True)
    j.run()
    polys = sorted(glob.glob(os.path.join(OUT, 'POLY_ALIGNED', '*.WAV')))
    assert polys, 'no polywavs'
    return polys, ev


def check_audio(polys):
    """Every take's audio sits on a track named for its mic; every other track is silent there."""
    checks = 0
    covered = {}
    for name, tcs, length, tnames in TAKES:
        start = TC.tc_to_samples(tcs, FPS)
        mid = start + (length // 2) * SR
        hits = [q for q in polys if WavInfo(q).time_reference <= mid < WavInfo(q).time_reference + WavInfo(q).nframes]
        assert hits, (name, 'no polywav covers it')
        for poly in hits:
            wq = WavInfo(poly)
            names = [wq.track_names().get(i + 1) for i in range(wq.channels)]
            x = wq.read(mid - wq.time_reference, 2 * SR)
            used = {}
            labs = []
            for nm in tnames:
                nm = nm.strip()
                used[nm] = used.get(nm, 0) + 1
                labs.append(nm if used[nm] == 1 else f'{nm} ({used[nm]})')
            # this file carries the take only if it has the take's whole layout (merged: always; per-layout: its set)
            mine = all(l in names for l in labs)
            expect_on = {names.index(l): TONES[(name, k)] for k, l in enumerate(labs)} if mine else {}
            for ch in range(wq.channels):
                f = tone_of(x[:, ch])
                if ch in expect_on:
                    assert f is not None and abs(f - expect_on[ch]) < 2, (poly, name, names[ch], f, expect_on[ch])
                else:
                    assert f is None, (poly, name, names[ch], 'should be silent, has', f)
                checks += 1
            got_all = len(expect_on) == len(tnames)
            covered[name] = covered.get(name, False) or got_all
    for name, *_ in TAKES:       # some file holds every track of every take
        assert covered.get(name), (name, 'no polywav holds all its tracks')
    return checks


def main():
    make_day()
    # default: one polywav set per layout (A, B, C), each with exactly that layout's names
    polys, ev = run('per_layout')
    sets = {}
    for q in polys:
        w = WavInfo(q)
        m = re.search(r'_POLY_ALN_([A-Z])_', os.path.basename(q))
        sets.setdefault(m.group(1), set()).add(tuple(w.track_names().get(i + 1) for i in range(w.channels)))
    print('per-layout sets:', {k: sorted(v)[0] for k, v in sorted(sets.items())})
    assert sorted(sets) == ['A', 'B', 'C'], sets
    assert sets['A'] == {('BOOM', 'LAV', 'BOOM -6db', 'LAV -6db')}, sets['A']
    assert sets['B'] == {('MIX', 'MIX (2)', 'BOOM1', 'BOOM2', 'LAV1', 'LAV2')}, sets['B']
    assert sets['C'] == {('Boom', 'Lavs', 'BOOM', 'Lav1 Alex', 'Lav2 Ellie', 'Lav3 Emma')}, sets['C']
    n = check_audio(polys)
    print(f'  {n} track checks: right tone on the right named track, silence elsewhere')
    xml = open(glob.glob(os.path.join(OUT, 'TIMELINES', 'PREMIERE RESOLVE XML', '*_SYNC_ALIGN.xml'))[0]).read()
    for nm in ['BOOM', 'LAV -6db', 'MIX (2)', 'BOOM1', 'Lav3 Emma']:
        assert f'<name>{nm}</name>' in xml, nm
    print('  XML carries the track names')
    assert any('one polywav set per layout' in str(d) for k, d in ev if k == 'log')

    # merged: every mic name of the day in one wide layout
    polys, ev = run('merged')
    w = WavInfo(polys[0])
    names = [w.track_names().get(i + 1) for i in range(w.channels)]
    print('merged layout:', names)
    want = ['BOOM', 'LAV', 'BOOM -6db', 'LAV -6db', 'MIX', 'MIX (2)', 'BOOM1', 'BOOM2', 'LAV1', 'LAV2',
            'Boom', 'Lavs', 'Lav1 Alex', 'Lav2 Ellie', 'Lav3 Emma']
    assert names == want, (names, want)
    df = w.desc_fields()
    assert df.get('dTC') and df.get('dFRAMERATE'), df
    assert w.time_reference == TC.tc_to_samples(TAKES[0][1], FPS)
    n = check_audio(polys)
    print(f'  {n} track checks: right tone on the right named track, silence elsewhere')
    print('LAYOUTS GOOD')


if __name__ == '__main__':
    main()
