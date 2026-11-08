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
3. **Timecode check** (wavesync.py): every roll against every other (never against the other cards of its own
   camera body); missed jams over 0.2 s corrected, no-TC files placed by a whole-day search. **Fix the fewest
   sources** (`consensus`): a source is one clock (camera body or sound folder); the biggest group of sources
   whose clocks agree is the truth and is left alone (ties: the group with the reference camera), everything
   outside it is corrected onto it. So one GoPro moves, not three cameras; a recorder with a dead TC box moves,
   not the two cameras that agree; a reference camera that is alone in being off moves too. The one-line reason
   (`decision`) is shown in the review stop, the log and the report.
4. **Review stop** (`review_fixes`): the window shows every fix and waits: write / keep the files' own TC / stop.
5. **Corrected sound** (`do_sound_set`): SOUND_TC_CORRECTED/ holds every sound file of the day in its original
   format; fixed files get the new TC, the rest are exact copies (APFS clones on the same drive).
6. **Auto-Align Post** (refplan.py + resolve_aap.py + aap_panel.py): camera mode aligns to the picked onboard mic
   on every card of that camera body; audio-only picks boom > plant > best lav per stretch. Mix / TC / empty tracks
   pass through untouched, never aligned to. The app builds the Resolve timeline (REF on A1), sets up the Auto-Align
   Post panel (its own Electron app: Reference = REF, every source ticked) and presses Render; renders are picked
   up from our folder or wherever Resolve's "Aligned files for ..." bin says, named with or without an offset. The
   transcript runs alongside the render. The panel is closed once every render is in (its backend crashes on
   exit, so the panel would otherwise spin at "Rendering…" forever; the hidden "reopen windows?" dialog that
   crash leaves for next time is answered by `dismiss_backend_dialog`).
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
- Lanes like Tentacle Sync Studio: A CAM, B CAM... All cards of one camera body sit on one lane: grouped by the body
  serial (Canon uuid box, Sony XML), else the letter, else the folder. Letters come from the card folder
  (UDP_261106_C80_B_001 -> B), else the camera's clip names (A001C003, C_0001C002), else by how much each shot.
  Click a camera's name on the timeline to change its letter; ⌘G puts the selected cards on one camera.
  Then each sound folder; multitrack lanes open into tracks.
- Colour: cameras by letter; sound by mic (boom gold, lav green, multitrack copper, mix grey). Overlays: orange =
  TC fixed, white = set by you, red stripes = needs you, grey hatching = slow motion (own timeline), dim = left out.
- Playhead: click the ruler or the timeline; Space plays the sound there (selected clip, else the recorder's mix /
  boom, else the camera) through afplay; a still of the picture shows beside it.
- Undo / redo (⌘Z / ⇧⌘Z, and the ↶ ↷ buttons) for every edit: use / leave out, timecode typed or picked, slow-motion
  marks, camera letters and grouping, frame rate, reference mic, sync-to, job name, files added, removed or cleared.
  Each step stores only what that edit changed, so undoing never reverses what a sync run did since.
- Keys (⌘/ shows them all): ⌘A select all, ⌘1 cameras, ⌘2 sound, ⌘-click / ⇧-click to pick several, ⌫ leave out or
  use, ⌘⌫ remove from the list, ⌘G one camera, ⌘C copy timecode, Space / L / K / J play stop back, ← → a frame,
  ⇧← ⇧→ a second, ⌥← ⌥→ clip edges, Home / End, ↑ ↓ clips, ⌘I info, T type a timecode, ⌘F find by waveform,
  + − zoom, ⇧Z or \ fit, ⌘O add, ⌘↩ sync, ⌘, settings, Esc.
- Zoom slider with a percentage under the timeline (100% = the whole day); big timecode over the picture.
- Settings sheet (⌘,): synchronisation, sound out, export, transcript. The panel keeps Sync to / Frame rate / Output /
  a one-line-per-step summary.
- Clip rows carry the facts Tentacle shows: card, camera, codec, size, rate, audio, where the timecode came from.
- Frame rate block with the evidence; "Sync to" Auto / Camera mic / Audio only; preflight checks; one list of
  what needs attention; clip list grouped by lane.

## Camera formats worth knowing
- Canon Cinema RAW Light (.CRM, card layout <card>/CRM/REEL_xxx/): ffmpeg can't open the picture, so it never sees
  the timecode. media.qt_info reads the QuickTime boxes directly: tmcd (timescale/frameDuration/frames + the
  4-byte frame number at the first chunk), the video track's stts for rate and frame count, and Canon's uuid box
  (85c0b687-820f-11e0-8111-f4ce462b6a48, EXIF-style CMT1-CMT4) for model and body serial. Checked against the real
  C500 Mark II and C80 files of 2026-11-06.

## Dev
- Test days: tests/make_day.py (camera day), make_extras.py (mixed formats), make_mixer_day.py (audio only),
  test_slowmo.py builds /home/claude/slowday, test_refpick.py builds /home/claude/hopday.
- Suites: run_pipeline, run_extras, run_mixer_day, run_two_days, test_guard, test_framerate, test_slowmo,
  test_refpick, test_camera_copy, test_transcript (PREP_FAKE_ASR stand-in), test_review, test_canon_raw (builds
  /home/claude/canonday + canonday_plain), test_window_keys (the window driven by the keyboard against the real
  Python side, via Playwright); check_originals runs them all around the fingerprint check.
- `python -m midwigsync.cli <folders> --scan-only` prints lanes, serials, roles and frame-rate evidence, read-only.
- packaging/build_app.sh builds "dist/Prep Station.app" and the zip; the launcher sets up a private Python with uv
  (requirements.txt; requirements-extra.txt is optional: OTIO, AAF, Word).
