"""Build small stand-ins for Canon Cinema RAW Light clips: QuickTime files whose timecode lives only in a tmcd
track ffmpeg can't index (as on real .CRM), with an unknown picture codec and Canon's uuid box (model + body
serial) inside moov. Layout copied from the real C500 Mark II / C80 files of 2026-11-06."""
import os, struct, subprocess, sys

CANON_UUID = bytes.fromhex('85c0b687820f11e08111f4ce462b6a48')


def atoms(buf, a, b):
    pos = a
    while pos + 8 <= b:
        size, typ = struct.unpack('>I4s', buf[pos:pos + 8])
        hl = 8
        if size == 1:
            size, hl = struct.unpack('>Q', buf[pos + 8:pos + 16])[0], 16
        yield typ, pos, pos + hl, pos + size
        pos += size


def box(typ, payload):
    return struct.pack('>I4s', 8 + len(payload), typ) + payload


def canonize(src, dst, model, serial, clip, fourcc=b'CRAW'):
    buf = bytearray(open(src, 'rb').read())
    top = list(atoms(buf, 0, len(buf)))
    assert top[-1][0] == b'moov', 'moov must be last'
    _, mpos, mpl, mend = top[-1]
    for t, _, pl, end in atoms(buf, mpl, mend):
        if t != b'trak':
            continue
        stack, handler, tables = [(pl, end)], None, {}
        while stack:
            x, y = stack.pop()
            for t3, _, pl3, end3 in atoms(buf, x, y):
                if t3 in (b'mdia', b'minf', b'stbl'):
                    stack.append((pl3, end3))
                elif t3 == b'hdlr' and handler is None:
                    handler = bytes(buf[pl3 + 8:pl3 + 12])
                else:
                    tables[t3] = pl3
        if handler == b'tmcd':                      # no sample table entries: ffmpeg can't index the sample
            buf[tables[b'stts'] + 4:tables[b'stts'] + 8] = b'\0\0\0\0'
            buf[tables[b'stsz'] + 8:tables[b'stsz'] + 12] = b'\0\0\0\0'
        elif handler == b'vide':                    # a picture codec nobody here can decode
            buf[tables[b'stsd'] + 12:tables[b'stsd'] + 16] = fourcc
    inner = (box(b'CNCV', b'CanonCRM0001\0') + box(b'CNMN', model.encode() + b'\0')
             + box(b'CNFV', b'Firmware Ver.01.00.00\0') + box(b'CNSN', serial.encode() + b'\0')
             + box(b'CNDT', b'2026-11-06T10:49:02-05:00\0') + box(b'CNCN', clip.encode() + b'\0')
             + struct.pack('>IIII', 0, 1, 2, 3))
    u = box(b'uuid', CANON_UUID + inner)
    buf[mpos:mpos + 4] = struct.pack('>I', mend - mpos + len(u))
    buf += u
    open(dst, 'wb').write(bytes(buf))


def make(out_dir, name, tc, model, serial, seconds=4, cam_audio='scratch34'):
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, '.' + name + '.mov')
    # 4 channels like the C500 Mark II: ch1-2 silent, ch3-4 the onboard mic
    filt = ('[1:a]asplit=2[s3][s4];anullsrc=r=48000:cl=mono[z1];anullsrc=r=48000:cl=mono[z2];'
            '[z1][z2][s3][s4]amerge=inputs=4,atrim=0:%d[a]' % seconds)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc=size=64x36:rate=24000/1001:duration={seconds}',
                    '-f', 'lavfi', '-i', f'anoisesrc=r=48000:a=0.05:d={seconds}', '-filter_complex', filt,
                    '-map', '0:v', '-map', '[a]', '-c:v', 'mpeg4', '-c:a', 'pcm_s24le', '-timecode', tc,
                    '-f', 'mov', tmp], check=True)
    dst = os.path.join(out_dir, name + '.CRM')
    canonize(tmp, dst, model, serial, name)
    os.remove(tmp)
    return dst


def build(root='/home/claude/canonday', plain='/home/claude/canonday_plain'):
    """Two cards per body, named like the real day (letter in the card folder), and the same clips again in
    folders with no letter at all (Card1..Card4), where only the body serial can group them."""
    c500, c80 = 'UDP_261106_C500_A_00%d', 'UDP_261106_C80_B_00%d'
    spec = [(c500 % 1, 'Card1', 'A001C001_261106AB_CANON', '10:49:02:08', 'Canon EOS C500 Mark II', '582022100067'),
            (c500 % 2, 'Card2', 'A002C001_261106AB_CANON', '11:20:00:00', 'Canon EOS C500 Mark II', '582022100067'),
            (c80 % 1, 'Card3', 'C_0001C001X261106_CANON', '10:49:13:18', 'Canon EOS C80', '012919000260'),
            (c80 % 2, 'Card4', 'C_0002C002X261106_CANON', '10:56:37:05', 'Canon EOS C80', '012919000260')]
    out = []
    for folder, card, name, tc, model, serial in spec:
        for r, f in ((root, folder), (plain, card)):
            d = os.path.join(r, f, 'CONTENTS', 'CLIPS001')
            if not os.path.exists(os.path.join(d, name + '.CRM')):
                out.append(make(d, name, tc, model, serial))
    return out


if __name__ == '__main__':
    print('\n'.join(build()))
