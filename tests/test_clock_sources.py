"""Which clock a no-timecode camera clip is placed by, per stamp format. No fixtures, no ffmpeg: this is the
decision table for Project._clock_place, which test_clock_place.py only covers end to end for the iPhone case.

  Canon file name      -> the record start it carries, even when the creation stamp is the END of the take
  local stamp + offset -> that time of day (iPhone)
  local stamp, naive   -> that time of day (Canon RAW's uuid atom, and most non-Apple cameras)
  plain UTC 'Z'        -> left unplaced on purpose: the time of day needs a zone the file doesn't carry
  no camera metadata   -> left unplaced: only the file's own date survived the copy
  a day past midnight  -> the whole day added, so an early-hours clip lands after the evening, not before it
"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync import tc as TC
from midwigsync.scan import Project

H = TC.SR * 3600


class P(Project):
    def __init__(self, clips=None):
        self.fps, self.df = 24000 / 1001, False
        self.clips = clips or []


def clip(created=None, name='IMG_8554.MOV', meta=True, day='2026-11-10'):
    return dict(kind='video', name=name, path='/x/' + name, created=created, created_meta=meta,
                orig_start=None, start=None, day=day, fps=None)


def at(tc, fps=24000 / 1001):
    return TC.tc_to_samples(tc, fps, False)


p = P()
cases = [
    ('iPhone, offset without a colon', clip('2026-11-10T07:58:59-0500'), at('07:58:59:00')),
    ('iPhone, offset with a colon', clip('2026-11-10T07:58:59-05:00'), at('07:58:59:00')),
    ('naive local stamp (Canon RAW)', clip('2026-11-10T20:13:02', 'A_0004C008X261110_2013022S_CANON.CRM'),
     at('20:13:02:00')),
    ('plain UTC, zone unknown', clip('2026-11-10T12:58:59.000000Z', 'GX010042.MP4'), None),
    ('file date only, no camera metadata', clip('2026-11-12T22:40:00', meta=False), None),
    ('nothing at all', clip(None), None),
]
for label, c, want in cases:
    got = p._clock_place(c)
    assert got == want, (label, got, want)

# Canon writes the record start into the file name; its XF-AVC creation stamp is the END of the take, so the
# name is the one stamp that always means the same thing.
c = clip('2026-11-10T08:12:00-0500', 'B_0001C015A261110_080400HN_CANON.MXF')
assert p._clock_place(c) == at('08:04:00:00'), ('name clock lost to the end-of-take stamp', p._clock_place(c))

# A day that runs past midnight: analyze() adds a whole day to everything early, but a clip placed by its clock
# never goes through that pass. Without the same offset it lands ~24 h before the rest of the day.
night = P([dict(kind='video', day='2025-02-08', orig_start=int(20.5 * H), after_midnight=False),
           dict(kind='video', day='2025-02-08', orig_start=int(25.5 * H), after_midnight=True)])
late = night._clock_place(clip('2025-02-09T01:30:00-0500', 'IMG_1.MOV', day='2025-02-08'))
evening = night._clock_place(clip('2025-02-08T20:40:00-0500', 'IMG_2.MOV', day='2025-02-08'))
assert late == at('01:30:00:00') + TC.day_samples(night.fps), ('after-midnight clip not carried past midnight', late)
assert late > evening, ('an after-midnight clip sorted before the evening it followed', late, evening)
assert evening == at('20:40:00:00'), evening

# The same clock time on a day that does NOT wrap stays where it is.
plain = P([dict(kind='video', day='2026-11-10', orig_start=int(9 * H), after_midnight=False)])
assert plain._clock_place(clip('2026-11-10T01:30:00-0500', 'IMG_3.MOV')) == at('01:30:00:00')

# A clip that already has a position is never touched.
assert p._clock_place(dict(clip('2026-11-10T07:58:59-0500'), orig_start=at('09:00:00:00'))) is None
assert p._clock_place(dict(clip('2026-11-10T07:58:59-0500'), start=at('09:00:00:00'))) is None

print('CLOCK SOURCES GOOD')
