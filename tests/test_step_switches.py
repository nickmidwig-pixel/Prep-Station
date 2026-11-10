"""2.5.1: the What-happens steps switch independently. Polywavs off -> no POLY_ALIGNED, the editorial XML carries
the aligned ISOs; ISOs off with polywavs on -> no ISO_ALIGNED, XML on the polywavs."""
import os, shutil, sys, importlib
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'app')); sys.path.insert(0, HERE)
from midwigsync.scan import Project
from midwigsync.pipeline import Job, default_out_dir
from midwigsync import safety
rp = importlib.import_module('run_pipeline')

DAY = '/home/claude/testday'
ok = True
for name, extra, want_poly, want_iso, xml_tag in (
        ('SW_nopoly', dict(make_polys=False), False, True, 'SYNC_ISO'),
        ('SW_noiso', dict(iso_folder=False), True, False, 'SYNC_ALN')):
    p = Project(); p.add_paths([DAY]); p.check_short_clips(20)
    out = default_out_dir(p, name)
    if os.path.exists(out):
        safety.allow_output(out); shutil.rmtree(out)
    errs = []
    j = Job(p, dict(job_name=name, out_dir=out, _fake_aap=rp.fake_aap, waveform_check=False, transcribe=False, **extra),
            lambda k, d: errs.append(d) if k == 'error' else None)
    j.run()
    has_poly = os.path.isdir(os.path.join(out, 'POLY_ALIGNED')) and bool(os.listdir(os.path.join(out, 'POLY_ALIGNED')))
    has_iso = os.path.isdir(os.path.join(out, 'ISO_ALIGNED'))
    xmls = sorted(os.listdir(os.path.join(out, 'TIMELINES')))
    good = not errs and has_poly == want_poly and has_iso == want_iso and any(xml_tag in x for x in xmls)
    if xml_tag == 'SYNC_ISO':
        x = open(os.path.join(out, 'TIMELINES', next(f for f in xmls if 'SYNC_ISO' in f))).read()
        good &= 'ISO_ALIGNED' in x
    print(f"{name}: polywavs {has_poly}, ISOs {has_iso}, timelines {xmls} -> {'ok' if good else 'BAD'}")
    ok &= good
print('SWITCHES GOOD' if ok else 'SWITCHES FAILED')
sys.exit(0 if ok else 1)
