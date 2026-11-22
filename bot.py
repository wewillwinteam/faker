"""Mech Royale bot, team 2.3%.

hybrid.practice "giddy": the giddygiddyboy driver (drivers/giddy/mechbot, Go, MIT) runs as a child process and plays
while the match looks like practice; anything else: our brain (brain/) plays the whole match. With
hybrid.ffa_switch_max_s > 0 our brain takes over once 2+ deaths we did not cause happen by then (a free-for-all).

One tick on 1 CPU: the child answers first (deadline hybrid.child_timeout_ms), then only the cheap half of our brain
runs (Harness.observe: world model, tracker, FFA detector). The full brain (Harness.decide) runs when it drives: after
the switch, on a tick the child missed (if hybrid.fallback_brain_ms is not used up), or for good after
hybrid.max_child_fails misses in a row. params.json holds every setting; MECH_PARAMS points at another file.
"""
import json
import os
import sys
import time

import mechfight as mf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import nav
except Exception:
    nav = None

from brain.harness import Harness  # noqa: E402
from driver import SubprocessDriver  # noqa: E402

VARIANT = "S11"
GIDDY = [os.path.join(HERE, "drivers", "giddy", "mechbot")]


def load_params():
    with open(os.environ.get("MECH_PARAMS") or os.path.join(HERE, "params.json"), encoding="utf-8") as f:
        return json.load(f)


def salted_hello(hello, salt):
    """The child's hello with its RNG seed mixed with a salt (0 = unchanged). Our own brain keeps the real hello."""
    try:
        salt = int(salt or 0)
    except (TypeError, ValueError):
        salt = 0
    if not salt:
        return hello
    h = dict(hello)
    h["seed"] = (int(hello.get("seed") or 0) + salt * 2654435761) % 4294967291 or salt
    return h


def slot_value(hello, table, default=None):
    """table {"<slot>": value} -> the value for the slot in this hello, else default."""
    try:
        v = (table or {}).get(str(int(hello.get("slot"))))
    except (TypeError, ValueError, AttributeError):
        v = None
    return default if v is None else v


def pick_salt(hello, hy):
    """hybrid.slot_salts {"<slot>": salt} for matches played from that slot, else hybrid.seed_salt. The board replays
    fixed seeds and the slot is in the hello, so a slot is a fixed set of board matches (wiki/concepts/board-ties.md)."""
    try:
        s = (hy.get("slot_salts") or {}).get(str(int(hello.get("slot"))))
        if s is not None:
            return int(s)
    except (TypeError, ValueError, AttributeError):
        pass
    return hy.get("seed_salt", 0)


class Bot:
    name = "2.3%-" + VARIANT

    def __init__(self, hello):
        P = load_params()
        self.hy = hy = P.get("hybrid", {})
        self.practice = hy.get("practice")
        self.ffa_max_s = hy.get("ffa_switch_max_s", 90)
        self.child_timeout = hy.get("child_timeout_ms", 30) / 1000.0
        self.max_fails = hy.get("max_child_fails", 5)
        self.fallback_ms = hy.get("fallback_brain_ms", 20)
        self.fails = 0
        self.child_last = {}
        self.ffa_mode = False
        self.pending = None
        self.drv = None
        if self.practice == "giddy":
            eg = slot_value(hello, hy.get("slot_endghost"))  # practice late-hide radius for this slot (None = giddy's 140)
            if eg is None:
                os.environ.pop("GIDDY_ENDGHOST", None)
            else:
                os.environ["GIDDY_ENDGHOST"] = str(float(eg))
            try:
                cmd = json.loads(os.environ["MECH_DRIVER_CMD"]) if os.environ.get("MECH_DRIVER_CMD") else GIDDY
                d = SubprocessDriver(cmd, cwd=None if os.environ.get("MECH_DRIVER_CMD") else os.path.dirname(GIDDY[0]),
                                     name="giddy")
                if d.spawn(salted_hello(hello, pick_salt(hello, hy))):  # start it first: it loads its map while we build ours
                    self.drv = d
            except Exception as e:
                print("giddy driver failed to start: %s" % e, file=sys.stderr)
        m = None
        if nav is not None:
            try:
                m = nav.Map(hello["map"]["dir"])
            except Exception as e:
                print("map load failed:", e, file=sys.stderr)
        self.h = Harness(hello, m, P)
        if self.drv is not None:
            if self.drv.wait_ready(hy.get("child_ready_s", 10)):
                print("giddy driver ready in %.0f ms" % self.drv.ready_ms, file=sys.stderr)
            else:
                self.drv = None                     # it printed why; our brain drives

    def check_ffa(self):
        w = self.h.w
        if not self.ffa_mode and w.ffa and w.t <= self.ffa_max_s:
            self.ffa_mode = True
            print(f"free-for-all detected at {w.t:.0f}s: our brain drives", file=sys.stderr)
            if self.drv is not None:
                self.drv.close()
                self.drv = None

    def observe(self, obs):
        """World + tracker + FFA detector, every tick (bot.after runs it once the child's answer is sent)."""
        h = self.h
        h.observe(obs)
        if self.drv is not None:
            for ev in h.w.events:                   # upgrades the child picked count for our kit list later
                if ev.get("type") == "upgrade" and ev.get("id"):
                    h.upg_taken.append(ev["id"])
        self.check_ffa()

    def after(self):
        obs, self.pending = self.pending, None
        if obs is not None:
            self.observe(obs)

    def brain(self, obs, tag=""):
        act, agent, note = self.h.decide(obs)
        w = self.h.w
        mf.draw.label(f"{VARIANT}/{self.h.kit_id} {tag}{agent} {note} hp {w.me['hp']:.0f}")
        return act

    def act(self, obs):
        if self.drv is None:
            self.observe(obs)                       # our brain always runs, so it knows the match
            return self.brain(obs)
        t0 = time.perf_counter()
        a = self.drv.act(obs, self.child_timeout)
        if a is not None:                           # send the child's answer now; our world model catches up after
            self.fails = 0
            self.child_last = {k: a[k] for k in ("move", "aim", "boost") if k in a}
            self.pending = obs
            mf.draw.label(f"{VARIANT} practice:giddy")
            return a
        h = self.h
        self.observe(obs)
        if self.ffa_mode:                           # child closed: our brain drives from this tick on
            return self.brain(obs)
        self.fails += 1                             # the child missed this tick (late, crashed or bad line)
        if self.drv.dead or self.fails >= self.max_fails:
            print(f"giddy driver dropped at {h.w.t:.1f}s ({self.drv.why or str(self.fails) + ' misses in a row'}):"
                  " our brain drives from now on", file=sys.stderr)
            self.drv.close()
            self.drv = None
        if (time.perf_counter() - t0) * 1000 < self.fallback_ms:
            return self.brain(obs, "fallback ")
        return dict(self.child_last)                # no time for our brain: keep moving the way the child was


if __name__ == "__main__":
    mf.run(Bot)
