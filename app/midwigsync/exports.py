"""Hand-off files for editorial and audio post, written next to the XML:

  <job>.otio      OpenTimelineIO: DaVinci Resolve (File › Import › Timeline), Premiere (beta), Avid (preview)
  <job>_AUDIO.aaf AAF with the sound tracks linked to the WAVs: Pro Tools / audio post, Avid
  <job>.ale       Avid Log Exchange: every clip with its (corrected) timecode, roll, scene/take, tracks
  <job>_CLIPS.csv the same as a spreadsheet: what each file is, where it sits, what changed
  READ ME.txt     what's in the output folder and which file to open in which app
  SYNCED_CLIPS/   (optional) camera clips re-wrapped with the aligned sound inside; picture is copied, not re-encoded
"""
import csv
import datetime as dt
import os
import subprocess
from fractions import Fraction

import numpy as np

from . import tc as TC
from .bwf import WavInfo, WavWriter
from .brand import APP_NAME, APP_VERSION


def _otio():
    import opentimelineio as otio
    return otio


def _uri(p):
    import pathlib
    return pathlib.Path(os.path.abspath(p)).as_uri()


# ------------------------------------------------------------------ OTIO
def write_otio(path, name, fps, cams, tracks):
    """cams: [dict(label, clips)], tracks: [dict(label, clips)] as built for the FCP7 XML (audio tracks per channel;
    only channel 1 of each file is used, OTIO has no channel routing)."""
    otio = _otio()
    RT, TR = otio.opentime.RationalTime, otio.opentime.TimeRange
    rate = float(fps)
    allc = [c for t in cams + tracks for c in t['clips']]
    if not allc:
        return None
    hr = TC.nominal(fps) * 3600
    seq0 = TC.samples_to_frames(min(c['start'] for c in allc), fps, 'floor') // hr * hr
    tl = otio.schema.Timeline(name=name, global_start_time=RT(seq0, rate))

    def lane_split(clips):
        lanes = []
        for c in sorted(clips, key=lambda c: c['start']):
            for lane in lanes:
                if lane[-1]['start'] + lane[-1]['length'] <= c['start']:
                    lane.append(c)
                    break
            else:
                lanes.append([c])
        return lanes

    def add(label, clips, kind):
        for i, lane in enumerate(lane_split(clips)):
            tr = otio.schema.Track(name=label if i == 0 else f'{label} ({i + 1})', kind=kind)
            pos = seq0
            for c in lane:
                cf = c.get('fps') or fps
                crate = float(cf)
                st = TC.samples_to_frames(c['start'], fps)
                if st > pos:
                    tr.append(otio.schema.Gap(source_range=TR(RT(0, rate), RT(st - pos, rate))))
                dur_seq = max(1, TC.samples_to_frames(c['length'], fps, 'ceil'))
                dur_src = max(1, TC.samples_to_frames(c['length'], cf, 'ceil'))
                ms = c.get('media_start_frames', TC.samples_to_frames(c['start'], cf)) % (TC.nominal(cf) * 86400)
                ref = otio.schema.ExternalReference(target_url=_uri(c['path']),
                                                    available_range=TR(RT(ms, crate), RT(dur_src, crate)))
                tr.append(otio.schema.Clip(name=os.path.basename(c['path']), media_reference=ref,
                                           source_range=TR(RT(ms, crate), RT(dur_src, crate)),
                                           metadata={APP_NAME: dict(roll=c.get('reel', ''))}))
                pos = st + dur_seq
            tl.tracks.append(tr)
    for cam in cams:
        add(cam['label'], cam['clips'], otio.schema.TrackKind.Video)
    for cam in cams:
        sc = [c for c in cam['clips'] if c.get('channels')]
        if sc:
            add(f"{cam['label']} scratch", sc, otio.schema.TrackKind.Audio)
    for t in tracks:
        cl = [c for c in t['clips'] if c.get('src_track', 1) == 1]
        if cl:
            add(t['label'].rsplit('_', 1)[0] if t['label'].endswith('_1') else t['label'], cl, otio.schema.TrackKind.Audio)
    otio.adapters.write_to_file(tl, path, adapter_name='otio_json')
    return path


# ------------------------------------------------------------------ AAF (sound only)
def _wav_meta(path):
    w = WavInfo(path)
    fmt = {16: 's16', 24: 's32', 32: 'flt' if w.format_tag == 3 else 's32'}.get(w.bits, 's32')
    return dict(format=dict(format_name='wav', format_long_name='WAV / WAVE (Waveform Audio)',
                            duration=str(w.nframes / w.sr), bit_rate=str(w.sr * w.channels * w.bits)),
                streams=[dict(codec_type='audio', codec_name=f'pcm_s{w.bits}le' if w.format_tag != 3 else 'pcm_f32le',
                              sample_rate=str(w.sr), channels=w.channels, duration_ts=w.nframes,
                              bit_rate=str(w.sr * w.channels * w.bits), sample_fmt=fmt,
                              bits_per_sample=w.bits)])


def write_aaf(path, name, fps, df, tracks):
    """One composition: a timecode track plus one sound track per recorder channel, each clip linked (not embedded)
    to its WAV, placed sample-accurately at its timecode."""
    import aaf2
    allc = [c for t in tracks for c in t['clips']]
    if not allc:
        return None
    hr = TC.nominal(fps) * 3600
    seq0_frames = TC.samples_to_frames(min(c['start'] for c in allc), fps, 'floor') // hr * hr
    seq0 = TC.frames_to_samples(seq0_frames, fps)
    end = max(c['start'] + c['length'] for c in allc)
    vr = f'{Fraction(fps).numerator}/{Fraction(fps).denominator}'
    tmp = path + '.partial'
    if os.path.exists(tmp):
        os.remove(tmp)
    with aaf2.open(tmp, 'w') as f:
        masters = {}
        comp = f.create.CompositionMob()
        comp.name = name
        f.content.mobs.append(comp)
        tc_slot = comp.create_timeline_slot(vr)
        tcomp = f.create.Timecode(TC.nominal(fps), bool(df))
        tcomp.start = seq0_frames
        tcomp.length = TC.samples_to_frames(end - seq0, fps, 'ceil')
        tc_slot.segment = tcomp
        for t in tracks:
            slot = comp.create_sound_slot(edit_rate=48000)
            slot.name = t['label']
            seq = slot.segment
            pos = seq0
            for c in sorted(t['clips'], key=lambda c: c['start']):
                key = os.path.realpath(c['path'])
                if key not in masters:
                    masters[key] = f.content.create_ama_link(c['path'], _wav_meta(c['path']))[0]
                mm = masters[key]
                start = int(c['start'])
                if start < pos:                    # overlap on one track: trim the head
                    skip = pos - start
                else:
                    skip = 0
                    if start > pos:
                        seq.components.append(f.create.Filler('sound', start - pos))
                n = int(c['length']) - skip
                if n <= 0:
                    continue
                slot_id = mm.slots[min(c.get('src_track', 1), len(mm.slots)) - 1].slot_id
                sc = mm.create_source_clip(slot_id=slot_id, start=skip, length=n, media_kind='sound')
                seq.components.append(sc)
                pos = start + skip + n
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------ ALE + CSV
ALE_COLS = ['Name', 'Tape', 'Start', 'End', 'Duration', 'Tracks', 'FPS', 'Source File', 'Camera', 'Scene', 'Take',
            'Sound TC', 'Auxiliary TC1', 'Day', 'Comments']


def _clip_rows(project, aligned_by_source=None):
    fps, df = project.fps, project.df
    aligned_by_source = aligned_by_source or {}
    rows = []
    for c in sorted(project.clips, key=lambda c: (c['kind'] != 'video', c['roll'], c['start'] or 0, c['name'])):
        if not c['include'] or c['start'] is None:
            continue
        cf = c.get('fps') or fps
        start, end = c['start'], c['start'] + c['length']
        nch = c['channels'] if c['kind'] == 'audio' else sum(s['channels'] for s in c.get('audio_streams') or [])
        tracks = ('V' if c['kind'] == 'video' else '') + ''.join(f'A{i + 1}' for i in range(nch))
        notes = []
        if c['orig_start'] is not None and c['start'] != c['orig_start']:
            notes.append(f"timecode moved {(c['start'] - c['orig_start']) / TC.SR:+.3f} s from {TC.samples_to_tc(c['orig_start'], cf, df)}")
        elif c['orig_start'] is None:
            notes.append('no timecode in the file, placed by ' + ('hand' if c.get('manual') else 'waveform'))
        if c.get('ltc'):
            notes.append(f"timecode from LTC on ch {c['ltc']['channel'] + 1}")
        if c.get('converted'):
            notes.append(f"converted {c['converted']}")
        out = aligned_by_source.get(c.get('stamped_path') or c['path']) or aligned_by_source.get(c['path'])
        rows.append(dict(
            Name=os.path.splitext(c['name'])[0], Tape=c['roll'], Start=TC.samples_to_tc(start, cf, df and cf == fps),
            End=TC.samples_to_tc(end, cf, df and cf == fps), Duration=TC.samples_to_tc(c['length'], cf, df and cf == fps),
            Tracks=tracks, FPS=TC.rate_label(cf), **{'Source File': c.get('source_path') or c['path']},
            Camera=c.get('camera', ''), Scene=c.get('scene', ''), Take=c.get('take', ''),
            **{'Sound TC': TC.samples_to_tc(start, fps, df) if c['kind'] == 'audio' else '',
               'Auxiliary TC1': TC.samples_to_tc(c['orig_start'], cf, df) if c['orig_start'] not in (None, c['start']) else '',
               'Day': c.get('day', ''), 'Comments': '; '.join(notes)},
            kind=c['kind'], output=out or c.get('stamped_path') or '', flags=' '.join(c['flags'])))
    return rows


def write_ale(path, project, aligned_by_source=None):
    rows = _clip_rows(project, aligned_by_source)
    fps = project.fps
    lines = ['Heading', 'FIELD_DELIM\tTABS', 'VIDEO_FORMAT\t1080', 'AUDIO_FORMAT\t48khz', f'FPS\t{TC.rate_label(fps)}', '',
             'Column', '\t'.join(ALE_COLS), '', 'Data']
    for r in rows:
        lines.append('\t'.join(str(r.get(k, '')).replace('\t', ' ') for k in ALE_COLS))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return path


def write_csv(path, project, aligned_by_source=None):
    rows = _clip_rows(project, aligned_by_source)
    cols = ['kind', 'Tape', 'Name', 'Start', 'End', 'Duration', 'FPS', 'Tracks', 'Camera', 'Scene', 'Take', 'Day',
            'Comments', 'flags', 'Source File', 'output']
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


# ------------------------------------------------------------------ READ ME
def write_readme(path, job, mode, files):
    """files: list of (relative path, what it is)."""
    L = [f'{job}', f'Made by {APP_NAME} {APP_VERSION} on {dt.datetime.now().strftime("%Y-%m-%d %H:%M")}', '',
         'Mode: ' + ('audio only (reference mic chosen per stretch)' if mode == 'audio' else 'synced to camera audio'), '',
         'WHAT TO OPEN', '',
         '  Premiere Pro ......... TIMELINES/*_SYNC_*.xml  (File › Import)',
         '  DaVinci Resolve ...... TIMELINES/*_SYNC_*.xml or *.otio  (File › Import › Timeline)',
         '  Avid Media Composer .. link the camera media + ISO_ALIGNED WAVs, then merge TIMELINES/*.ale',
         '  Pro Tools / audio post TIMELINES/*_AUDIO.aaf  (links to the aligned WAVs; keep the folder structure)',
         '', 'FOLDERS', '',
         '  ISO_ALIGNED/     recorder files after Auto-Align Post: same start timecode, length, track names and',
         '                   metadata as the originals. Mix and timecode tracks are untouched.',
         '  POLY_ALIGNED/    the aligned tracks as polyphonic WAVs (whole timecode minutes, under 4 GB each)',
         '  TC_FIXED/        copies of files whose timecode was wrong or missing, with the corrected timecode',
         '  CONVERTED_48K/   copies of non-48 kHz or non-WAV audio, converted, timecode kept',
         '  REF_<camera>/    the camera channel everything was aligned to',
         '  TIMELINES/       XML, OTIO, AAF, ALE and a clip list (CSV)',
         '  SYNCED_CLIPS/    camera clips with the aligned sound inside (if that option was on)',
         '  WORK FILES (not for the edit)/  report, corrected-TC sound copies, plans, caches',
         '', 'Originals were not changed.', '', 'FILES IN THIS DELIVERY', '']
    for rel, what in files:
        L.append(f'  {rel}  —  {what}')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')
    return path


# ------------------------------------------------------------------ synced camera clips
def synced_clips(out_dir, cams, audio_tracks, fps, df, ffmpeg, log=print, progress=None, cancel=None):
    """For every camera clip: a .mov with the camera's picture and sound (copied as they are) plus one mono track per
    recorder channel, cut from the aligned files to the clip's exact range, with the clip's (corrected) timecode."""
    progress = progress or (lambda *a: None)
    made = []
    clips = [c for cam in cams for c in cam['clips']]
    for i, c in enumerate(clips):
        if cancel and cancel():
            raise KeyboardInterrupt
        progress(i, len(clips), f"Synced clip: {os.path.basename(c['path'])}")
        if c.get('raw'):
            log(f"  {os.path.basename(c['path'])}: RAW, can't be re-wrapped (skipped)")
            continue
        a, n = int(c['start']), int(c['length'])
        chans, names = [], []
        for t in audio_tracks:
            buf = np.zeros(n, np.float32)
            hit = False
            for s in t['clips']:
                lo, hi = max(a, s['start']), min(a + n, s['start'] + s['length'])
                if hi - lo <= 0:
                    continue
                w = WavInfo(s['path'])
                if w.sr != TC.SR:
                    continue
                buf[lo - a:hi - a] = w.read(lo - s['start'], hi - lo, s.get('src_track', 1) - 1)
                hit = True
            if hit:
                chans.append(buf)
                names.append(t['label'])
        if not chans:
            continue
        wav = os.path.join(out_dir, '.tmp_sync.wav')
        os.makedirs(out_dir, exist_ok=True)
        ww = WavWriter(wav, n, len(chans), TC.SR, 24)
        ww.write_float(np.stack(chans, 1))
        ww.close()
        cf = c.get('fps') or fps
        tcs = TC.frames_to_tc(TC.samples_to_frames(a, cf) % (TC.nominal(cf) * 86400), cf, df and cf == fps)
        dst = os.path.join(out_dir, c.get('reel') or 'CAM', os.path.splitext(os.path.basename(c['path']))[0] + '.mov')
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        # one mono track per mic, so editors see each recorder channel on its own
        fc = ';'.join(f'[1:a:0]pan=mono|c0=c{k}[s{k}]' for k in range(len(chans)))
        cmd = [ffmpeg, '-y', '-v', 'error', '-i', c['path'], '-i', wav, '-filter_complex', fc, '-map', '0:v:0', '-map', '0:a?']
        for k in range(len(chans)):
            cmd += ['-map', f'[s{k}]']
        cmd += ['-c:v', 'copy', '-c:a', 'pcm_s24le', '-timecode', tcs, '-metadata', f"reel_name={c.get('reel', '')}"]
        n_cam = c.get('channels_streams', 0)
        for k, nm in enumerate(names):
            cmd += [f'-metadata:s:a:{n_cam + k}', f'title={nm}']
        cmd += [dst + '.partial.mov']
        r = subprocess.run(cmd, capture_output=True, text=True)
        os.remove(wav)
        if r.returncode != 0:
            log(f"  {os.path.basename(c['path'])}: couldn't re-wrap ({r.stderr.strip()[-200:]})")
            try:
                os.remove(dst + '.partial.mov')
            except OSError:
                pass
            continue
        os.replace(dst + '.partial.mov', dst)
        made.append(dst)
    return made
