"""Frame-rate assessment: 23.976 by default, Zaxcom metadata never votes, slow motion never votes, reports parsed,
other rates asked about, override honoured, LTC-only files follow a 24 project."""
import os, sys, tempfile, zlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync.scan import Project
from midwigsync import framerate as FR, tc as TC, ltc as LTC

def clip(kind, roll, fps, df=False, originator=None, length=48000 * 600, **kw):
    return dict(kind=kind, roll=roll, fps=TC.parse_rate(fps) if fps else None, df=df, originator=originator,
                length=length, include=True, path=f'/x/{roll}/f', flags=[], start=0, orig_start=0, **kw)

def proj(clips, reports=(), override=None):
    p = Project(); p.clips = clips; p.reports = list(reports); p.fps_override = override
    return FR.assess(p, p.reports, override)

tmp = tempfile.mkdtemp()
def write(name, data):
    path = os.path.join(tmp, name); open(path, 'wb').write(data); return path
sd_csv = write('SOUND_REPORT.csv', b'"Sound Report"\n"Project","KY UNITE"\n"Frame Rate","25ND"\n\n"File Name","Scene","Take","Start TC"\nT001.WAV,1,1,10:00:00:00\n')
col_csv = write('report_day2.csv', b'File,Scene,Take,TC Rate,Start\nT001.WAV,1,1,29.97DF,10:00:00;00\nT002.WAV,1,2,29.97DF,10:05:00;00\n')
txt = write('sound log.txt', b'Mixer: J. Doe\nTimecode Rate: 23.98 NDF\nSample rate 48k\n')
stream = zlib.compress(b'BT /F1 12 Tf 72 700 Td (Sound Report) Tj 0 -14 Td (Frame Rate: 25 NDF) Tj ET')
pdf = write('SoundReport.pdf', b'%PDF-1.4\n1 0 obj << /Length ' + str(len(stream)).encode() + b' /Filter /FlateDecode >>\nstream\n' + stream + b'\nendstream\nendobj\n%%EOF\n')
rep = {os.path.basename(p): FR.read_report(p) for p in (sd_csv, col_csv, txt, pdf)}
for k, v in rep.items():
    print(f'  report {k:22s} -> {dict(v["rates"]) if v else None}')
assert rep['SOUND_REPORT.csv']['rates'].most_common(1)[0][0] == '25'
assert rep['report_day2.csv']['rates'].most_common(1)[0][0] == '29.97 DF'
assert rep['sound log.txt']['rates'].most_common(1)[0][0] == '23.976'
assert rep['SoundReport.pdf']['rates'].most_common(1)[0][0] == '25'
assert sorted(os.path.basename(x) for x in FR.find_reports([tmp])) == sorted(rep)

cases = [
    ('nothing at all', [], [], None, ('23.976', 'ok', None)),
    ('normal day', [clip('video', 'FX6', '23.976'), clip('video', 'C500', '23.976'), clip('audio', 'MIX', '23.976')], [], None, ('23.976', 'ok', None)),
    ('Zaxcom says 30, cameras 23.976', [clip('video', 'FX6', '23.976'), clip('audio', '02LAV', '30', originator='ZAXCOM NOVA')], [], None, ('23.976', 'ok', None)),
    ('Zaxcom alone says 30', [clip('audio', '02LAV', '30', originator='Zaxcom Nomad')], [], None, ('23.976', 'ok', None)),
    ('slow motion B-cam 59.94', [clip('video', 'FX6', '23.976'), clip('video', 'FX3', '59.94', length=48000 * 3000)], [], None, ('23.976', 'ok', None)),
    ('European 25p day + report', [clip('video', 'FX6', '25'), clip('video', 'C70', '25')], [rep['SOUND_REPORT.csv']], None, ('23.976', 'ask', '25')),
    ('only the sound report says 25', [], [rep['SoundReport.pdf']], None, ('23.976', 'ask', '25')),
    ('29.97 DF report + cameras', [clip('video', 'FX6', '29.97', df=True)], [rep['report_day2.csv']], None, ('23.976', 'ask', '29.97 DF')),
    ('true 24 cinema cameras', [clip('video', 'VENICE', '24')], [], None, ('23.976', 'ask', '24')),
    ('you set 25', [clip('video', 'FX6', '25')], [], '25', ('25', 'set', '25')),
    ('you set 23.976 on a 25 day', [clip('video', 'FX6', '25')], [], '23.976', ('23.976', 'set', '25')),
]
ok = True
for name, clips, reps, ov, want in cases:
    r = proj(clips, reps, ov)
    got = (r['label'], r['status'], r['suggest'])
    good = got == want
    ok &= good
    print(f"  {'ok ' if good else 'BAD'} {name:34s} -> {r['label']:8s} {r['status']:4s} suggest={r['suggest']}  | {r['reason']}")

# LTC-only audio file: decoded at 23.976 by default, follows a 24 project exactly
sr = 48000
f24 = TC.parse_rate('24')
sig = LTC.encode(TC.tc_to_frames("10:00:00:00", f24), 24 * 12, f24, sr, lead_s=0.5)
r = LTC.start_from_ltc(sig, sr, TC.parse_rate('23.976'))
c = dict(kind='audio', roll='TENT', fps=r['fps'], df=False, include=True, path='/x/a', flags=[], length=sr * 12,
         start=r['start'], orig_start=r['start'], tc_source='LTC on ch 1',
         ltc=dict(fps=TC.rate_label(r['fps']), anchor=r['anchor'], rate_from='guess'))
p = Project(); p.clips = [c, clip('video', 'VENICE', '24')]; p.fps_override = '24'; p._ltc_follow_rate = Project._ltc_follow_rate.__get__(p)
p.fps = TC.parse_rate('24'); p._ltc_follow_rate()
want = TC.tc_to_samples("10:00:00:00", TC.parse_rate("24")) - 24000
print(f"  LTC file at 24: start error {c['start'] - want} samples (decoded at 23.976 first: {r['start'] - want})")
ok &= abs(c['start'] - want) <= 2
print('FRAMERATE GOOD' if ok else 'FRAMERATE FAILED')
