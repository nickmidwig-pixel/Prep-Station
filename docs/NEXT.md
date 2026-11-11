# Prep Station: next pass (handoff, written 2026-11-06 late evening)

Big-picture pass by Fable after looking at Tentacle Sync Studio 1.37 with Nick's real day loaded, and at the real
day itself: "Untitled Dance Project" 11/06/26 on `/Volumes/FS LOANER_0405/Untitled Dance Project/261106`
(C500 Mk II + C80, both Canon RAW Light .CRM, ~3.3 TB, 31 clips; Sound Devices 633, 7 polywav takes, 4–8 tracks;
one sound report CSV). Everything below was checked against those files unless marked "verify".

## A. What the real files taught us (do these first; each is a bug or gap today)

1. **CRM timecode is invisible to ffmpeg/PyAV.** Because the picture codec is unknown, neither exposes the
   `tmcd` track, so `media.probe_video` gets no start TC for .CRM (likely .braw too on real files). The TC is a
   normal QuickTime `tmcd` track: `stsd` holds timescale 24000 / frameDuration 1001 / numberOfFrames 24; the first
   sample (4-byte big-endian frame count, via `co64`/`stco`) is the start. 945533 frames = 10:56:37:05, matching
   Tentacle exactly on all 31 clips. `stamp._qt_tmcd_offset` already walks these atoms: lift that walker into
   `media.py` as `qt_timecode(path) -> (frames, timescale, frameDuration, nframes, df)` and use it whenever the
   container is QuickTime-style and PyAV reports no timecode (CRM, BRAW, any MOV). Video frame count comes from the
   video track's `stts` (one entry: count, 1001).
2. **Camera body identity for lanes:** the Canon `uuid` atom in `moov` carries `Canon EOS C500 M`, `Canon EOS C80`,
   the body serial (`582022100067`, `012919000260`), firmware, local creation time with UTC offset, clip name.
   Parse model + serial there (and the equivalent for Sony XML `Device serialNo`, which `sony_sidecar` already reads)
   and set `c['serial']` so `Project.lanes()` groups cards by body, not by folder name. Today's six C500 cards must
   land on ONE "A CAM" lane; six C80 cards on ONE "B CAM" lane.
3. **Canon letter parsing:** C80 clips are `C_0001C002X261106_…`: the regexes in `camera_letter` must accept the
   `C_0001C002` form (letter, underscore, 4-digit reel, C, 3-digit clip). Folder names `UDP_261106_C500_A_001` and
   `UDP_261106_C80_B_001` carry the letter after the model: add a `_([A-Z])_\d{3}$` folder rule as a fallback.
4. **Track roles from a real 633 report/iXML.** Names seen: `BOOM `, `LAV`, `BOOM -6db`, `LAV -6db`, `MIX`,
   `BOOM1`, `BOOM2`, `LAV1`, `LAV2`, `Ale Best Mic`, `Geo Best Mic`, `Boom`, `Lavs`, `Lav1 Alex` … `Lav 5 Clara`.
   Rules to add to `refplan.role_of`: `best mic` → mix; plural `lavs` → mix (a lav submix); `-6db` / `safety` /
   `-6` / `-12` → `safety` (a copy of another track: never a reference; align it with the same offset as its
   parent if the parent is aligned, else pass through); numbered `BOOM1/BOOM2` stay boom; `Lav1 Alex` → lav with
   speaker name `Alex` (strip `Lav\s*\d+\s*`). Also: iXML TRACK_LIST is authoritative over bext `sTRKn` (the bext
   list in ConversatT01 skips index 6 and is mangled in ConversatT02; the iXML is clean). Transcript speaker names
   should come from the stripped name (`Alex`, `Ellie`, `Emma`, `Bailey`, `Clara`), not `LAV1 ALEX`.
5. **(Resolved 1.4.1: there is no gap; see Progress.) Sound report Start TC ≠ file TC.** The 633 CSV says GeorgiaT01 starts 10:48:56:00; bext and iXML both say
   10:49:34:22. The gap grows through the day (39, 44, 54, 63 s). Treat the report as a frame-rate and track-name
   source only, never as a timecode source. **Verify** on the first real run: the waveform check must decide whether
   the bext TC is right (expected: cameras rolled ~30 s before sound) or 39 s off. Camera TCs: A001C001 10:49:02:08,
   C_0001C001 10:49:13:18.
6. **Camera channel layout confirmed:** C500 ch1–2 silent (−92 dB), ch3–4 live (onboard, −45 dB RMS): the "scratch on
   3 and 4" case. C80 ch1 hot (−30 RMS, peaks −1.5 dB: probably the mixer's hop), ch2 quiet, ch3–4 live. The picker
   (`refpick`) already skips silent channels and detects feeds, so expect `C500 ch3` or `C80 ch3/4` to win: log it.
7. **Scale.** Single clips of 392–482 GB, 60–76 min. Everything that reads camera audio must stream (iter_audio does),
   never load a clip; envelopes must come from the cache on re-runs; the Resolve timeline build must not import
   picture for alignment (audio-only import or proxies). Preflight should warn when a camera clip is over 100 GB
   that Resolve will need the CRM plugin and time to index. Space estimate: corrected-sound set = sound size only
   (clones on the same APFS volume are free; on a different volume it's a real copy).
8. The sound card has `TRASH`, `FALSETAKES`, `UNDO`, `.Spotlight-V100` folders: skip `TRASH`, `UNDO`,
   `.Spotlight-V100`, `.fseventsd` outright; put `FALSETAKES` clips in with `include=False` and a chip "false take".

## B. What to take from Tentacle Sync Studio (seen, not guessed)

Layout: left column = clip list (thumbnail + name + facts), right top = player with big timecode, right bottom =
sync map (timeline), one toolbar across the top: Import · Export [XML | AAF | Media | Wave] · Sync Info ·
Read AudioTC · Sync Map · Settings. One window, no panels to open.

1. **Clip list rows are dense and factual**, four lines each: name; `62.6 GB | Duration 00:14:53:12 | *11/6/26,
   8:42:44 PM`; `Video ● Cinema RAW Light, 6000x3164, 23.98fps`; `Audio ● 4 x [LPCM, 1CH, 24bit, 48000Hz]`;
   `File-TC ● 19:41:52:16 (23.98fps)`; `Audio-TC  no Audio-Timecode found`. Green dots = read OK. A big numeral
   badge on the thumbnail = sync group. A green corner ribbon "(synced)" appears after sync. **Do:** rebuild our
   table as this card list (thumbnail from the frame cache, four fact lines, one status ribbon), grouped under
   lane headers. Keep our chips but only for things that need a person.
2. **Timeline = one V track per camera lane + one audio track per channel**, labels `V1`, `A1…A11` with M/S
   buttons; camera audio channels are listed under the video as A1–A8 (one per channel), recorder tracks after.
   Clip blocks are rounded, pale green (audio) / blue (video), with a small waveform glyph. Ours is close; adopt:
   mute/solo per lane for the playback preview, pale clip blocks with dark text, no dashed ghost unless hovering.
3. **Zoom slider at the bottom left** with a percentage readout, plus pinch. Add the slider next to our +/− keys.
4. **Big monospace timecode in the player** for the playhead; the picture appears when the playhead is on a clip,
   else black with the TC. Ours should match: a single large playhead TC over the viewer, not a side block.
5. **Settings are a single sheet**, grouped: Import (Read Audio Timecode on Import; Automatically apply AV Shift),
   Synchronisation Behavior (Automatically set Sync Groups; Linear Timeline Mode (preserve gaps); Use file creation
   date to separate footage of multiple shooting days), Export (Synchronized clip names; metadata source), Codec
   Support (RED/BRAW/ProVideo/MXF importer versions). **Do:** fold our "More options" and the per-step checkboxes
   into one Settings sheet with the same four groups; the main panel keeps only Sync to / Frame rate / Output / Go.
6. **Sync Info** toggles a per-clip overlay instead of a separate panel. **Do:** our attention list stays, but the
   review stop should also mark fixed clips in the list with the same corner ribbon style ("fixed", orange).
7. What Tentacle lacks and we keep: the review stop before writing, originals lock, Auto-Align, polywavs, slow
   motion separation, transcript, frame-rate assessment.

## Progress (1.4, 2026-11-06)
- C1 done: A1-A3 (CRM tmcd, serial + model from the uuid box, card-folder and C80 letters), CRM/REEL_xxx treated as a
  card. Real A005C004 = 17:17:58:07 / 582022100067 and C_0001C002 = 10:56:37:05 / 012919000260, read in place.
- C2 done: A4 roles (safety copies, Best Mic / Lavs mixes, iXML interleave order), speaker names (ALEX, CLARA...);
  A8 (TRASH/UNDO skipped, FALSETAKES left out with a chip).
- C5 mostly done: B5 settings sheet, B3 zoom slider, B4 big timecode over the picture, B1 facts line per clip, plus
  undo/redo and the full keyboard map (Nick asked for both). Still open: B2 mute/solo per lane, pale clip blocks,
  B6 "fixed" corner ribbon (the chips cover it for now).
- C3 done (1.4.1): the real day scanned read-only with the app's own code (Cowork VM, code delivered through a Desktop
  folder Nick connected): A CAM C500 II 6 cards / 16 clips, B CAM C80 6 cards / 15 clips, 7 takes on one 633 card in
  3 layouts (4, 6, 8 tracks), 23.976 checked against cameras, sound files and the report. Nick also loaded it in 1.4
  on the Mac: same lanes. The 633's layouts showed the sound lane mislabelling track rows; fixed (rows list the names
  used at each position, role 'varies' when the takes disagree).
- **A5 is not a problem: the sound report and the files agree.** GeorgiaT01 bext = 10:48:56:00, same as the report.
  The "39-63 s" gap came from my hand check converting bext samples at 24 fps real time instead of 23.976 (0.1% of
  the time of day: 39 s at 10:49, 63 s at 17:31). The app converts correctly. Sound rolled ~6 s before A CAM.
- C4 next: one run with Auto-Align off on the Mac to see the waveform check's verdict on the real day.

## 1.5 (2026-11-07): speed and pictures
- **Camera audio straight from the sample table** (media._iter_qt_pcm): PCM audio in MOV/MP4/CRM/BRAW is read chunk by
  chunk from stco/co64 + stsc + stsz, never through the demuxer. ffmpeg read the whole file (picture included) to get
  the audio: 17 GB to get 164 s of audio from A002C001 (21 s); the new path reads it in 0.27 s, and the 483 GB
  A001C001 (76 min) in 7.9 s, sample-identical (checked on the real files and on every test MOV/MP4/CRM/BRAW).
  Falls back to PyAV for anything else (AAC, MXF, odd stsz). PREP_SLOW_AUDIO=1 forces the old way.
- **Resolve start-up:** the first real run stopped at "Resolve would not create a new project": the app asked 20 s
  after launching Resolve and gave up in under a second. It now waits up to 3 minutes for Resolve's project manager
  and skips names already taken.
- **Pictures:** every CRM carries the camera's 2048x1080 preview JPEG (top-level uuid eaf42b5e..., box PRVW) and a
  160x120 THMB in the moov uuid. media.embedded_preview reads them; the viewer shows the preview for Canon RAW
  ("camera preview, start of clip"), and the clip list and timeline tooltip get thumbnails (Api.thumbs, cached in
  CACHE/thumbs). "Open in Cinema RAW Development" (⇧⌘O) hands a clip to Canon's app (installed on Nick's Mac).
- **Real-day result of the first run:** the timecode check found nothing to fix (no clips moved).
- **Still to speed up:** read several clips at once during the check; run the transcript while Resolve renders;
  later, Resolve stills at the playhead and small preview copies for scrubbing Canon RAW.

## 1.6 (2026-11-07, overnight): what the first real run through Resolve taught us
The 1.5.2 run of 261106 got to Auto-Align in ~5 min, then stopped at "Press Render" (0 of 32). Three separate
problems, all fixed in 1.6:
- **The reference was one camera card, not the camera.** Rolls are cards (UDP_261106_C80_B_001…006); since 1.4 the
  window groups them into lanes, but the reference still meant the picked roll only: "Reference:
  UDP_261106_C80_B_001 channel 1 → 8 WAV(s)". Only the Georgia takes (shot on B card 001) got reference pieces; Alex,
  Conversat and Friends had nothing to align to. Now `Job.ref()` carries `rolls` = every card of that body (the
  lane), used by the listening (wavesync `ref_rolls`), the reference WAVs, the transcript and refpick (which also
  compares camera bodies, not cards). Log: "Reference: B CAM (6 cards) channel 1".
- **Auto-press could never work.** The Auto-Align Post panel is its own Electron app (bundle com.github.Electron,
  window "Auto-Align Post"), not part of Resolve's AX tree, and its Render button stays greyed out until a reference
  is chosen and sources are ticked: Reference defaulted to the empty "Audio 1", no sources ticked. New
  `aap_panel.py`: finds the Electron process by its window title, picks REF in "Reference Track", ticks every
  source track (the list draws ~6 rows; Tab walks down to draw the rest), checks Time/Phase, checks "N selected",
  presses Render, confirms "Rendering…". Tested against a fake panel that behaves like the real one
  (tests/test_aap_panel.py). Resolve's empty default track is now deleted after our tracks are added (Resolve
  refuses to delete a timeline's last track, which is why it survived before), so REF is A1.
- **The renders were invisible to Prep Station.** By hand (overnight) the panel rendered all 32 pieces in ~4 min,
  but (a) Auto-Align's destination was "Same as project", not _SYNC_WORK/AAP_RENDERS, and (b) AAP 2 under Resolve 21
  names files `<src>-AtAlgn-<hash>.wav`, without `_si0ch0off<N>`. Now: the wait loop asks Resolve where the files in
  its "Aligned files for <timeline>" bin live (`ResolveBridge.render_dirs`) and watches there too; RENDER_RE takes
  both names; `Assembler.locate` places an offset-less render from the timeline plan (aap_plan.json): candidates =
  each piece of that file with 0/1/2 s handles or the handle its length implies; length narrows, the waveform
  confirms (|cc| ≥ 0.2 at a lag under 0.2 s); the offset is always a candidate's exact value, so Auto-Align's own
  shift is kept. Tests: test_aap2_names.py (handles, cut-short render, two equal-length pieces, 0.5 s handles, an
  unrelated render left out) and run_pipeline.py with AAP2=1 (all 12 renders placed to the sample).
- **Speed:** the transcript starts as soon as the corrected sound is written and runs while Resolve renders (its own
  % in the step list); listening reads 3 clips at a time (PREP_LISTEN_THREADS; results identical to one at a time).
- **Canon RAW playback (first cut):** "Make playable" in the viewer (⌥-click: every RAW clip of the day) has Resolve
  render a 960x540 H.264 viewing copy per clip into the app's cache (project "Prep Station viewing copies");
  the viewer then shows the picture anywhere in the clip, and Play steps through it once a second. Not while a
  sync runs. Untested against real Resolve yet: the render settings calls are the documented scripting API.
- **Still open:** Auto-Align's destination could be set to our folder through the panel's settings (Custom /
  Choose opens a file dialog; not automated). Mute/solo per lane, pale clip blocks, "fixed" ribbon.

## 1.6.1 / 1.7 (2026-11-07, evening): the first full real run, and what it showed
- **The whole day ran end to end** (1.6): 31 CRM clips, 7 takes, Auto-Align 69 pieces in ~10 min, 34 aligned ISOs,
  9 day polywavs, XML + CSV, transcript, report. Total about 15 min once Auto-Align behaved.
- **Auto-Align's backend hangs on a hidden dialog.** AutoAlignPost (the backend binary) crashes on *exit* after every
  successful render (EXC_GUARD in its PACE/iLok teardown, macOS 26.5; crash reports sent to Nick). macOS then shows a
  "reopen windows?" dialog on the next launch; started from the panel in the background, nobody can answer it, so
  the panel waits at "Converting... 0/N" for ever (the panel's log ~/Library/Logs/aap-davinci-resolve-log.txt ends
  on `AutoAlignPost --get-known-formats` with no answer). 1.6.1: `RA.backend_check` clears
  ~/Library/Saved Application State/com.soundradix.AutoAlignPost.savedState and runs `--version` with a timeout
  before building the Resolve project; the wait loop says what to do if nothing lands in 4 min; "Press Render again"
  checks first too. How the panel works (read from its index.html): ffprobe each clip, ffmpeg one mono file per
  track into /tmp when needed, write a JSON, spawn `AutoAlignPost process-json <file>`, read a JSON line per
  aligned clip, move results to the project folder as `<src>-AtAlgn-<hash>.wav`, import to an "Aligned files for
  <timeline>" bin. Polywavs are fine with it (it extracts channels itself).
- **Sync verified on the real day, independently of the app's own numbers** (boom on the polywav vs the C80
  reference mic, 3 points per camera clip, FFT lag): 31 of 34 confident points at 0.0 ms from 10:49 to 19:49, fit
  0.5 ms/hour (noise). Script: tests/measure_drift.py (runs on the Mac against the originals).
- **Track names did not carry through** (1.6): `channel_layout` took each roll's first take's names and squeezed
  later takes in by position, so the 633's 6-track Conversat BOOM1 sat on a track called "BOOM -6db" and the
  Friends lavs were numbered. 1.7: layout by (roll, track name) across the day; a recorder with several layouts
  gets **one polywav set per layout** (`_POLY_ALN_A_`, `_B_`...; `poly_layout='merged'` gives one wide layout with
  every name, 19 tracks here = 4 GB every 26 min); the XML names its audio tracks (`<track><name>`); `poly_style`
  setting (auto / day / per_take); report lists the sets. tests/test_layouts.py covers both modes with tones per
  (take, track). Note the real day has 4 layouts, not 3: Conversat T01 (`MIX, MIX, ...`) and T02
  (`Ale Best Mic, Geo Best Mic, ...`) differ by name; T01 is 46 s, so set B is one 40 MB file. Merging layouts
  whose aligned mics match would be a reasonable later refinement.
- **Premiere could not be driven from the background** (no accessibility tree), so the check was done on the files.
- Still open: auto-press of the panel has not been seen working end to end (every run so far hit the stuck
  backend first); RAW viewing copies untested; mute/solo, pale blocks, "fixed" ribbon.

## C. Order of work for the next model

1. A1–A3 (CRM timecode, serial, letters) + a test using the small real clips (A005C004 370 MB, C_0001C002 1.3 GB):
   stage those two into the container, assert TC 17:17:58:07 and 10:56:37:05 and serials.
2. A4 roles + transcript speaker names; extend `test_transcript` with these names.
3. Run the real day scan read-only on the Mac through the headless CLI (`--scan-only`) once the app bundle has
   the fixes: expect 2 camera lanes, 7 takes, 23.976 "checked against cameras, sound files, sound report".
4. Then one full run with Auto-Align skipped (`--no-aap`) to see the timecode check's verdict on A5.
5. B1–B5 window pass. B5 first (settings sheet) since it simplifies the panel the most.
6. Rebuild, test (`tests/check_originals.py` must still say ORIGINALS UNTOUCHED), push.

Repo: github.com/nickmidwig-pixel/prep-station (private). Tests and data layout in README.md.

## 1.8 (2026-11-07, night): fix the fewest sources

Nick: "if 2 cameras are in sync, but audio is off it is better to change the audio … we should be changing the
least amount of sources as possible … often there is one timecode box somewhere that doesn't get plugged in
correctly or a GoPro that gets added to the mix — use your best assessment to figure out what timecode should
get corrected."

Done (wavesync.consensus / decision_text):
- A *source* is one clock: a camera body (every card it shot, via the lane) or a sound folder. Sources whose
  clocks agree form groups; the biggest group is the truth and is never touched. Ties: the group with the
  reference camera, then the one with more clips. Everything outside the truth is corrected onto it, however
  many files it shot (so one GoPro moves, not three cameras; a recorder with a dead TC box moves, not the cams;
  and if the reference camera is the odd one out, *it* moves, not the two sources that agree).
- The decision is one line in the confirm step, the log and the report:
  "A CAM + B CAM agree · 633 off by -2.41 s → correct 633 (2 files), not the 4 that agree."
  Per-clip oddities (one card jammed late) are named too: "B001C002 (B CAM) off by -7.47 s on its own".
- Two measurement bugs found by the new fixture, both real on Nick's days: (1) a clip was measured against the
  other cards of its own camera (same clock, never overlapping) and a 5-second garbage match pulled the median;
  now a clip is never checked against its own body and a verdict needs ≥15 s of shared sound. (2) A clip's
  position came from the median of every roll's opinion, so one confident wrong roll could tip it; now the
  biggest agreeing group of rolls decides and a tie leaves the clip alone.
- After every render is in, the Auto-Align panel is closed (its backend crashes on exit so the panel never hears
  "done" and would spin forever).
- Tests: tests/make_consensus_day.py (audio_off, gopro, ref_off, all_good) + tests/test_consensus.py, in
  check_originals.

Not done / caveats:
- Zaxcom-style recorders that write one folder per track look like several sound sources (one per folder) and
  so count several times in the vote. Fine in practice (they agree with each other), but a day with two cameras
  vs a 6-folder Zaxcom would move the cameras. Group audio folders by recorder (bext originator + same parent
  folder) when that comes up.
- Merging layouts whose aligned mics match (Conversat T01/T02) — waiting on Nick.

## 2.0 (2026-11-08, morning): simple, simple, simple

Nick's notes after the 1.7 run, thinking as an editor opening the app and the folder:

- **Make playable** (RAW viewing copies through Resolve) — gone. Opening Resolve to make proxies is not an option.
  The viewer keeps the camera's own RAW preview frame and the clip thumbnails.
- **Sync to** was three tabs (Auto / Camera mic / Audio only) plus a dropdown of every card and a channel menu.
  Now one sentence ("Everything syncs to B CAM's onboard mic.") and a *Change* link whose chooser lists only
  the camera bodies and "Sound only" (+ "Let Prep Station pick" once you've chosen). The app always listens for
  the channel itself, also when you pick the camera (`reference.listen`, `refpick.only_lane`). API:
  `set_sync_to('auto' | 'audio' | <lane key>)`; `set_setting('mode')` / `set_reference` still work.
- **Clip list**: a sound file is one line — name, scene/take, size; Tracks shows "4 tracks · boom, lav + 2 safety
  copies" and opens (▸) into the numbered track list. No chip row of track names. The facts line lost
  "24-bit 48 kHz", "4 × audio", "TC from bext/tmcd" (still in the Info sheet).
- **Output folder**, for an editor:
  `ISO_ALIGNED/ · POLY_ALIGNED/ · SOUND REPORT/ · TIMELINES/ · TRANSCRIPT/ · WORK FILES (not for the edit)/`
  TIMELINES was EDITORIAL, TRANSCRIPT was TRANSCRIPTS. SOUND REPORT holds copies of the mixer's CSV/PDF found next
  to the media. WORK FILES (sorts last) holds the report, SOUND_TC_CORRECTED, plans, caches, logs; `brand.WORK_DIR`.
  No READ ME any more. CAMERA_TC_CORRECTED stays top-level when made (the XML points at it).
- Kept against the notes: the -6 dB safety tracks stay inside the polywavs (the mixer recorded them; they are not
  written as ISOs); the report is still written, in WORK FILES.
- tests/shot_ui.py: Playwright screenshots + smoke test of the new controls (in check_originals).

## 2.0.1 (2026-11-08): the mixer's files, aligned

- Nick, looking at `20261106_POLY_ALN_A_01of03_TC104856.WAV` next to the mixer's `GeorgiaT01.WAV`: "I just want this
  to feel like the same files that came off the sound mixer, but aligned." `refplan.delivery_style` no longer
  turns takes over 25 minutes into day polywavs: a mixer's multitrack takes are always delivered per take, same
  names, same folder (`POLY_ALIGNED/11-6-UDP/GeorgiaT01.WAV`), same bext/iXML (scene, take, notes, track names),
  so Avid's bin and the sound report line up file for file. Day polywavs only for mono-ISO recorders.
- Avid and sound reports: Avid reads scene/take/notes/track names from the BWF itself; the CSV/PDF is for the
  assistant checking the bin. It is copied to SOUND REPORT/ (2.0); nothing else needed.
- The "reopen windows?" box showed up *after* the 1.8 run (the backend crashes on exit when the panel closes);
  1.8 only answered it while waiting for renders. Now a sweeper thread answers it every 5 s from the moment the
  panel is closed until the job ends, plus one last look before "done".
- Correction to what was said about safety tracks: ISO_ALIGNED does include the -6 dB safety copies
  (`AlexT01_BOOM_-6db.WAV`), as recorded. Left that way (everything carries through); say so to Nick.

## 2.0.2 (2026-11-08): a drive that blinks doesn't end the run

- Nick's 2.0.1 run: polys/ISOs/XML perfect, then at 10:43:18 the loaner drive was unreachable for a moment:
  the transcript hit ENOENT on FriendsT01.WAV and the report step EACCES on the volume root, in the same second.
  Transcript, SOUND REPORT and the report were lost; the job ended in error.
- `transcribe.patient()` retries a read for up to 2 minutes on OSError; the report step is wrapped the same way
  and, if it still fails, the job finishes with "Report could not be written" instead of an error.

## 2.0.3 (2026-11-08): the reopen dialog after "Press Render again"

- Nick's 2.0.2 run: power to the loaner drive dropped at 28 of 69; Auto-Align's backend died. "Press Render
  again" worked, but every backend launch after a crash (--get-known-formats, --has-license, process-json) gets
  the "reopen windows?" box, and the wait loop only answered it while the count was 0. Stuck at 28 of 69 until
  Claude clicked three boxes by hand. Now the loop looks every 3 s whenever nothing has landed for 5 s, however
  many files are already counted; the 4-minute stall hint too.

## 2.1 (2026-11-08, afternoon): DONE you can see, and hear the difference

- Nick: "if I was glancing at this for a second I wouldn't know it was finished." A big green DONE banner with a
  tick replaces the progress bar when the run ends ("20261106 is synced · finished 2:31 PM"); the Sync button
  turns quiet. Hidden again on the next run.
- "Hear the difference" button (viewer, after a run): at the playhead, the camera's reference mic summed with
  the boom - 4 s from the mixer's ORIGINAL file at its own timecode, 4 s from the ALIGNED file, twice - with a
  BEFORE / AFTER label over the picture. A misaligned pair sounds hollow and doubled; aligned sounds like one
  mic. Audio-only days compare boom + lav. `Api.ab_play`, tests/test_ab.py.
- Premiere check of the 2.0.2 run (full-screen control): XML imports clean, 39 items linked; our files' BWF
  metadata reads 23.976 NDF when imported directly (same as the mixer's originals). Premiere shows any WAV that
  comes in *through an XML* as 29.97 DF (10;49;35;00 for 10:48:56:00) - proven with the mixer's original file
  through a test XML, and with four ways of writing the timecode element (24 ntsc, 30, none, no rate). Premiere
  behaviour, not ours; positions on the timeline are right. Workaround for editors: import POLY_ALIGNED first,
  then the XML.

## 2.2 (2026-11-08, late afternoon): control and readability

Nick's notes, all in:
- "Hear the difference" removed (was a one-off for his ears, not a feature).
- **Slow motion, best effort** (scan._mark_slowmo). Canon's manuals: S&F records no sound and forces timecode to
  Rec Run. Two new tells for silent clips, no camera-specific metadata needed:
  `clock` — the file's creation stamp and last-write time say how long the take really took; a clip that plays
  >1.3× longer than that was shot faster than it plays. `recrun` — on a jam-synced card every clip's TC sits a
  fixed distance from the camera clock; a silent clip more than 60 s off that line is S&F in Rec Run.
  Existing tells stay (other time base, capture fps in the file, TC overrun into the next clip, marked by you).
  Fixture: E_C500 card in tests/test_slowmo.py. Still wanted: Nick's two-clip C500 II test to see whether Canon
  writes the shooting rate at all (would make it a sure thing).
- **Timeline control**: drag a box on empty timeline to select everything it touches (⇧/⌘ add to the selection);
  ⇧-click range and ⌘-click toggle as before. With several clips selected the selection line offers
  "Mark as slow motion (n)" / "Not slow motion" for the cameras in it (`Api.clips_slowmo`).
- **Mute / solo** boxes (M, S) on every lane label. When any is set, Play mixes every audible lane under the
  playhead (its best channel: camera reference channel, mix else boom else first mic), 60 s at a time
  (`Api._play_mix`); otherwise Play behaves as before. Changing M/S while playing restarts the preview.
- **Clip list**: File / Start / Length headers sort (click again to flip), inside the camera/sound groups.
- **Fit** and the zoom slider moved above the timeline, into the toolbar. "Left out" dropped from the legend.
- **Readability**: base type 13 → 14.5 px and every small size up a step; muted grey brightened (#8E969E →
  #A7AFB7); lane rows 34 → 38 px; lane names 13.5 px; roomier clip rows.

## 2.2.1
- Drag-select on the timeline now starts anywhere in the lane area, including on top of a clip (previously a press on a clip only selected that clip, so a band could not begin there). Text selection / image-drag on the canvas is suppressed so Safari/WebKit does not steal the gesture.
- Camera lane colours follow tape convention: A red, B blue, C yellow; D green, E orange, F purple, G teal, H pink (no industry standard past C — these were chosen to stay distinct from the red/blue/yellow trio and from the sound colours).
- Legend shows the three camera swatches instead of a single "Cameras" chip.

## 2.2.2
- Shoot-day grouping (from the 20250208 RODEO day): a camera whose clock date is wrong (C500 II saying 2019) no longer becomes its own day tab even when it holds most of the dated clips — a camera-only date with no sound recorder agreeing and no neighbouring day is placed by its timecode. Zaxcom ISOs recorded after midnight but stamped with the evening's date stay with the evening ("recorded after midnight").
- Canon C80/C400 `XFVC/REEL_xxxx` (XF-AVC S / XF-HEVC S .MP4) folder now counts as part of the same card as CONTENTS and CRM — no phantom "C CAM".
- tests/test_days.py covers both, plus a real camera-only next day that must stay separate.

## 2.3
- **Master date from the folder.** The folder you drop (or a dated folder under it) names the day: `20250208`, `2025-02-08 rodeo`, `UDP_261106_C80` all count. Every clip inside gets that date whatever its camera or recorder clock says (note: "camera date says 2019-04-27"). A single drop with no date in its name is treated as one day too. Dated folders side by side under one drop stay separate days; separate undated drops keep the clock-based grouping.
- **Canon clip names carry the record-start clock** (`B_0001C015A250209_001152HN` = 00:11:52). Used for the clock tells: XF-AVC .MXF files stamp the END of the take as creation time, RAW/MP4 the start, so the name is the one stamp that always means the same thing. A silent 23.976 Canon clip that runs longer than the name→stamp span is S&F → slow motion ("clock").
- **JKL shuttle.** L plays forward, J backwards, each tap one step faster up to 5× (rendered with areverse/atempo, afplay); K or Space stops; Space from a stop plays 1×. Badge next to Play shows direction and speed. The back-5-seconds button is gone.
- **Clip list:** fps column (sortable; a rate that isn't the day's is highlighted), draggable column widths (double-click a grip to reset), kept between launches.
- **Panes:** drag the bar under the viewer to trade timeline height for clip-list height (lanes grow with it); drag the edge of the side panel to widen it. Double-click either to reset. Kept between launches.
- Legend no longer lists the camera swatches (the lane labels already say A/B/C).

## 2.4
- **Two worlds on the timeline.** Real-time lanes on top; under a hatched "SLOW MOTION" divider, a second stack with one lane per camera holding only its off-rate / S&F / marked clips. A clip is in exactly one world, so nothing slow ever sits beside synced sound. Slow lanes show the rate and playback percentage instead of the camera model and have no M/S (nothing synced to hear). The clip list mirrors it: a SLOW MOTION section with its own camera groups. Marking / unmarking a selection moves it between worlds.
- Hatch overlay and its legend entry dropped (the divider says it). Transport help line shortened. tl-bar tools no longer wrap.
- tests/shot_day.py: screenshot any day folder; `window.MS.rows()` exposes the row plan for tests.

## 2.4.1
- **Playback is one player at a time.** A play request renders first (ffmpeg) and only then starts afplay; Space during that render used to fire a second request, and the first one started its player anyway once it finished — an orphan nobody could stop. Now every request takes a generation number, a stop or a newer request retires the old one before it can start, the Play button shows "… Stop" while rendering so Space / K cancel it, and each render writes its own file. The player is killed when the window closes and at process exit (no more sound after quitting).
- "N to know about" strip shows only what blocks the run (no timecode, unreadable files, failed checks); informational notes stay on the clip rows and the summary line.
- tests/test_play_race.py.

## 2.4.2
- Slow-motion tells carry a confidence: the file's own rate, a capture rate in the file, and your word are certain; the clock tell (camera took 208 s, plays 1036 s) and the timecode tells are strong but inferred, so those clips read "Slow motion · 5× in camera — confirm?" and the summary line offers "N to confirm as slow motion" → the inspector asks "Yes, slow motion / No, real time". Confirmed clips become certain.
- The inspector hides the whole "Find by waveform" block for slow motion (it isn't synced, so there is nothing to find).
- Jumping to a clip from a note scrolls its row into view and opens its group (a clip without timecode has no place on the timeline to jump to; the row is where it lives).
- Slow lane label: "59.94 fps · 40% speed".
- On the 20250208 card the clock tell found the two in-camera S&F clips Nick described (C017, C029: XF-HEVC S MP4s, 17 and 14 min, shot in 208 s and 171 s = 5×, i.e. 120 fps → 23.976).
- **Corrected sound moves to the top level** (Nick, on seeing the first confirm prompt on 20250208): `<date>_SYNCED/SOUND_TC_CORRECTED/` mirrors the folders the sound came in below the dropped folder (`SOUND/01LAV/…`, `SOUND/02LAV/…`), not WORK FILES. WORK FILES keeps the report, plans, caches and logs.

## 2.4.3
- Timeline stacks cameras like an edit: A CAM sits right above the sound (V1), B CAM above it, C above that — "it moves up from there". The exported XML already had A on V1, B on V2 (checked). The clip list stays A-first (it is a list).
- Cameras with no letter anywhere (card name, clip names, your ⌘G) are lettered by how much they shot: the body that carried the day gets the next free letter, a phone or GoPro with a few clips falls to the end. The reference pick no longer influences lettering. tests/test_days.py covers it (A labelled + unlabelled C80 with most footage + iPhone → A, B, C).

## 2.4.4
- Inspector header shows the clip's frame rate next to the codec — "23.976 fps (shot 5× faster)" or "(shot at 119.88)" for slow motion, highlighted; the slow-motion question starts with the file's rate.

## 2.4.5
- Step through everything waiting for a slow-motion decision without leaving the inspector: "‹ 2 of 3 ›" next to the Yes / No buttons (⇧⌥↓ / ⇧⌥↑), in timeline order; answering Yes or No moves on to the next one. The summary-line link opens the first in timeline order.

## 2.4.6
- **Viewer, header and timeline name the same clip.** Selecting a clip (click, list, ‹ › stepper, a note) now moves the playhead into it, so the picture, the big timecode and the highlight agree; before, the viewer kept showing whatever camera was under the old playhead (Nick saw "A CAM · A001C003" over a selected B CAM slow clip). The selected clip always wins in the viewer, labelled "B CAM · slow motion · <name>". The inspector header leads with the camera as the timeline says it ("B CAM · B01 · C80 · hevc · 23.976 fps …") instead of the card name.
- **Right-click a clip** (timeline or list): Show in Finder, Open in its app, Copy file name, Copy timecode, Mark as slow motion / Real time, Leave out / Use.
- **Each slow-motion block carries its own rate** ("59.94", "23.976 · 5×") in a dark tag at its left edge; the slow lane label is just the camera ("C80 · slow motion") instead of a mixed list of rates.
- Slow-motion question text uses the panel's full width, buttons below it.

## 2.4.7
Nick confirmed C017 and C029 (C80, 120 fps S&F conformed to 23.976 in camera, MOS), and set the rule: **silence is never a slow-motion tell** — B-roll, an unplugged input or a timecode-only interview camera are silent and real time.
- Inferred slow motion now rests on two clock tells only, neither of which looks at sound: **clock** (record start from the clip name / creation stamp → end stamp / last write vs how long it plays) and **next** (the next clip on the same card started — by camera clock, else timecode — before this one would have ended in real time). Both together = certain, no question asked; one alone = shown and asked once. The rec-run tell (silent + timecode off the clock line) is gone: an unjammed silent camera looked exactly like it.
- A start stamp within 3 s of the record start is not taken as the end of the take (Canon MP4/RAW stamp the start); C029 had been reading as a 1 s take.
- Wording is how a camera person says it: "Shot at 120 fps · plays at 23.976" (header), "Slow motion · shot at 120 fps" (list), "120 fps" tag on the block, "59.94 fps" for the shoot-high clips; the reasoning reads "Shot at about 120 fps, plays at 23.976: the camera took 3:28 to record a clip that plays 17:16; the next clip on this card started 3:58 after this one". Next-gap-only gives a lower bound: "Shot at 72 fps or faster". Rates snap to common camera rates (24 … 120 … 240).
- Viewer: the moment a clip is picked the picture box says which clip it is fetching ("B CAM · slow motion · <name> · loading…") instead of keeping the last camera's frame up.
- On 20250208: 23 × 59.94 (certain, file rate), C017 and C029 both "shot at about 120 fps", both certain; nothing left to confirm.

## 2.4.8 (from Nick's look at the 20250208 output)
- **SOUND_TC_CORRECTED/ holds only the files whose timecode changed** (in their source folders, SOUND/03_LAV/…). The full working set Auto-Align reads (fixed files + instant clones of the rest) is back in WORK FILES/sound_for_align/. On 20250208 the visible folder had 9 full copies (one fixed) plus 45 Auto-Align pieces.
- **Auto-Align renders never stay in a delivery folder.** AAP writes `*-AtAlgn-*.wav` next to its sources; 2.4.2 moved the sources into the visible folder, so 45 render pieces landed in SOUND_TC_CORRECTED/SOUND/01LAV. Renders found outside WORK FILES/AAP_RENDERS are moved there after collection (our output only), and the sources are in WORK FILES again.
- **Wrong 3.5-hour correction fixed.** 03_LAV_004 (22:52:04, the straight continuation of 03_LAV_003) was moved to 19:19:28 by the whole-day search on an 11 s edge overlap with 02LAV at 0.87. The whole-day search now needs ≥ 60 s / a quarter of the file of shared sound, and never runs on a file that carries straight on from the previous one on the same recorder (allowing the 0.1 % 23.976 drift: a 2.5 h file ends ~9 s "late" in timecode). The A001C001 fix (+117.32 s) stands: it brings the C001→C002 gap in timecode (8:10) to the camera clock's (6:13).

## 2.4.9
- The timeline is bounded to the day: one hour before the first clip to one hour after the last. Zooming out stops there, scrolling stops there, the playhead can't leave it; the buffer hours are shaded so the day's edges read at a glance.

## 2.5 — Prep Station never converts picture (Nick, 11/09)
"Files of different frame rates need to be converted before brought into the software … this can only be changing the time code on a file." Some off-rate clips are slow motion (59.94 on a 23.976 day), some are real time shot at the wrong rate by accident (an iPhone at 29.97); only you know which.
- **Frame-rate sheet before the run.** Sync the day opens "Clips at another frame rate" whenever a camera has clips at another rate that you haven't decided: one row per camera and rate, **Slow motion** (own timeline, never synced) or **Real time · I'll convert** (left out of this run, listed in the report under CONVERT BEFORE ADDING AGAIN). The likely answer is highlighted (≥1.5× the day's rate → slow motion, else real time); Sync the day enables once every row is answered. Also reachable from the summary line ("N clips at another frame rate — decide"). Undoable.
- **Camera timecode copies never change file type.** The exact-copy patch is unchanged (2–4 bytes differ). The old fallback (streams rewrapped into .mov) is gone: a MOV/MP4 with no timecode track (iPhone) gets one by a stream copy into the same container (.mp4 stays .mp4); anything else that can't be patched in place is left alone and listed.
- Dropped from the plan: conforming frame rates (no picture is ever converted).

## Queue
- First-run transcript: show "downloading the speech model (1.6 GB)" with its own progress instead of sitting at 0%.

## 2.5.1 (Nick, 11/09 evening: "simplify")
- Frame-rate sheet removed. Rule: **anything not at the day's rate goes on the slow-motion timeline, automatically** — 59.94, 29.97, 25, whatever. No choice to make. The only question the app still asks is for clips at the day's rate that look shot faster by the clocks (one tell alone: "confirm?").
- The report's CONVERT BEFORE ADDING AGAIN section is gone with it. Camera timecode copies keep the 2.5 rule (never change file type).

## 2.5.2 — every step is a switch
- "What happens" is a list of on/off switches: Check timecode by waveform · Stop so you confirm the fixes · Auto-Align Post · Polywavs · ISOs · Timelines (pick formats) · Transcript; Report always. A switch that can't apply is greyed with the reason (confirm needs the timecode check; ISOs need Auto-Align). Choices are remembered between launches.
- New: **Polywavs off** (make_polys) → no POLY_ALIGNED; the editorial XML is built on the aligned ISOs instead (`<job>_SYNC_ISO.xml`). **Confirm the fixes off** → fixes are written without stopping (originals still untouched; the report lists every fix).
- tests/test_step_switches.py (polywavs off → ISO XML; ISOs off → polywav XML).

## 2.5.3 — Auto-Align never works from the originals
- **Bug (2.4.8–2.5.2):** with no sound timecode fix, Auto-Align was given the original sound files, and it writes its renders next to its sources: on Nick's 20250208 run (2.5.2) 40 `-AtAlgn-` files landed in the original `SOUND/01LAV/`. The originals themselves were not touched (dates and sizes unchanged), but files were added to the original card folder. Now, whenever Auto-Align is on, the working set (instant clones) is always made in WORK FILES/sound_for_align, fixes or not. tests/test_step_switches.py asserts every Auto-Align source is inside the output folder, including on a run with no fixes.
- "Press Render again" is hidden while Auto-Align is working; it appears only when Auto-Align goes quiet for 4 minutes (the same moment the hint explains what to do).

## 2.5.4
- Timeline hover card (Nick: the slow-motion B CAM thumbnail looked sloppy): placed by its real size and flipped left/up at the window edges, no longer clipped by the timeline's edge into a half-empty grey box; thumbnail a clean 16:9 at 240 px; slow-motion clips say "Slow motion · 59.94 fps" in the card.
- The drag bar now sits directly under the timeline (above the picture box), so the timeline grows and shrinks against everything below it, like the side panel's edge.

### 2.5.4, continued — interface pass (walked at 1440×900 and 1280×800 with tests/walk.py)
- TIMELINES is sorted by use: `PREMIERE RESOLVE XML`, `PRO TOOLS AAF`, `AVID ALE`, `OTIO`, `CLIP LIST CSV`. No empty folders.
- Names: `<day>_SYNC_ALIGN.xml` (was _SYNC_ALN), `<day>_SLOWMO.xml` (was _SLOW_MOTION).
- Slow-motion XML: one track, shot order, clips butted together. The window's own timeline still shows where they were shot.
- Monitor row: picture + Play + timecode on the left, selected clip beside it, fixed height so the list never jumps.
- Legend and shortcuts live behind "?"; SYNC REF badge; review card lists file first, per-file prose removed.
- BUG fixed: after DONE the button said "Stop" (the job thread reported itself as still running).
- BUG fixed: a camera-only day waited on Auto-Align forever. Now: "No sound files on this day", skips to timelines.
- While a decision is waiting the side panel shows only the decision.

## 2.5.5 — what Nick caught on the real Mac window (the app is WebKit; tests run Chromium)
- "N without timecode" was drawn over the clips. Now "N no timecode" sits under the lane name.
- Leftover `-AtAlgn-` renders beside the sound are never read as source files (that was the "49 sound files / 40 without timecode").
- Clip labels are fitted with an ellipsis, never cut mid-letter; a rate tag shows whole or not at all.
- Undo/redo are drawn icons (the ↶ ↷ glyphs rendered as hooks on the Mac); zoom slider and dropdowns are styled explicitly, no native controls.
- Summary line shows whole notes only (short forms: 20:17 – 02:32 · 25 slow-mo), never "slow mo…".
- Sync to: "Change…" is a real button; clicking a camera's name in the timeline offers "Sync everything to B CAM's mic".
- RULE for me: anything drawn by the browser's defaults (glyph characters, range inputs, selects, scrollbars) will differ on the Mac. Draw it explicitly, and check the real window on Nick's Mac after a build when he allows it.

## 2.5.6 — from looking at 2.5.5 on Nick's real window (he opened it for me)
- 2.5.5's fixes hold on the Mac: slider, undo/redo, summary, labels, "9 sound files".
- BUG: clicking a clip in the timeline left the clip list blank until the next scroll (WebKit + scrollIntoView). The list now moves its own scrollTop (showRow).
- BUG: clicking a sliver could select one clip and show another's picture (near-miss hit, playhead left outside the clip, and frame() only let the selected clip win within 2 s). Hit-test prefers the clip really under the cursor; the playhead snaps into the clicked clip; the selected clip always wins; stale pictures are dropped.
- Settings: "Stop so I confirm the fixes" sits under the timecode check, not under Auto-Align.
- From watching a real 20250208 run on the Mac (11/11, 1 AM):
  - While running, the step that is working is lit and pulses, with a live line under it ("Getting the sound ready · 20250208_202339_03_LAV_003.WAV"); the bottom shows the step's name over the bar. Before, the active step was a hidden one (confirm switched off), so nothing was lit and the bar showed a bare file name. A switched-off step hands "active" to the next step that runs.
  - The faint text showing through above the Stop button is gone (the fade gradient was removed).
  - The big red clock is the playhead, always (with the camera · clip it's on), like an editor. It used to show the selected clip's start ("B_0001C029H250209_ starts") while the viewer showed the playhead: two different numbers.
  - Clip list Start: a moved clip shows its new timecode, with "was 20:32:26:11" underneath. Before, the struck-out old one pushed the new one to "2…".
  - Status chip "Moved +1:57.32" (was "Fixed +117.320 s"); every shift reads +7.47 s or +1:57.32.
  - Inspector: one "Timecode … from the camera" row when nothing changed; "In the file / In the edit" when it moved. No "(container)".
  - Slow-motion reason says the length once and matches the Length column (both round down): "Shot at about 120 fps, plays at 23.976. The camera took 2:52 to record it but it plays 14:14; the next clip on this card started 2:59 later."
