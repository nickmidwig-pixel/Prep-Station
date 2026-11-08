"""FCP7 XML (xmeml) for Premiere Pro and DaVinci Resolve: one synced timeline for the day.
Video track per camera roll, camera scratch audio, then one audio track per ISO (or per polywav channel)."""
import os
import urllib.parse
import uuid
from xml.sax.saxutils import escape

from . import tc as TC


def pathurl(p):
    return 'file://localhost' + urllib.parse.quote(os.path.abspath(p))


def _rate(fps):
    return f'<rate><timebase>{TC.nominal(fps)}</timebase><ntsc>{"TRUE" if TC.is_ntsc(fps) else "FALSE"}</ntsc></rate>'


def _tc(fps, frames, df, reel=None):
    frames = int(frames) % (TC.nominal(fps) * 86400)       # after-midnight clips wrap like the real timecode
    s = TC.frames_to_tc(frames, fps, df)
    reel_x = f'<reel><name>{escape(reel)}</name></reel>' if reel else ''
    return (f'<timecode>{_rate(fps)}<string>{s}</string><frame>{frames}</frame>'
            f'<displayformat>{"DF" if df else "NDF"}</displayformat>{reel_x}</timecode>')


class XmlBuilder:
    def __init__(self, name, fps, df=False):
        self.name, self.fps, self.df = name, fps, df
        self.spf = TC.samples_per_frame(fps)
        self.files = {}
        self.vtracks = []      # list of (label, [clips])
        self.atracks = []
        self.n_clip = 0
        self.links = {}

    def _file(self, path, frames, start_frames, kind, channels, width=0, height=0, reel=None, fps=None):
        fps = fps or self.fps
        key = os.path.realpath(path)
        if key in self.files:
            return f'<file id="{self.files[key]}"/>'
        fid = f'file-{len(self.files) + 1}'
        self.files[key] = fid
        vid = (f'<video><samplecharacteristics>{_rate(fps)}<width>{width}</width><height>{height}</height>'
               f'<anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio>'
               f'<fielddominance>none</fielddominance></samplecharacteristics></video>') if kind == 'video' else ''
        aud = (f'<audio><samplecharacteristics><depth>24</depth><samplerate>48000</samplerate></samplecharacteristics>'
               f'<channelcount>{max(1, channels)}</channelcount></audio>') if channels else ''
        return (f'<file id="{fid}"><name>{escape(os.path.basename(path))}</name><pathurl>{pathurl(path)}</pathurl>'
                f'{_rate(fps)}<duration>{frames}</duration>{_tc(fps, start_frames, self.df and fps == self.fps, reel)}'
                f'<media>{vid}{aud}</media></file>')

    def frames(self, samples, mode='round'):
        return TC.samples_to_frames(samples, self.fps, mode)

    def add_clip(self, tracks, track_label, clip):
        """clip: dict(path, start(abs samples), length(samples), kind, channels, src_track, width, height,
        media_start_frames, reel, link_key)"""
        for lab, lst in tracks:
            if lab == track_label:
                lst.append(clip)
                return
        tracks.append((track_label, [clip]))

    def video(self, label, clip):
        self.add_clip(self.vtracks, label, clip)

    def audio(self, label, clip):
        self.add_clip(self.atracks, label, clip)

    def _place(self, tracks):
        """Split a track's clips onto extra tracks if any overlap."""
        out = []
        for lab, clips in tracks:
            lanes = []
            for c in sorted(clips, key=lambda c: c['start']):
                for lane in lanes:
                    if lane[-1]['start'] + lane[-1]['length'] <= c['start']:
                        lane.append(c)
                        break
                else:
                    lanes.append([c])
            for i, lane in enumerate(lanes):
                out.append((lab if i == 0 else f'{lab} ({i + 1})', lane))
        return out

    def build(self, seq_start_samples=None):
        allc = [c for _, l in self.vtracks + self.atracks for c in l]
        if not allc:
            raise ValueError('nothing to export')
        first = min(c['start'] for c in allc)
        if seq_start_samples is None:
            hr = TC.nominal(self.fps) * 3600
            seq_start_frames = (self.frames(first, 'floor') // hr) * hr
        else:
            seq_start_frames = self.frames(seq_start_samples, 'floor')
        seq0 = seq_start_frames
        end = max(self.frames(c['start'] + c['length'], 'ceil') for c in allc) - seq0
        parts = []

        def clipitem(c, mediatype, idx):
            self.n_clip += 1
            cid = f'clipitem-{self.n_clip}'
            st = self.frames(c['start']) - seq0
            dur = max(1, self.frames(c['length'], 'ceil'))              # sequence frames
            cfps = c.get('fps') or self.fps                               # the clip's own rate (mixed-rate days)
            sdur = dur if cfps == self.fps else max(1, TC.samples_to_frames(c['length'], cfps, 'ceil'))
            if c.get('link_key'):
                self.links.setdefault(c['link_key'], []).append((cid, mediatype, c.get('track_no', 1), idx))
            f = self._file(c['path'], sdur, c.get('media_start_frames', self.frames(c['start'])), c['kind'],
                           c.get('channels', 0), c.get('width', 0), c.get('height', 0), c.get('reel'), cfps)
            src = ''
            if mediatype == 'audio':
                src = f'<sourcetrack><mediatype>audio</mediatype><trackindex>{c.get("src_track", 1)}</trackindex></sourcetrack>'
            mk = ''
            if c.get('note') and mediatype == 'video':
                mk = (f'<marker><name>{escape(c.get("note_name", "Note"))}</name><comment>{escape(c["note"])}</comment>'
                      f'<in>0</in><out>-1</out></marker>')
            return cid, (f'<clipitem id="{cid}"><name>{escape(os.path.basename(c["path"]))}</name><enabled>TRUE</enabled>'
                         f'<duration>{sdur}</duration>{_rate(cfps)}<start>{st}</start><end>{st + dur}</end>'
                         f'<in>0</in><out>{sdur}</out>{f}{src}{mk}@@LINK:{c.get("link_key", "")}@@</clipitem>')

        vxml = []
        for i, (lab, clips) in enumerate(self._place(self.vtracks)):
            for c in clips:
                c['track_no'] = i + 1
            items = ''.join(clipitem(c, 'video', k + 1)[1] for k, c in enumerate(clips))
            vxml.append(f'<track><name>{escape(lab)}</name>{items}<enabled>TRUE</enabled><locked>FALSE</locked></track>')
        axml = []
        for i, (lab, clips) in enumerate(self._place(self.atracks)):
            for c in clips:
                c['track_no'] = i + 1
            items = ''.join(clipitem(c, 'audio', k + 1)[1] for k, c in enumerate(clips))
            axml.append(f'<track><name>{escape(lab)}</name>{items}<enabled>TRUE</enabled><locked>FALSE</locked>'
                        f'<outputchannelindex>{1 + i % 2}</outputchannelindex></track>')
        w = max([c.get('width', 0) for _, l in self.vtracks for c in l] + [1920])
        h = max([c.get('height', 0) for _, l in self.vtracks for c in l] + [1080])
        body = (f'<media><video><format><samplecharacteristics>{_rate(self.fps)}<width>{w}</width><height>{h}</height>'
                f'<anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio>'
                f'<fielddominance>none</fielddominance></samplecharacteristics></format>{"".join(vxml)}</video>'
                f'<audio><numOutputChannels>2</numOutputChannels><format><samplecharacteristics><depth>24</depth>'
                f'<samplerate>48000</samplerate></samplecharacteristics></format>{"".join(axml)}</audio></media>')
        # resolve link placeholders
        import re

        def link_repl(m):
            key = m.group(1)
            if not key or len(self.links.get(key, [])) < 2:
                return ''
            return ''.join(f'<link><linkclipref>{cid}</linkclipref><mediatype>{mt}</mediatype>'
                           f'<trackindex>{tn}</trackindex><clipindex>{ci}</clipindex></link>'
                           for cid, mt, tn, ci in self.links[key])
        body = re.sub(r'@@LINK:([^@]*)@@', link_repl, body)
        parts.append('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="5">')
        parts.append(f'<sequence id="sequence-1"><uuid>{uuid.uuid4()}</uuid><name>{escape(self.name)}</name>'
                     f'<duration>{end}</duration>{_rate(self.fps)}{_tc(self.fps, seq0, self.df)}{body}</sequence>')
        parts.append('</xmeml>\n')
        return '\n'.join(parts)


def write_day_xml(path, name, fps, df, cameras, audio_tracks):
    """cameras: [dict(label, clips=[clip...])]; audio_tracks: [dict(label, clips=[clip...])] (already ordered)."""
    xb = XmlBuilder(name, fps, df)
    for cam in cameras:
        for c in cam['clips']:
            xb.video(cam['label'], dict(c, link_key=c['path']))
    for cam in cameras:
        for c in cam['clips']:
            if c.get('channels'):
                xb.audio(f"{cam['label']} ch1", dict(c, src_track=1, link_key=c['path']))
    for t in audio_tracks:
        for c in t['clips']:
            xb.audio(t['label'], c)
    xml = xb.build()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(xml)
    return path


def write_slowmo_xml(path, name, fps, df, clips):
    """Slow motion and other frame rates, in the order they were shot, back to back from 01:00:00:00, one track
    per camera, picture only, each clip at its own rate (100%) with a marker saying what it is. Interpret / conform
    them to slow them down; nothing here is synced to sound."""
    xb = XmlBuilder(name, fps, df)
    spf = TC.samples_per_frame(fps)
    hour = TC.frames_to_samples(TC.nominal(fps) * 3600, fps)
    gap = int(round(float(spf) * TC.nominal(fps)))           # one second between clips
    pos = hour

    def order(c):
        return (c.get('orig_start') is None, c.get('day') or '', c.get('orig_start') or 0, str(c.get('created') or ''), c['name'])
    for c in sorted(clips, key=order):
        cfps = c.get('fps') or fps
        sm = c.get('slowmo') or {}
        label = c.get('lane_label') or c['roll']
        what = {'timebase': 'SLOW MO (time base)', 'in_camera': 'SLOW MO (in camera)', 'likely': 'SLOW MO?',
                'rate': 'OTHER RATE', 'marked': 'SLOW MO'}.get(sm.get('kind'), 'SLOW MO')
        tc = TC.samples_to_tc(c['orig_start'], cfps, bool(c.get('df'))) if c.get('orig_start') is not None else 'no TC'
        xb.video(label, dict(path=c['path'], start=pos, length=c['length'], kind='video', channels=0,
                             width=c.get('width', 0), height=c.get('height', 0), reel=c['roll'], fps=cfps,
                             media_start_frames=TC.samples_to_frames(c['orig_start'], cfps) if c.get('orig_start') is not None else 0,
                             note_name=what, note=f"{c['roll']} {c['name']} at {tc}. {sm.get('why', '')}"))
        n = TC.samples_to_frames(c['length'], fps, 'ceil')
        pos += TC.frames_to_samples(n, fps) + gap
    xml = xb.build(seq_start_samples=hour)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(xml)
    return path
