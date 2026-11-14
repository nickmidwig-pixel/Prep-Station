"""tc_far means "this clip's timecode sits hours away from every other clip of the day" — what a camera that
missed its jam looks like. It is measured to the NEAREST other timecode, not from the day's middle.

The old rule used distance from the day's median, which flags the tail of any long day whose clips bunch at
one end. On 20261110 (07:48 - 19:13, most of 249 clips in the morning) the median landed mid-morning and the
last clip of the night — 14 minutes after the one before it, on a camera fed by a timecode box, on a body the
consensus had already voted into the truth group — came out 9.3 h from the median and was marked suspect.
From there wavesync stopped trusting its timecode, sent it to the whole-day waveform search, found nothing to
match (the lav had stopped 8 minutes earlier) and reported it as a clip that could not be placed.
"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync import tc as TC
from midwigsync.scan import TC_ALONE_S, _tc_gap


def at(h, m=0, s=0):
    return int((h * 3600 + m * 60 + s) * TC.SR)


def far(starts, x):
    return _tc_gap(sorted(starts), x) > TC_ALONE_S * TC.SR


# 20261110's shape: a long day, clips bunched in the morning, a gap over the afternoon, an evening tail.
DAY = ([at(7, 48) + i * 90 * TC.SR for i in range(120)]
       + [at(11, 0) + i * 120 * TC.SR for i in range(40)]
       + [at(13, 0), at(13, 20)]
       + [at(17, 19), at(17, 40), at(18, 20), at(18, 56, 42)]
       + [at(19, 11, 2)])
C008 = at(19, 11, 2)

# The clip that started this: 14 minutes after the one before it. Not far from anything.
assert _tc_gap(sorted(DAY), C008) < 15 * 60 * TC.SR
assert not far(DAY, C008), 'the last clip of a long day is not an outlier'

# ... and the rule it replaced did flag it, which is the whole point of this test existing.
median = sorted(DAY)[len(DAY) // 2]
assert abs(C008 - median) > 8 * 3600 * TC.SR, 'the old median rule flagged this clip'

# The first clip after a long break is not an outlier either: it is judged by its nearest neighbour on
# EITHER side, so only a clip alone on both sides counts.
assert not far(DAY, at(17, 19)), 'a clip that opens an evening block has the rest of the block beside it'

# A whole body offset together is not this flag's job — the fewest-sources vote handles that, and each of
# its clips sits close to its siblings anyway.
shifted = [at(17), at(17, 5), at(17, 10)]
assert not any(far(DAY + shifted, s) for s in shifted)

# What it still catches: a timecode alone by more than the threshold on both sides.
assert far([at(9), at(9, 30), at(23, 30)], at(23, 30))
assert far([at(0, 5), at(9), at(9, 30)], at(0, 5))

# Edges.
assert not far([at(9), at(9)], at(9)), 'two clips sharing a timecode are not outliers'
assert not far([at(9)], at(9)), "the day's only timecode has nothing to be judged against"
assert not far([at(1), at(9)], at(9)), 'exactly at the threshold is not over it'
assert far([at(0, 59), at(9)], at(9)), 'just over the threshold is over it'

# Known limit, written down so it is not mistaken for a pass: a clip only a few hours adrift is NOT caught,
# because the threshold has to stay wide enough for a day where a recorder rolls once in the morning and the
# cameras only shoot in the evening. The rule this replaced missed that case too (it sat 6.8 h from the
# median, inside the same 8 h). Catching it needs a different test — continuity against the neighbours on its
# own card — and a real example to design against.
rogue = at(3, 5)
assert not far(DAY + [rogue], rogue), 'still uncaught: documented, not a regression'
assert abs(rogue - median) < 8 * 3600 * TC.SR, 'the old rule missed it as well'

print('TC FAR GOOD')
