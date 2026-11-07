# Prep Station

One-stop prep for a shoot day: drop the day's folder (camera cards + sound), and it checks and fixes timecode,
runs Sound Radix Auto-Align Post in DaVinci Resolve, and hands back aligned polywavs (always under 4 GB),
optional aligned ISOs, editorial timelines (XML for Premiere and Resolve, OTIO, AAF for Pro Tools, ALE for Avid,
a clip list), an optional transcript of the day, and a report.

The Python package is still called `midwigsync`; the app name, bundle id, version and folder names come from
`app/midwigsync/brand.py`.

## Originals are read-only (safety.py)
Every dropped folder and every file read as media is registered; a Python audit hook refuses any write, rename,
move, delete, truncate, date/permission/xattr change or hard link that would touch one (by path or by the file
itself), and any ffmpeg output onto one. Writing is allowed only inside the job's output folder and the app's own
cache. The output folder may not sit inside a camera card or a folder holding original media. There is no switch.
`tests/check_originals.py` fingerprints every original (hash, size, dates, inode, links, mode), runs every test,
and checks again; `tests/test_guard.py` tries 19 ways to damage an original.

## Order of a run (pipeline.py)
1. **Read the day** (scan.py): probe everything; shoot days from trusted dates only; track roles per channel
   (boom/plant/lav/mix/tc); frame rate assessed (framerate.py: 23.976 unless cameras, a sound report or non-Zaxcom
   sound metadata clearly say otherwise, and then the window asks); slow motion and other frame rates found
   (`_mark_slowmo`: time-base change, Sony S&Q capture fps, sensor fps metadata, or a silent clip whose TC runs into
   the next) and kept off the sync timeline.
2. **Alignment track** (refpick.py): every camera channel compared with the recorder's mics on a few clips;
   silent, LTC and mixer-feed channels (waveform rebuilt from the recorder's tracks, feed_score ≥ 0.7) are never
   used; the onboard mic that matches best wins (near-ties go to the lower channel).
3. **Timecode check** (wavesync.py): every roll against every other; missed jams over 0.2 s corrected, no-TC files
   placed by a whole-day search.
4. **Review stop** (`review_fixes`): the window shows every fix and waits: write / keep the files' own TC / stop.
5. **Corrected sound** (`do_sound_set`): SOUND_TC_CORRECTED/ holds every sound file of the day in its original
   format; fixed files get the new TC, the rest are exact copies (APFS clones on the same drive).
6. **Auto-Align Post** (refplan.py + resolve_aap.py): camera mode aligns to the picked onboard mic; audio-only
   picks boom > plant > best lav per stretch. Mix / TC / empty tracks pass through untouched, never aligned to.
7. **Aligned ISOs** (optional, ISO_ALIGNED/): each mic on its own, no mix.
8. **Aligned polywavs** (POLY_ALIGNED/): per-take mixer polywavs come back aligned with the same names and track
   layout; ISO days become day polywavs of the largest whole TC minutes under 4 GB. Mix tracks included.
9. **Editorial** (EDITORIAL/): XML with cameras on A CAM / B CAM... tracks (one lane per camera body, Project.lanes)
   and the aligned polywavs; `<job>_SLOW_MOTION.xml` for slow motion and other frame rates (shot order, markers);
   OTIO, AAF (from the aligned ISOs), ALE, CSV.
10. **Transcript** (optional, transcribe.py, TRANSCRIPTS/): local Whisper large-v3-turbo (MLX on Apple Silicon,
    faster-whisper on Intel; installed on first use), listening to the mix/boom/lavs per stretch, speakers from the
    lav track names, TC at each turn and every minute: Word, text and CSV.
11. **Report** + READ ME.
After the run: **camera copies** on request (CAMERA_TC_CORRECTED/): the original bytes with only the timecode
number changed (QuickTime/MP4/CRM/BRAW tmcd, MXF timecode components), else a stream copy into .mov.

Job name = the shoot date (today if the files don't say). Output = `<date>_SYNCED` at the top of the dropped
shoot folder (never inside a card or roll folder).

## Window (ui/)
- Lanes like Tentacle Sync Studio: A CAM, B CAM... (all cards of one camera body on one lane; letters from the
  camera's own naming, else by how much each shot), then each sound folder; multitrack lanes open into tracks.
- Colour: cameras by letter; sound by mic (boom gold, lav green, multitrack copper, mix grey). Overlays: orange =
  TC fixed, white = set by you, red stripes = needs you, grey hatching = slow motion (own timeline), dim = left out.
- Playhead: click the ruler or the timeline; Space plays the sound there (selected clip, else the recorder's mix /
  boom, else the camera) through afplay; a still of the picture shows beside it.
- Keys: Space play/stop, + / − zoom, ⌘O add, ⌘↩ sync, ↑↓ clips, Delete include, Esc.
- Frame rate block with the evidence; "Sync to" Auto / Camera mic / Audio only; preflight checks; one list of
  what needs attention; clip list grouped by lane.

## Dev
- Test days: tests/make_day.py (camera day), make_extras.py (mixed formats), make_mixer_day.py (audio only),
  test_slowmo.py builds /home/claude/slowday, test_refpick.py builds /home/claude/hopday.
- Suites: run_pipeline, run_extras, run_mixer_day, run_two_days, test_guard, test_framerate, test_slowmo,
  test_refpick, test_camera_copy, test_transcript (PREP_FAKE_ASR stand-in), test_review; check_originals runs them
  all around the fingerprint check.
- packaging/build_app.sh builds "dist/Prep Station.app" and the zip; the launcher sets up a private Python with uv
  (requirements.txt; requirements-extra.txt is optional: OTIO, AAF, Word).
