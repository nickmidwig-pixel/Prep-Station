"""Measure boom-vs-camera offset across a finished day from the files themselves (not the app's own numbers).
Run on the Mac inside <day>_SYNCED:  python3 measure_drift.py [POLY_FOLDER] [MOUNT_PREFIX]
For each reference-camera clip it reads 8 s at 8%, 50% and 92% of the clip from the camera's reference channel and
from the BOOM/BOOM1 track of whichever polywav covers that moment, and reports the lag (ms) and correlation.
0.0 ms everywhere, with no trend across the day, means the aligned sound is locked to the picture."""
import glob, json, os, re, struct, subprocess, sys
import numpy as np

SR = 48000
POLY = sys.argv[1] if len(sys.argv) > 1 else 'POLY_ALIGNED'
MOUNT = sys.argv[2] if len(sys.argv) > 2 else ''          # e.g. "/Volumes/X=/home/me/mnt/X" when run in a VM


def wav_hdr(p):
    with open(p, 'rb') as f:
        pos = 12; ch = bits = None; tr = 0; ds = None; names = []
        while True:
            f.seek(pos); hd = f.read(8)
            if len(hd) < 8: break
            cid, sz = struct.unpack('<4sI', hd)
            if cid == b'ds64': d = f.read(sz); ds = struct.unpack('<Q', d[8:16])[0]
            if cid == b'fmt ': d = f.read(sz); ch = struct.unpack('<H', d[2:4])[0]; bits = struct.unpack('<H', d[14:16])[0]
            if cid == b'bext': b = f.read(sz); tr = struct.unpack('<Q', b[338:346])[0]
            if cid == b'iXML': names = re.findall(r'<NAME>([^<]*)</NAME>', f.read(sz).decode('utf-8', 'replace'))
            if cid == b'data': return dict(ch=ch, bits=bits, tr=tr, off=pos + 8, n=(ds or sz) // (ch * bits // 8), names=names)
            pos += 8 + sz + (sz & 1)


def wav_read(p, h, ch, start, n):
    bps = h['bits'] // 8; fr = h['ch'] * bps
    with open(p, 'rb') as f:
        f.seek(h['off'] + start * fr); raw = f.read(n * fr)
    a = np.frombuffer(raw, dtype=np.uint8).reshape(-1, fr)[:, ch * bps:(ch + 1) * bps]
    if bps == 3:
        v = a[:, 0].astype(np.int32) | (a[:, 1].astype(np.int32) << 8) | (a[:, 2].astype(np.int32) << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        return v.astype(np.float32) / (1 << 23)
    return a.view('<i2')[:, 0].astype(np.float32) / 32768


def cam_read(p, ch, start, n):
    cmd = ['ffmpeg', '-v', 'error', '-ss', f'{start / SR:.6f}', '-i', p, '-map', f'0:a:{ch}', '-t', f'{n / SR:.6f}',
           '-f', 'f32le', '-ac', '1', '-']
    return np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, dtype=np.float32)


def lag(a, b, maxlag=4800):
    a = a - a.mean(); b = b - b.mean()
    if a.std() < 1e-5 or b.std() < 1e-5: return None, 0
    L = 1
    while L < 2 * len(a): L *= 2
    c = np.fft.irfft(np.fft.rfft(a, L) * np.conj(np.fft.rfft(b, L)), L)
    c = np.concatenate([c[-maxlag:], c[:maxlag + 1]])
    k = int(np.argmax(np.abs(c))); return k - maxlag, float(c[k] / (np.linalg.norm(a) * np.linalg.norm(b)))


def main():
    job = json.load(open('WORK FILES (not for the edit)/job.json'))
    ref = job.get('settings', {}).get('_ref') or {}
    rolls = set(ref.get('rolls') or [ref.get('roll')])
    refch = int(ref.get('channel', 0))
    def fix(p):
        if MOUNT and '=' in MOUNT:
            a, b = MOUNT.split('=', 1); p = p.replace(a, b)
        return p
    cams = []
    for c in job['clips']:
        if c['roll'] in rolls and not c['path'].lower().endswith('.wav'):
            p = fix(c['path'])
            d = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', p],
                                     capture_output=True, text=True).stdout.strip() or 0)
            cams.append(dict(c, path=p, name=os.path.basename(p), length=int(d * SR)))
    polys = [(p, wav_hdr(p)) for p in sorted(glob.glob(os.path.join(POLY, '*.WAV')))]
    print(f"{'camera clip':38} {'at TC':>11} {'boom-cam ms':>11}  cc   file / track")
    res = []
    for c in sorted(cams, key=lambda c: c['start']):
        for frac in (0.08, 0.5, 0.92):
            t = int(c['start'] + frac * c['length']); W = 8 * SR
            for p, h in polys:
                if not (h['tr'] <= t < h['tr'] + h['n']): continue
                bch = next((i for i, nm in enumerate(h['names']) if nm.strip().upper() in ('BOOM', 'BOOM1')), None)
                if bch is None: continue
                boom = wav_read(p, h, bch, t - h['tr'], W)
                if np.abs(boom).max() < 1e-4: continue
                cam = cam_read(c['path'], refch, t - c['start'], W)
                n = min(len(cam), len(boom))
                if n < SR: continue
                L, cc = lag(boom[:n], cam[:n])
                tcf = int(round(t / SR * 24000 / 1001))
                tc = f"{tcf // 86400:02d}:{tcf // 1440 % 60:02d}:{tcf // 24 % 60:02d}:{tcf % 24:02d}"
                if L is None: print(f"{c['name']:38} {tc:>11} {'silent':>11}"); continue
                res.append((t, L, cc))
                print(f"{c['name']:38} {tc:>11} {1000 * L / SR:11.1f}  {cc:.2f}   {os.path.basename(p)} / {h['names'][bch]}")
    good = [(t, L, cc) for t, L, cc in res if abs(cc) > 0.3]
    if good:
        ms = [1000 * L / SR for _, L, _ in good]
        print(f"\n{len(good)} confident points: offset mean {np.mean(ms):.2f} ms, min {min(ms):.1f}, max {max(ms):.1f}")
        if len(good) > 2:
            k = np.polyfit(np.array([x[0] for x in good]) / SR / 3600, np.array(ms), 1)[0]
            print(f"drift across the day: {k:.2f} ms per hour")


if __name__ == '__main__':
    main()
