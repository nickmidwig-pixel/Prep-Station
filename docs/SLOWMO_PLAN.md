# Slow motion: how every major camera does it, and how Prep Station will catch it

*Researched 2026-11-08 for Prep Station 2.3. Sources: ARRI, RED, Sony, Canon, Blackmagic and Panasonic manuals and SDK notes (links at the end).*

## The one-paragraph version

Cameras make slow motion in two different ways, and that decides how hard it is to spot:

1. **Record at the real rate, play at the real rate** (ARRI, RED, GoPro, DJI, iPhone, Zoom-style action cams). The file *says* 60 or 120 fps. Easy: the frame rate in the file doesn't match the day. Prep Station already catches all of these.
2. **Record fast, but write a file that plays at the project rate** (Canon S&F, Sony S&Q, Blackmagic off-speed, Panasonic VFR). The file says 23.98 and plays at 23.98 — the slow motion is baked in. The only way to know is a second number in the file ("sensor fps", "capture fps") or by what's *missing*: no sound, and timecode that no longer matches the wall clock.

Everything in group 1 is solved. Group 2 is where the work is, and three of the four (Sony, Blackmagic, Panasonic) write the shooting rate into the file in a way that can be read. Canon is the odd one out, and it is the camera you own — so the plan leans on what Canon's own manuals guarantee S&F does (no audio, Rec Run timecode) and on a two-minute test you can shoot to settle whether Canon writes the shooting rate at all.

## Camera by camera

| Camera | Slow-mo mode | What the file says it plays at | Where the real shooting rate lives | Sound recorded? | Timecode behaviour | Prep Station today | Plan |
|---|---|---|---|---|---|---|---|
| **ARRI** ALEXA 35 / Mini LF / Mini / AMIRA (MXF ProRes, ARRIRAW) | "Sensor fps" set above "Project fps" | **Project rate** (23.976) — ARRI conforms in the file | MXF header, SMPTE RDD 54/55 metadata: *Sensor FPS* vs *Project FPS*; older ARRIs: the same pair in their own metadata; ARRI Reference Tool / Resolve show "Sensor FPS" | **No** (ARRI only records audio when sensor = project speed) | Free run / Rec run as set; ARRI keeps time-of-day TC running correctly even off-speed | Catches it when ffprobe/pyav expose the sensor fps (our `capture_fps` reader looks for "sensor … fps" keys) — **unverified on an ALEXA 35 MXF** | Read Sensor FPS straight from the MXF header (RDD 55 key) instead of hoping the demuxer surfaces it. Fallback: silent + `clock`/`recrun` tells. Needs one ALEXA 35 off-speed clip to test. |
| **RED** V-Raptor / Komodo / DSMC2 (R3D) | "Recording Frame Rate" above "Project Time Base" (Varispeed) | **Recording rate** (e.g. 120) — R3D stores the real rate; time base is separate metadata | R3D header: *FPS* (recording) and *Time Base* (project); REDline / R3D SDK expose both | **No** audio in varispeed | TC counts at the time base; RED warns Varispeed on screen | Caught by tell 1 (file rate ≠ day rate) **once we can read R3D at all** — Prep Station doesn't open R3D today | Add R3D header parsing (FPS + time base, it's a small fixed header) or require REDline. Either way: file rate 120 on a 23.976 day → slow motion, with speed = time base / FPS. |
| **Sony** VENICE 2 / FX9 / FX6 / FX3 / FX30 / A7S (XAVC, X-OCN) | S&Q Motion | **Project rate** (conformed in camera) | Clip XML (`C0001M01.XML`): `<VideoFrame captureFps="119.88p" formatFps="23.98p">`; X-OCN MXF carries the same in RDD 18 metadata | **No** audio in S&Q | **Rec Run forced** in S&Q on the FX line (free-run TC not available) | **Caught** — tell 2 (`captureFps` ≠ `formatFps`) | Nothing to add for XAVC. For X-OCN/VENICE read the MXF header value; test clip wanted. |
| **Canon** C500 Mk II / C80 / C400 / C300 Mk III / C70 / R5 C (CRM, XF-AVC MXF, MP4) | Slow & Fast Motion | **Project rate** (23.98) — conformed in camera | **Not published.** Canon's clip metadata is read only by Canon XF Utility / Cinema RAW Development; no public tag name for "shooting frame rate" in CRM or the MXF. The sidecar XML (XF-AVC `CLIP/*.XML`) *may* carry it — unverified | **No** (manual: "Sound is not recorded with the clip"; XF605/C400 can write a separate WAV) | **Rec Run forced** during S&F (manual: TC mode switches to Rec Run and returns afterwards) | Caught **only** by the three silent-clip tells: TC overrun into the next clip (`likely`), real recording span from the file's own clock (`clock`), TC off the camera-clock line (`recrun`). All three are inference, not a flag. | **Your two-clip test decides it.** Shoot one normal clip and one S&F clip (60 → 23.98) on the C500 II onto a card; drop the card in the Desktop folder. I dump every box in the CRM and every tag in the XML and diff them. If a shooting-rate field exists it becomes a hard tell; if not, the three inference tells stay primary and I add a fourth: Canon's *recorded frame count vs. clock span* from the Canon `uuid`/`CTMD` boxes. |
| **Blackmagic** URSA Mini Pro / Pocket 4K/6K (BRAW, ProRes) | Off-speed frame rate | **Project rate** (conformed) | BRAW clip metadata: `sensor_rate` (real) vs `frame_rate` (project); Resolve shows "Camera FPS"; `.sidecar` JSON can carry it too | **No** audio off-speed | Rec run / time of day as set; off-speed TC counts project frames | Reads `sensor_rate`-style keys when the demuxer exposes them — **unverified on real BRAW** | Parse the BRAW header's metadata atoms directly (the keys are plain text in the file) or use the free BRAW SDK's `braw` tool. Test clip wanted. |
| **Panasonic** VariCam LT/35, EVA1, S1H/GH (MXF AVC-Intra, MOV) | VFR | **Project rate** (conformed) | VariCam MXF: frame rate + "VFR" in clip metadata (P2 XML `<FrameRate>` and `<VFR>`/`<ShootingFrameRate>` style tags); S1H MOV: `com.panasonic.*` atoms with the sensor rate | **No** audio in VFR | Rec run | Not specifically handled (would rely on silent-clip tells) | Read the P2 clip XML (`CONTENTS/CLIP/*.XMI` / `.XML`) `VFR` + `ShootingFrameRate`; MOV: scan `com.panasonic` atoms. Test clip wanted. |
| **GoPro / DJI Osmo / Insta360 / drones** (MP4) | High frame rate modes | **Real rate** (60 / 120 / 240) | File frame rate | Yes (GoPro keeps audio even at 120/240) | None or own clock | **Caught** — tell 1 (`rate` / `timebase`) | Nothing to add. These also aren't jam-synced, so they're placed by waveform. |
| **iPhone / Android** slo-mo | Slo-mo | **Real rate** (120 / 240) | File frame rate | Yes | None | **Caught** — tell 1 | Nothing to add. |
| **Phantom / high-speed specialty** (cine files, ProRes exports) | Always off-speed | Export rate (usually project) | In the export's metadata if the operator kept it | No | Usually none / cine timer | Silent-clip tells | Treat as slow motion by default when the model name says Phantom/Chronos; let the user unmark. |

## What this means for Prep Station

### Tells, in the order they're checked (2.2 today)

1. **File rate ≠ day rate** → `timebase` (speed known) or `rate` (other normal rate). Solves ARRI-recording-real-rate cameras, RED (once readable), GoPro, phones.
2. **Capture rate in the file ≠ playback rate** → `in_camera`, speed known. Solves Sony S&Q today; will solve ARRI (Sensor FPS), Blackmagic (`sensor_rate`), Panasonic (VFR tag) once those headers are read directly.
3. **Silent clip, real recording span from the file's own clock is much shorter than the playback length** → `clock`. Camera-agnostic. Needs the copy to have kept the file's write time (Hedge, Shotput, Finder copies do; some cloud syncs don't).
4. **Silent clip whose TC sits off the camera-clock line that the other clips on the card sit on** → `recrun`. Camera-agnostic; built on Canon and Sony both forcing Rec Run in slow motion.
5. **Silent clip whose TC runs past the start of the next clip on the card** → `likely`.
6. **You marked it** → `marked`; and you can unmark any of the above.

### What 2.3 adds

- **Direct header readers** for the three formats whose shooting rate is written but not surfaced by the demuxer: ARRI MXF (RDD 55 Sensor FPS), BRAW (`sensor_rate`), Panasonic P2 XML / S1H atoms. Each one is a small, read-only parse; each needs one real clip to confirm the key.
- **R3D header** (FPS + time base) so RED days are recognised at all.
- **Canon**: whatever your two-clip test shows. Best case a hard flag; worst case the inference tells stay primary and the report says so plainly ("looks like slow motion: no sound, timecode off the clock line").
- **Confidence in the UI.** Hard flags (tells 1–2) say "Slow motion (60 fps in the file)". Inferences (3–5) say "Looks like slow motion — no sound, timecode doesn't match the clock" and get the red-stripe *needs you* treatment so an editor glances at them once. Multi-select + "Mark as slow motion" (2.2) is the fix when a whole run of B-roll was shot off-speed.
- **Never touch the sound.** A clip flagged slow motion is kept off the sync timeline and its channels are never used as a reference — unchanged from today.

### What I need from you

1. **The Canon test** (two minutes): C500 II, one normal clip, one S&F clip at 60 → 23.98, same card. Copy the card into the Desktop folder "prep station for claude". Same again on the C80 if it's handy — C80 writes MP4/XF-AVC S as well as CRM and may tag it differently.
2. **One off-speed clip** from any of ARRI / Blackmagic / Panasonic / RED the next time one crosses your desk. A few seconds is enough; I only read the header.

## Sources

- ARRI ALEXA 35 Workflow & Post Guide — MXF metadata per SMPTE RDD 54 / RDD 55 (afcinema.com PDF); ALEXA SXT technical data: audio only at same project/sensor speed.
- RED operation guides (Komodo-X, V-Raptor, DSMC2): Recording Frame Rate vs Project Time Base; Varispeed indicator; RED 101 "Intro to Slow Motion Video".
- Sony NonRealTimeMeta clip XML (`captureFps` / `formatFps`), SMPTE RDD 18 acquisition metadata.
- Canon XF605 / XF705 / XA70 / C300 Mk III manuals: S&F records no sound, TC switches to Rec Run; Canon R5 C / C80 online manuals (CRM, XF-AVC S).
- Blackmagic RAW SDK (clip metadata; Resolve "Camera FPS" = sensor rate), Blackmagic forum thread on reading camera FPS outside Resolve.
- Panasonic VariCam LT / EVA1 recording-format papers (VFR 1–240 fps, conformed recording).
