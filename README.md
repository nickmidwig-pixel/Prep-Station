# Prep Station

One-stop prep for a shoot day: drop camera cards and sound folders, get timecode checked and fixed,
Sound Radix Auto-Align Post run in DaVinci Resolve, aligned ISOs, ~4 GB polywavs and editorial timelines
(XML for Premiere and Resolve, OTIO, AAF for Pro Tools, ALE for Avid, a clip list).

The Python package is still called `midwigsync`; the app name, bundle id, version and folder names all
come from `app/midwigsync/brand.py`.

## Pipeline (midwigsync/pipeline.py)
1. **scan.py**: probe every file (media.py: PyAV for video TC incl. Sony M01.XML sidecars; bwf.py for BWF).
   - Video: MXF/MOV/MP4/MTS/AVI/MKV/INSV/3GP. Canon CRM and Blackmagic BRAW: picture can't be decoded here,
     audio and tmcd are read from the QuickTime-style container. P2/OP-Atom: audio from
     CONTENTS/AUDIO/<clip>NN.MXF. R3D/NEV/ZRAW: tried, else audio and TC from a same-name proxy, else listed
     with a reason.
   - Audio: WAV/BWF/RF64 (16/24/32-bit, float). Non-48k BWF is resampled with bext/iXML/TC kept;
     AIFF/M4A/MP3/FLAC/CAF are converted (convert.py, cache in ~/Library/Caches/Prep Station/converted).
   - ltc.py: files with no or zero TC are checked for LTC on every channel; that channel is never used
     for matching. GoPro chapters follow the previous chapter.
   - Shoot days: trusted dates only (user bits, bext date, camera metadata, Sony XML; never file mtime).
     Recordings before 05:00 count as the night before; TC past midnight is unwrapped within a day.
     Duplicates (same recording on two cards) are left out.
   - Track roles per channel (refplan.role_of): boom, plant, lav, mix (incl. bare L/R), tc, empty.
2. **wavesync.py**: 200 Hz log-energy envelopes, per-roll day beds, pairwise roll offsets, majority clock;
   clips off by > 0.2 s are corrected, no-TC files found by a whole-day search, refined to the sample.
3. **stamp.py**: TC_FIXED/ copies (WAV: new bext/iXML; video: ffmpeg re-wrap to .mov). Originals untouched.
4. **refplan.py + resolve_aap.py**: what Auto-Align aligns to.
   - Camera mode: a camera mic (FX6 by default) is the reference.
   - Audio-only mode (auto when there's no camera sound): per stretch the boom, else plant, else the lav
     that matches the others best, with hysteresis. Multichannel recorder files are split into bit-exact
     monos for AAP and repacked afterwards; mix, TC and empty channels pass through untouched.
   - Resolve scripting builds the timeline; Accessibility opens the Auto-Align Post panel and presses Render.
5. **assemble.py**: full-length aligned ISOs from AAP renders; gaps get offset/polarity from render edges or
   borrow from other ISOs. Reference windows pass through unchanged.
6. **polywav.py**: largest whole TC minute under 4 GiB (or whole hours), track-number order, bext + iXML.
   Per-take recorder deliveries skip day-long polywavs unless "Day-long polywavs" is on.
7. **xmlexport.py + exports.py** (EDITORIAL/): FCP7 xmeml, OTIO, AAF (sound only, linked WAVs), ALE, CSV;
   optional synced camera clips (picture copied, aligned mono tracks, corrected TC).
8. **report.py**: verification vs the reference, <JOB>_REPORT.txt and READ ME.txt.

Several shoot days dropped together run as one job each, each with its own folder
(`gui.Api._day_names`). Output goes next to the shoot folder (a folder called just SOUND or AUDIO is
skipped over), named `<YYYYMMDD>_<shoot>_SYNCED`, and is ignored if dropped again.

## By hand (manual.py, the clip panel)
- Type a TC (HH:MM:SS:FF, HHMMSSFF, HH:MM:SS), nudge ±1 frame / ±1 s (← → and ⇧← ⇧→), back to the file's TC.
- Find by waveform: whole-day search against everything or one roll, with confidence and the match near
  its own TC. "Use" locks the position; locked positions survive re-runs and count as trusted.

## Window (ui/)
- Colour: each camera its own blue/teal/violet; sound by mic (boom gold, lav green, multitrack copper,
  mix grey). Overlays: orange outline = TC fixed, white outline = set by you, red stripes = needs you,
  dimmed = left out.
- Day chips when more than one shoot day is loaded; "Sync to" Auto / Camera mic / Audio only;
  one list of everything that needs attention; preflight checks (disk space, Resolve, Auto-Align, iLok,
  Accessibility, missing media).
- Keys: ⌘O add, ⌘↩ sync, ↑↓ clips, ←→ nudge, Space/Delete include, Esc close.

## Dev
- tests/make_day.py + run_pipeline.py: simulated camera day with a stand-in for AAP.
- tests/make_extras.py + run_extras.py: GoPro chapters with LTC, BRAW with a missed jam, P2, 96 kHz float,
  M4A memo, one typed-in TC, synced clips.
- tests/make_mixer_day.py + run_mixer_day.py: audio-only, 6-track per-take recorder (prints ALL GOOD).
- tests/run_two_days.py: two shoot days in one run through the window's API (prints TWO DAYS GOOD).
- packaging/build_app.sh builds "dist/Prep Station.app" and the zip. The launcher sets up a private
  Python with uv on first launch (~/Library/Application Support/Prep Station; logs in
  ~/Library/Logs/Prep Station) and migrates an old "Midwig Sync" setup.
- Headless: cd "Prep Station.app/Contents/Resources/app" && <venv>/bin/python -m midwigsync.cli <folders> --scan-only
