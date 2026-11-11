"""Pictures from files ffmpeg can't decode (ProRes RAW from an iPhone in the Blackmagic app, and other formats only
Apple's own frameworks read), through macOS: AVFoundation first, then Quick Look for a poster frame.
Read-only: the clip is opened for reading; the JPEG goes to memory or to the app's cache. Mac only; None elsewhere."""
import os
import subprocess
import sys
import tempfile


def frame_jpeg(path, sec=0.0, width=480):
    """JPEG bytes of the frame at `sec` seconds into the clip, or None."""
    if sys.platform != 'darwin':
        return None
    return _avf(path, sec, width) or (_quicklook(path, width) if sec < 1.5 else None)


def _avf(path, sec, width):
    try:
        import AVFoundation
        import CoreMedia
        import Foundation
        import Quartz
    except Exception:  # noqa: BLE001 - the frameworks aren't installed
        return None
    try:
        asset = AVFoundation.AVURLAsset.URLAssetWithURL_options_(Foundation.NSURL.fileURLWithPath_(path), None)
        gen = AVFoundation.AVAssetImageGenerator.assetImageGeneratorWithAsset_(asset)
        gen.setAppliesPreferredTrackTransform_(True)
        gen.setMaximumSize_((float(width), 0.0))
        tol = CoreMedia.CMTimeMake(1, 24)
        gen.setRequestedTimeToleranceBefore_(tol)
        gen.setRequestedTimeToleranceAfter_(tol)
        res = gen.copyCGImageAtTime_actualTime_error_(CoreMedia.CMTimeMakeWithSeconds(max(0.0, sec), 600), None, None)
        img = res[0] if isinstance(res, tuple) else res
        if img is None:
            return None
        data = Foundation.NSMutableData.data()
        dest = Quartz.CGImageDestinationCreateWithData(data, 'public.jpeg', 1, None)
        Quartz.CGImageDestinationAddImage(dest, img, {Quartz.kCGImageDestinationLossyCompressionQuality: 0.8})
        if not Quartz.CGImageDestinationFinalize(dest):
            return None
        return bytes(data)
    except Exception:  # noqa: BLE001
        return None


def _quicklook(path, width):
    """Quick Look's poster frame (what Finder shows), written to a temp folder in the app's own space."""
    d = tempfile.mkdtemp(prefix='ps_ql_')
    try:
        subprocess.run(['qlmanage', '-t', '-s', str(int(width)), '-o', d, path],
                       capture_output=True, timeout=20)
        for fn in os.listdir(d):
            if fn.lower().endswith('.png'):
                with open(os.path.join(d, fn), 'rb') as f:
                    return f.read()   # PNG; the callers turn it into a data URI either way
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        for fn in os.listdir(d):
            try:
                os.remove(os.path.join(d, fn))
            except OSError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            pass
    return None
