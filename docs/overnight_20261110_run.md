# 20261110 overnight run, 2.5.22, started 22:20 local (t=0 at app launch 22:15:37)
- t=303 s  Choosing the alignment track (A CAM clips <1 s each; B CAM C037 ~12 min, C042 42 s, C046 281 s x2 (one read per channel!), C050 running at 03:45Z)
- Efficiency note: refpick reads each B CAM clip once PER CHANNEL (C046: two 281 s reads). One pass for both channels would halve it even after the discard fix.
- t=2287 s (03:53:45Z) Checking timecode. Alignment-track step took 1984 s = 33 min (vs 41 this morning; A CAM clips <1 s, B CAM 40 s–12 min each, twice per clip).
- Checking timecode: B CAM envelopes 85–145 s each (whole-file reads), ~3 in parallel; 36/243 at t=2822.
- C006 envelope took 1149 s (19 min) — the biggest B CAM clip; parallel workers confirmed (overlapping). 44/243 at t=3601.
- 04:33Z: no new envelope line since t=3601 (15 min). No errors in launch.log. Big B CAM clips in flight (C006 took 19 min). Watching; call it stalled only if nothing by ~05:00Z.
- 04:41Z moving again: C021 1232 s, C023 274 s; 46/243 at t=5050.
- 04:55Z 55/243 (t=5999). B CAM C029 128 s, C030 76 s.
- 04:59Z 58/243 (t=6182).
- 05:04Z 59/243 (t=6359).
- 05:09Z 60/243 (t=6790). C034 779 s.
- 05:22Z 66/243 (t=7600).
- 05:26Z 68/243 (t=7662).
- 05:30Z 69/243 (t=7995). C037 1636 s (27 min).
- 05:34Z 70/243 (t=8119).
- 05:38Z 72/243 (t=8527).
- 05:43Z 74/243 (t=8793).
- 05:48Z 75/243 (t=9088).
- 05:53Z 77/243 (t=9436). C046 envelope 1774 s (29.5 min).
- 05:58Z B CAM listening finished ~t=9700 (TC-check B CAM phase ≈ 2 h 3 min). Canon clips now 2–10 s each; 109/243.
- 06:03Z Listening done at t=10019 (TC-check listening phase 7732 s = 2h09m, of which B CAM ≈2h03m). Per-clip TC check 215 clips in 7 s. Quiet since t=10026 — build_beds / search next.
- 06:09Z build_beds 63 s (t=10026→10089); Finding by waveform 28 C CAM clips in 85 s; Fine sync 47 clips from t=10177, B CAM read_abs 39–56 s each even with the seek (whole-file demux again), 26/47.
- 06:14:55Z FIXES STOP reached (t≈10640 → TC check step total ≈ 8350 s = 2h19m; stop appeared ~06:14Z). Proposal identical to the morning: 65 files off; A CAM + 02LAV agree; B CAM off by +22.92 s → correct 51 B CAM files (not the 164 that agree); C CAM no TC → placed by sound/clock (IMG_8554 07:58:59, 8555 08:00:04, 8556 08:02:13, 8557 08:04:37 …). Accepted at 06:15Z ("Write corrected sound and go on"). Same +22.92 s twice on independent runs/drives → it is reproducible.
- t=10640→10770: waiting at fixes stop ~130 s (my 3-min check cadence). Writing corrected sound t=10770–10895 (125 s, 2 WAVs). Auto-Align Post started t=10895 (06:17:12Z).
- Auto-Align Post t=10895→11295 (400 s = 6.7 min, no prompts). Building aligned ISOs from t=11295 (06:23:52Z).
- 06:34Z Measuring renders t=11481–11848 (6 min). Writing aligned ISO 1/2 at ~0.9 %/15 s → ~25 min per file (morning: whole ISO step 3 min). Output drive is the new one, with Nick's card copy running onto it at the same time: drive-bound, not CPU.
- 07:03Z aligned ISO 1/2 done (~24 min); file 2 at 20 %.
- Building aligned ISOs t=11295→14685 (56.5 min; morning 3 min — drive-bound while card copy ran). Checking alignment from t=14685 (07:20:22Z).
- 07:28Z Polywav writing at ~0.25 %/15 s → could be ~1.5–2 h if it holds (morning: ~3 min). Drive write speed is the bottleneck tonight (card copy onto the same drive).

# FINAL — overnight run 2.5.22, 20261110, 22:20:40 → 03:22:32 local (5 h 02 min)
Step                          wall      notes
Choosing the alignment track  33.1 min  A CAM clips <1 s; B CAM 20 reads = 1960 s (one read PER CHANNEL, 40–720 s each)
Checking timecode             139 min   listening 129 min (B CAM 51 envelopes 21,655 s summed, whole-file reads; Canon 162 clips 999 s summed); TC check 7 s; beds 63 s; find-by-waveform 28 C CAM clips 85 s; fine sync 463 s (B CAM read_abs 446 s)
Wait at fixes stop            2.2 min   (my 3-min polling)
Writing corrected sound       2.1 min
Auto-Align Post (Resolve)     6.7 min   no prompts
Building aligned ISOs         56.5 min  2 files; ~24 min each — DRIVE-BOUND (card copy running onto the same drive); morning on idle drive: 3 min
Checking alignment            2.3 min
Polywav                       59.9 min  one 3.89 GB file — DRIVE-BOUND; morning: ~3 min
XML / transcript / report     <1 min    transcript ran alongside
Where it went: B CAM whole-file reads ≈ 2 h 45 min (fixed in 2.5.23); drive-bound writes ≈ 1 h 57 min (environment); everything else ≈ 20 min.
Fixes proposed = identical to the morning run (B CAM +22.92 s × 51; C CAM 28 by sound/clock; 65 files). Reproducible across two drives.
Alignment track (log): A CAM (2 cards) ch 2, 0.93. BUG: the "Sync to" panel and the SYNC REF badge show B CAM after the run — label points at the wrong lane.
The four "suspect" B CAM clips (C046–C049) read fine; nothing wrong with them.
Efficiencies: (1) 2.5.23 discard fix; (2) refpick reads each clip once per channel → one pass; (3) refpick + TC-check envelopes read the same clips twice → share the cache; (4) ISO/polywav writes: measure on an idle drive before touching; (5) overnight: turn off "Stop so you confirm the fixes" to avoid the wait; (6) fix the Sync-to label bug.
