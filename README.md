# Midwig Sync

One-stop sync for a shoot day: drop camera cards + sound folders, get synced, Auto-Align-Post-aligned
ISOs, ~4 GB polywavs and an FCP7 XML (Premiere + Resolve).

## Pipeline (midwigsync/pipeline.py)
1. scan.py: probe every file (media.py: PyAV for video TC incl. Sony M01.XML sidecars; bwf.py for BWF).
   Formats: MXF/MOV/MP4/MTS/AVI/MKV/INSV; Canon CRM + Blackmagic BRAW (picture unreadable, audio + tmcd read
   from the QuickTime-style container); P2/OP-Atom (audio from CONTENTS/AUDIO/<clip>NN.MXF); R3D/NEV/ZRAW
   tried, else audio + TC from a same-name proxy (find_proxy), else listed in unsupported with a reason.
   Audio: WAV/BWF/RF64 (16/24/32-bit, float); non-48k BWF resampled to 48k with bext/iXML/TC kept and
   AIFF/M4A/MP3/FLAC/CAF converted (convert.py, cache in ~/Library/Caches/Midwig Sync/converted; the job
   gets a CONVERTED_48K/ copy). ltc.py: files with no/zero TC are checked for LTC on any audio channel
   (Tentacle into a camera mic input); TC comes from the LTC and that channel is never used for matching.
   GoPro chapters (GX01xxxx, GX02xxxx...) without their own TC follow the previous chapter.
   Rolls = camera card folder / recorder track folder (02LAV...). Flags: no_tc, zero_tc, tc_far,
   tc_overlap, short_silent (< 20 s and quiet -> left out), sample_rate, fps_mismatch.
2. wavesync.py: 200 Hz log-energy envelopes, per-roll day "beds", pairwise roll offsets, majority cluster
   = trusted clock; clips off by > 0.2 s get corrected (camera clock for cameras, own roll clock for ISOs);
   no-TC files found by full-day search; sample-accurate refine vs the best-matching roll.
   Reference roll channel is written to REF_<roll>/*_REF.WAV while listening.
3. stamp.py: TC_FIXED/ copies (WAV: new bext/iXML, head-padded to a frame; video: ffmpeg re-wrap to .mov).
4. resolve_aap.py: plan_timeline (REF on A1, ISO pieces under each ref clip on A2..), Resolve scripting
   build, Accessibility: Workspace > Workflow Integrations > Auto-Align Post + press Render (dumps the
   panel's AX tree to _MIDWIG/aap_panel_tree.txt), RenderWatcher.
5. assemble.py: full-length aligned ISOs from AAP renders (<src>_si0ch0off<N>-AtAlgn-*.wav, sample 0 =
   source start + N). Gaps: offset/polarity from render edges (ramped between two edges), else borrowed
   from other ISOs within 10 min.
6. polywav.py: largest whole TC minute under 4 GiB (or whole hours), track-number order, bext+iXML.
7. xmlexport.py: xmeml v5, video track per camera roll, camera ch1 scratch, then ISO (or poly) tracks.
8. report.py: verification vs reference + <JOB>_REPORT.txt.

## By hand (manual.py, the clip panel)
- Type a TC (HH:MM:SS:FF, HHMMSSFF, HH:MM:SS), nudge ±1 frame / ±1 s, back to the file's TC.
- Find by waveform: whole-day search of one clip against all other rolls or one roll, refined to the sample,
  with confidence (strong/good/weak) and the result near its own TC. "Use" locks the position.
- Locked positions (c['manual']) survive re-runs, count as trusted for placing other clips, get TC_FIXED
  copies, and land in the XML. "Write a copy now" stamps one clip immediately.

## Dev
- tests/make_day.py builds a simulated day; tests/run_pipeline.py runs everything with a stand-in for AAP.
- tests/make_extras.py adds GoPro chapters with LTC, fake BRAW (one missed jam), P2 OP-Atom, a 96 kHz float
  BWF and a 44.1 kHz M4A; tests/run_extras.py runs the full pipeline on that day plus one typed-in TC.
- The window logs to ~/Library/Logs/Midwig Sync/launch.log; setup shows a dialog, crashes show an alert.
- packaging/build_app.sh builds dist/Midwig Sync.app (shell launcher sets up a private Python with uv
  on first launch in ~/Library/Application Support/Midwig Sync; logs in ~/Library/Logs/Midwig Sync).
- Headless: cd "Midwig Sync.app/Contents/Resources/app" && <venv>/bin/python -m midwigsync.cli <folders> --scan-only
