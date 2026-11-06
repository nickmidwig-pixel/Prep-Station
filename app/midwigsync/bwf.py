"""Broadcast WAV read/write (RIFF + RF64), bext and iXML, streaming sample access."""
import os
import re
import struct
import xml.etree.ElementTree as ET
import numpy as np

FMT_PCM, FMT_FLOAT, FMT_EXT = 1, 3, 0xFFFE
RIFF_LIMIT = 0xFFFFFFFF


class WavInfo:
    def __init__(self, path):
        self.path = path
        self.chunks = []          # (id, offset_of_payload, size)
        self.format_tag = None
        self.channels = 0
        self.sr = 0
        self.bits = 0
        self.block_align = 0
        self.data_off = None
        self.data_size = 0
        self.bext_raw = None
        self.ixml_raw = None
        self.is_rf64 = False
        self.declared_data_size = None
        self._parse()

    # ---- parsing
    def _parse(self):
        with open(self.path, 'rb') as f:
            hdr = f.read(12)
            if len(hdr) < 12 or hdr[8:12] != b'WAVE' or hdr[:4] not in (b'RIFF', b'RF64', b'BW64'):
                raise ValueError('not a WAV file')
            self.is_rf64 = hdr[:4] != b'RIFF'
            ds64_data = None
            fsize = os.path.getsize(self.path)
            while True:
                h = f.read(8)
                if len(h) < 8:
                    break
                cid, sz = struct.unpack('<4sI', h)
                pos = f.tell()
                if cid == b'ds64':
                    d = f.read(sz)
                    ds64_data = struct.unpack('<QQQ', d[:24])[1]
                if cid == b'data':
                    if self.is_rf64 and sz == 0xFFFFFFFF and ds64_data is not None:
                        sz = ds64_data
                    self.declared_data_size = sz
                    sz = min(sz, fsize - pos)   # tolerate truncated / still-rendering files
                    self.data_off, self.data_size = pos, sz
                elif cid == b'fmt ':
                    d = f.read(min(sz, 40))
                    self.format_tag, self.channels, self.sr, _, self.block_align, self.bits = struct.unpack('<HHIIHH', d[:16])
                    if self.format_tag == FMT_EXT and len(d) >= 26:
                        self.format_tag = struct.unpack('<H', d[24:26])[0]
                elif cid == b'bext':
                    self.bext_raw = f.read(sz)
                elif cid == b'iXML':
                    self.ixml_raw = f.read(sz)
                self.chunks.append((cid, pos, sz))
                if pos + sz > fsize:
                    break
                f.seek(pos + sz + (sz & 1))
        if self.data_off is None or not self.channels:
            raise ValueError('WAV has no fmt/data')
        if not self.block_align:
            self.block_align = self.channels * self.bits // 8

    @property
    def nframes(self):
        return self.data_size // self.block_align

    @property
    def bytes_per_sample(self):
        return self.block_align // self.channels

    # ---- bext
    @property
    def time_reference(self):
        if not self.bext_raw or len(self.bext_raw) < 346:
            return None
        return struct.unpack('<Q', self.bext_raw[338:346])[0]

    @property
    def description(self):
        if not self.bext_raw:
            return ''
        return self.bext_raw[:256].split(b'\0')[0].decode('latin-1', 'replace')

    @property
    def coding_history(self):
        if not self.bext_raw or len(self.bext_raw) <= 602:
            return b''
        return self.bext_raw[602:].rstrip(b'\0')

    def desc_fields(self):
        out = {}
        for line in re.split(r'[\r\n]+', self.description):
            if '=' in line:
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip()
        return out

    def bext_field(self, name):
        if not self.bext_raw:
            return ''
        sl = {'originator': (256, 288), 'originator_ref': (288, 320), 'date': (320, 330), 'time': (330, 338)}[name]
        return self.bext_raw[sl[0]:sl[1]].split(b'\0')[0].decode('latin-1', 'replace')

    # ---- iXML
    def ixml(self):
        if not self.ixml_raw:
            return None
        txt = self.ixml_raw.split(b'\0')[0].decode('utf-8', 'replace')
        try:
            return ET.fromstring(txt.strip())
        except ET.ParseError:
            return None

    def ixml_text(self, *path):
        root = self.ixml()
        if root is None:
            return None
        el = root.find('/'.join(path))
        return el.text.strip() if el is not None and el.text else None

    def track_names(self):
        names = {}
        root = self.ixml()
        if root is not None:
            for t in root.iter('TRACK'):
                idx = t.findtext('CHANNEL_INDEX') or t.findtext('INTERLEAVE_INDEX')
                nm = t.findtext('NAME')
                if idx and nm:
                    try:
                        names[int(idx)] = nm.strip()
                    except ValueError:
                        pass
        if not names:
            for k, v in self.desc_fields().items():
                m = re.match(r'sTRK(\d+)$', k)
                if m and v:
                    names[int(m[1])] = v
        return names

    def tc_rate(self):
        r = self.ixml_text('SPEED', 'TIMECODE_RATE')
        flag = self.ixml_text('SPEED', 'TIMECODE_FLAG')
        if not r:
            d = self.desc_fields()
            r = d.get('dFRAMERATE') or d.get('sSPEED')
            if r:
                flag = 'DF' if re.search(r'(?<!N)D(F)?$', r.upper()) and 'ND' not in r.upper() else 'NDF'
                r = re.sub(r'[^0-9./]', '', r.split('-')[0]) or None
        return r, (flag or 'NDF').upper() == 'DF'

    # ---- samples
    def _decode(self, b):
        bps = self.bytes_per_sample
        n = len(b) // bps
        b = b[:n * bps]
        if self.format_tag == FMT_FLOAT and bps == 4:
            return np.frombuffer(b, '<f4').astype(np.float32)
        if self.format_tag == FMT_FLOAT and bps == 8:
            return np.frombuffer(b, '<f8').astype(np.float32)
        if bps == 2:
            return np.frombuffer(b, '<i2').astype(np.float32) / 32768.0
        if bps == 3:
            a = np.frombuffer(b, np.uint8).reshape(-1, 3)
            v = a[:, 0].astype(np.int32) | (a[:, 1].astype(np.int32) << 8) | (a[:, 2].astype(np.int32) << 16)
            v = np.where(v >= 1 << 23, v - (1 << 24), v)
            return v.astype(np.float32) / 8388608.0
        if bps == 4:
            return np.frombuffer(b, '<i4').astype(np.float32) / 2147483648.0
        if bps == 1:
            return (np.frombuffer(b, np.uint8).astype(np.float32) - 128) / 128.0
        raise ValueError(f'unsupported sample size {bps}')

    def read(self, start, count, channel=None, fh=None):
        """Float32 samples [start, start+count) in file sample frames; zero-padded outside the file.
        channel=None -> (count, channels) array, int -> (count,) array."""
        nch = self.channels
        out = np.zeros((max(count, 0), nch), np.float32)
        if count <= 0:
            return out[:, channel] if channel is not None else out
        a, b = max(start, 0), min(start + count, self.nframes)
        if b > a:
            own = fh is None
            f = fh or open(self.path, 'rb')
            try:
                f.seek(self.data_off + a * self.block_align)
                raw = f.read((b - a) * self.block_align)
            finally:
                if own:
                    f.close()
            x = self._decode(raw)
            got = len(x) // nch
            out[a - start:a - start + got] = x[:got * nch].reshape(-1, nch)
        return out[:, channel] if channel is not None else out

    def read_raw(self, start, count, fh):
        """Raw interleaved bytes for [start, start+count) — caller guarantees in range."""
        fh.seek(self.data_off + start * self.block_align)
        return fh.read(count * self.block_align)


# ---------------------------------------------------------------- writing
def _pad(b, n):
    if isinstance(b, str):
        b = b.encode('latin-1', 'replace')
    return b[:n].ljust(n, b'\0')


def build_bext(description=None, originator='Midwig Sync', originator_ref='', date='', time='',
               time_reference=None, coding_history=b'', base=None):
    """Build a bext chunk payload. With base (an existing raw bext), every field is kept except
    description / time_reference when those are given, and coding_history is appended."""
    if isinstance(coding_history, str):
        coding_history = coding_history.encode('latin-1', 'replace')
    if base and len(base) >= 602:
        be = bytearray(base[:602])
        if description is not None:
            be[0:256] = _pad(description, 256)
        if time_reference is not None:
            be[338:346] = struct.pack('<Q', int(time_reference))
        hist = bytes(base[602:]).rstrip(b'\0')
        if coding_history:
            hist += coding_history
        return bytes(be) + hist
    return (_pad(description or '', 256) + _pad(originator, 32) + _pad(originator_ref, 32) + _pad(date, 10) + _pad(time, 8)
            + struct.pack('<QH', int(time_reference or 0), 1) + b'\0' * 64 + b'\0' * 10 + b'\0' * 180 + coding_history)


def set_desc_field(desc, key, value):
    lines = [l for l in re.split(r'\r?\n', desc) if l.strip()]
    out, done = [], False
    for l in lines:
        if l.split('=', 1)[0].strip() == key:
            out.append(f'{key}={value}')
            done = True
        else:
            out.append(l)
    if not done:
        out.append(f'{key}={value}')
    return '\r\n'.join(out) + '\r\n'


def build_ixml(project='', fps_label='23.976', fps_frac='24000/1001', df=False, time_reference=0, sr=48000, bits=24,
               tracks=(), note='', tape='', scene='', take='', base_xml=None):
    hi, lo = int(time_reference) >> 32, int(time_reference) & 0xFFFFFFFF
    if base_xml is not None:
        # update an existing iXML tree's timestamp + track list, keep everything else
        root = base_xml
        sp = root.find('SPEED')
        if sp is None:
            sp = ET.SubElement(root, 'SPEED')
        for tag, val in (('TIMESTAMP_SAMPLE_RATE', sr), ('TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI', hi),
                         ('TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO', lo)):
            el = sp.find(tag)
            if el is None:
                el = ET.SubElement(sp, tag)
            el.text = str(val)
        if tracks:
            tl = root.find('TRACK_LIST')
            if tl is not None:
                root.remove(tl)
            root.append(_track_list(tracks))
        return b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root, encoding='utf-8')
    root = ET.Element('BWFXML')
    ET.SubElement(root, 'IXML_VERSION').text = '1.61'
    for tag, val in (('PROJECT', project), ('SCENE', scene), ('TAKE', take), ('TAPE', tape), ('NOTE', note)):
        if val:
            ET.SubElement(root, tag).text = str(val)
    sp = ET.SubElement(root, 'SPEED')
    for tag, val in (('MASTER_SPEED', fps_frac), ('CURRENT_SPEED', fps_frac), ('TIMECODE_RATE', fps_frac),
                     ('TIMECODE_FLAG', 'DF' if df else 'NDF'), ('FILE_SAMPLE_RATE', sr), ('AUDIO_BIT_DEPTH', bits),
                     ('DIGITIZER_SAMPLE_RATE', sr), ('TIMESTAMP_SAMPLE_RATE', sr),
                     ('TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI', hi), ('TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO', lo)):
        ET.SubElement(sp, tag).text = str(val)
    if tracks:
        root.append(_track_list(tracks))
    return b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root, encoding='utf-8')


def _track_list(tracks):
    tl = ET.Element('TRACK_LIST')
    ET.SubElement(tl, 'TRACK_COUNT').text = str(len(tracks))
    for i, nm in enumerate(tracks):
        t = ET.SubElement(tl, 'TRACK')
        ET.SubElement(t, 'CHANNEL_INDEX').text = str(i + 1)
        ET.SubElement(t, 'INTERLEAVE_INDEX').text = str(i + 1)
        ET.SubElement(t, 'NAME').text = nm
    return tl


def _chunk(cid, d):
    return cid + struct.pack('<I', len(d)) + d + (b'\0' if len(d) & 1 else b'')


def wav_header(nframes, channels, sr=48000, bits=24, fmt_tag=FMT_PCM, bext=None, ixml=None, extra_chunks=()):
    ba = channels * bits // 8
    body = _chunk(b'fmt ', struct.pack('<HHIIHH', fmt_tag, channels, sr, sr * ba, ba, bits))
    if bext is not None:
        body += _chunk(b'bext', bext)
    if ixml is not None:
        body += _chunk(b'iXML', ixml)
    for cid, d in extra_chunks:
        body += _chunk(cid, d)
    dsz = nframes * ba
    riff = 4 + len(body) + 8 + dsz + (dsz & 1)
    if riff > RIFF_LIMIT:
        raise ValueError('file would exceed the 4 GiB WAV limit')
    return b'RIFF' + struct.pack('<I', riff) + b'WAVE' + body + b'data' + struct.pack('<I', dsz)


def encode(x, bits=24, fmt_tag=FMT_PCM):
    """float32 (n,) or (n,ch) -> interleaved little-endian bytes."""
    x = np.asarray(x, np.float32)
    if fmt_tag == FMT_FLOAT:
        return x.astype('<f4').tobytes()
    if bits == 16:
        return np.clip(np.round(x * 32768.0), -32768, 32767).astype('<i2').tobytes()
    if bits == 24:
        v = np.clip(np.round(x.reshape(-1) * 8388608.0), -8388608, 8388607).astype(np.int32)
        return np.stack([v & 255, (v >> 8) & 255, (v >> 16) & 255], 1).astype(np.uint8).tobytes()
    if bits == 32:
        return np.clip(np.round(x.astype(np.float64) * 2147483648.0), -2147483648, 2147483647).astype('<i4').tobytes()
    raise ValueError(bits)


class WavWriter:
    """Write a WAV whose length is known up front. Writes to <path>.partial, renames on close()."""

    def __init__(self, path, nframes, channels, sr=48000, bits=24, fmt_tag=FMT_PCM, bext=None, ixml=None):
        self.path, self.n, self.ch, self.bits, self.fmt_tag = path, nframes, channels, bits, fmt_tag
        self.ba = channels * bits // 8
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        self.tmp = os.path.join(os.path.dirname(path), '.' + os.path.basename(path) + '.partial')
        self.f = open(self.tmp, 'wb')
        self.f.write(wav_header(nframes, channels, sr, bits, fmt_tag, bext, ixml))
        self.written = 0

    def write_float(self, x):
        self.write_raw(encode(x, self.bits, self.fmt_tag))

    def write_raw(self, b):
        n = len(b) // self.ba
        if self.written + n > self.n:
            b = b[:(self.n - self.written) * self.ba]
            n = self.n - self.written
        self.f.write(b)
        self.written += n

    def close(self):
        if self.written < self.n:   # pad with silence if a source came up short
            left = (self.n - self.written) * self.ba
            while left > 0:
                k = min(left, 1 << 24)
                self.f.write(b'\0' * k)
                left -= k
            self.written = self.n
        if (self.n * self.ba) & 1:
            self.f.write(b'\0')
        self.f.close()
        os.replace(self.tmp, self.path)

    def abort(self):
        try:
            self.f.close()
            os.remove(self.tmp)
        except OSError:
            pass


class StreamWavWriter:
    """WAV writer for unknown length: sizes are patched on close()."""

    def __init__(self, path, channels, sr=48000, bits=24, fmt_tag=FMT_PCM, bext=None, ixml=None):
        self.path, self.ch, self.bits, self.fmt_tag = path, channels, bits, fmt_tag
        self.ba = channels * bits // 8
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        self.tmp = os.path.join(os.path.dirname(path), '.' + os.path.basename(path) + '.partial')
        self.f = open(self.tmp, 'wb')
        h = wav_header(0, channels, sr, bits, fmt_tag, bext, ixml)
        self.hlen = len(h)
        self.bext_off = h.find(b'bext') + 8 if bext is not None else None
        self.f.write(h)
        self.n = 0

    def write_float(self, x):
        b = encode(x, self.bits, self.fmt_tag)
        self.f.write(b)
        self.n += len(b) // self.ba

    def close(self, time_reference=None):
        dsz = self.n * self.ba
        if dsz & 1:
            self.f.write(b'\0')
        self.f.seek(4)
        self.f.write(struct.pack('<I', self.hlen - 8 + dsz + (dsz & 1)))
        self.f.seek(self.hlen - 4)
        self.f.write(struct.pack('<I', dsz))
        if time_reference is not None and self.bext_off is not None:
            self.f.seek(self.bext_off + 338)
            self.f.write(struct.pack('<Q', int(time_reference)))
        self.f.close()
        os.replace(self.tmp, self.path)

    def abort(self):
        try:
            self.f.close()
            os.remove(self.tmp)
        except OSError:
            pass


def patch_time_reference(path, time_reference, tc_string=None):
    """Rewrite bext TimeReference (+ dTC in the description and the iXML timestamp) in place."""
    w = WavInfo(path)
    with open(path, 'r+b') as f:
        for cid, pos, sz in w.chunks:
            if cid == b'bext':
                f.seek(pos + 338)
                f.write(struct.pack('<Q', int(time_reference)))
                if tc_string:
                    desc = set_desc_field(w.description, 'dTC', tc_string)
                    f.seek(pos)
                    f.write(_pad(desc, 256))
            elif cid == b'iXML':
                f.seek(pos)
                raw = f.read(sz)
                hi, lo = int(time_reference) >> 32, int(time_reference) & 0xFFFFFFFF
                new = re.sub(rb'(<TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI>)\d*(</)', rb'\g<1>%d\g<2>' % hi, raw)
                new = re.sub(rb'(<TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO>)\d*(</)', rb'\g<1>%d\g<2>' % lo, new)
                if len(new) <= sz:
                    f.seek(pos)
                    f.write(new.ljust(sz, b' ') if len(new) < sz else new)
