"""Playback is one player at a time (2.4.1). Space during a render, K, a newer J/L or a stop must never leave an
afplay running that the window doesn't know about; and a render that finishes after it was overtaken never starts."""
import os, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from midwigsync import gui


class Fake:
    live = []
    def __init__(self, cmd):
        self.cmd = cmd; self.dead = False; Fake.live.append(self)
    def poll(self): return 0 if self.dead else None
    def terminate(self): self.dead = True
    def wait(self, t=None): return 0
    def kill(self): self.dead = True


gui.subprocess.Popen = lambda cmd, *a, **k: Fake(cmd)
api = gui.Api()

g1 = api._play_begin()
api.stop_play()                                   # Space while the first render is still going
assert api._play_start(g1, 'a.wav') is False, 'a stopped request started a player'
assert not [f for f in Fake.live if not f.dead]

g2 = api._play_begin()
assert api._play_start(g2, 'b.wav') is True
g3 = api._play_begin()                            # L again (faster) while b plays
assert Fake.live[-1].dead, 'the old player kept playing'
assert api._play_start(g2, 'late.wav') is False, 'a late render from an overtaken request started a player'
assert api._play_start(g3, 'c.wav') is True
assert len([f for f in Fake.live if not f.dead]) == 1

# many requests racing from threads: at most one live player at the end, and it belongs to the newest generation
def req():
    g = api._play_begin(); time.sleep(0.01); api._play_start(g, f'{g}.wav')
ts = [threading.Thread(target=req) for _ in range(12)]
[t.start() for t in ts]; [t.join() for t in ts]
live = [f for f in Fake.live if not f.dead]
assert len(live) <= 1, live
if live:
    assert live[0].cmd[1] == f'{api._play_gen}.wav', (live[0].cmd, api._play_gen)
api.stop_play()
assert not [f for f in Fake.live if not f.dead]
print('PLAY RACE GOOD')
