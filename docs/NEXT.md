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
