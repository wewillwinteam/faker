"""Mech Royale bot, team 2.3%. Variant S11: practice is driven by a stronger public MIT bot until the match turns out to
be a free-for-all (2+ deaths we did not cause by hybrid.ffa_switch_max_s), then our own brain (below) takes over.
params.json hybrid.practice picks the practice driver:
  "giddy"       drivers/giddy/mechbot (justnau/giddygiddyboy-mech-royale, Go, a child process: driver.py)
  "ttun"        drivers/ttun/bot.js run by Bun (varchaskukreja/TTUN, TypeScript, a child process)
  "lance"       lance_bot.py (LanceHuang245, Python, in process; S8)
  "lance_plus"  lance_plus.py, our switch-gated fork of Lance (hybrid.lance_flags; S9 4.50, S10 all switches off)
  anything else: our brain drives everything.
Earlier, variant L: J ("blend": I's awareness + reasoner rules with F's farming) plus the fight read (brain/situation.py:
run or stay from who has a line on us, who is coming and who wins the trade, never from an HP line), laser kit.
H was the first agent brain, I added awareness + the reasoner.

The mech is a small set of agents under a harness (see brain/):
  world.py    world model: who sees us, who has a line on us, chance to stay unseen, cover spots
  agents.py   zone, escape, combat, loot, stealth, farm, drift: each bids for control
  harness.py  picks one agent per decision, adds shared reflexes, writes telemetry for the evaluator
Settings and kits: params.json (MECH_KIT=<kit> overrides the kit for a test run).
Playstyle: get the kit's weapon, move where no one sees us, strike from behind only when no one else is likely to
notice or a way out exists, break line of sight when 2+ enemies are on us.

Child-process drivers, one tick on 1 CPU: send the observation to the child first and wait for its answer (deadline
hybrid.child_timeout_ms), then run only the cheap half of our brain (Harness.observe: world model, tracker, the FFA
detector), so child time + our time stays far under 50 ms. The full brain (Harness.decide) runs only when it drives:
after the free-for-all switch, on a tick the child missed, or for good after hybrid.max_child_fails misses in a row.
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

try:
    import lance_bot  # practice driver (MIT, LanceHuang245): see lance_bot.py
except Exception as e:
    print("lance_bot import failed:", e, file=sys.stderr)
    lance_bot = None
try:
    import lance_plus  # our fork of it, every change behind a switch: see lance_plus.py
except Exception as e:
    print("lance_plus import failed:", e, file=sys.stderr)
    lance_plus = None

VARIANT = "S11"

# practice drivers that run as a child process (built in the Dockerfile): command, working dir, environment defaults
# (a variable already set in our environment wins, so `docker run -e` can override them for a test)
CHILD_DRIVERS = {
    "giddy": {"cmd": [os.path.join(HERE, "drivers", "giddy", "mechbot")], "cwd": os.path.join(HERE, "drivers", "giddy")},
    # Bun on 1 CPU: compile on the main thread, skip the slowest compiler tier, one GC marker, so the runtime's own
    # threads cannot freeze it past the deadline; no transpiler cache (read-only disk)
    "ttun": {"cmd": ["/usr/local/bin/bun", os.path.join(HERE, "drivers", "ttun", "bot.js")],
             "cwd": os.path.join(HERE, "drivers", "ttun"),
             "env": {"BUN_JSC_useConcurrentJIT": "0", "BUN_JSC_useFTLJIT": "0", "BUN_JSC_numberOfGCMarkers": "1",
                     "BUN_RUNTIME_TRANSPILER_CACHE_PATH": "0"}},
}


def load_params():
    with open(os.environ.get("MECH_PARAMS") or os.path.join(HERE, "params.json"), encoding="utf-8") as f:
        return json.load(f)


def child_command(name):
    """(command, cwd, env) for a child driver. MECH_DRIVER_CMD (a JSON list) overrides the command for local tests,
    e.g. a Windows build."""
    d = CHILD_DRIVERS[name]
    env = None
    if d.get("env"):
        env = dict(d["env"])
        env.update(os.environ)
    over = os.environ.get("MECH_DRIVER_CMD")
    if over:
        return json.loads(over), None, env
    return d["cmd"], d["cwd"], env


def salted_hello(hello, salt):
    """The hello for a child driver, with its RNG seed mixed with hybrid.seed_salt (0 = unchanged). Only the child
    sees it; our own brain keeps the real hello."""
    try:
        salt = int(salt or 0)
    except (TypeError, ValueError):
        salt = 0
    if not salt:
        return hello
    h = dict(hello)
    s = (int(hello.get("seed") or 0) + salt * 2654435761) % 4294967291
    h["seed"] = s or salt
    return h


def pick_salt(hello, hy):
    """The seed salt for this match: hybrid.slot_salts {"<slot>": salt} gives the matches we play from that slot
    their own salt; every other slot gets hybrid.seed_salt. The practice board replays the same private seeds on
    every submission and the bot never sees the seed, but the slot varies by match and is in the hello, so a slot is
    a fixed set of board matches: a salt that gained there can be kept there while every other match plays exactly
    as before. No table = seed_salt for all."""
    base = hy.get("seed_salt", 0)
    try:
        t = hy.get("slot_salts") or {}
        s = t.get(str(int(hello.get("slot"))))
        if s is not None:
            return int(s)
    except (TypeError, ValueError, AttributeError):
        pass
    return base


class Bot:
    name = "2.3%-" + VARIANT

    def __init__(self, hello):
        P = load_params()
        # hybrid: who drives while the match looks like practice (house bots), and how late we may still decide it's a
        # free-for-all (house bots only die to us or the zone; later zone deaths must not flip practice to FFA)
        self.hy = hy = P.get("hybrid", {})
        self.practice = hy.get("practice")
        self.ffa_max_s = hy.get("ffa_switch_max_s", 90)
        self.child_timeout = hy.get("child_timeout_ms", 30) / 1000.0
        self.max_fails = hy.get("max_child_fails", 5)
        self.fallback_ms = hy.get("fallback_brain_ms", 20)
        self.fails = 0
        self.child_last = {}
        self.drv = None
        if self.practice in CHILD_DRIVERS:
            try:
                cmd, cwd, env = child_command(self.practice)
                d = SubprocessDriver(cmd, cwd=cwd, name=self.practice, env=env)
                if d.spawn(salted_hello(hello, pick_salt(hello, hy))):  # start it first: it loads its map while we build ours
                    self.drv = d
            except Exception as e:
                print("%s driver failed to start: %s" % (self.practice, e), file=sys.stderr)
        m = None
        if nav is not None:
            try:
                m = nav.Map(hello["map"]["dir"])
            except Exception as e:
                print("map load failed:", e, file=sys.stderr)
        self.h = Harness(hello, m, P)
        self.lance = None
        drv = self.practice
        mod = lance_plus if drv == "lance_plus" else (lance_bot if drv == "lance" else None)
        if drv == "lance_plus" and mod is None:
            mod = lance_bot  # fork failed to import: plain Lance is still far better than nothing
        if mod is not None:
            try:
                self.lance = mod.Bot(hello)
                if mod is lance_plus:
                    fl = os.environ.get("MR_FLAGS")  # tests may override the shipped switches
                    self.lance.flags = set(x for x in (fl if fl is not None else self.hy.get("lance_flags", "")).split(",") if x)
                print(f"practice driver {mod.__name__} flags {sorted(self.lance.flags)}", file=sys.stderr)
            except Exception as e:
                print("lance init failed:", e, file=sys.stderr)
        if self.drv is not None:
            if self.drv.wait_ready(hy.get("child_ready_s", 10)):
                print("%s driver ready in %.0f ms" % (self.practice, self.drv.ready_ms), file=sys.stderr)
            else:
                self.drv = None             # it printed why; our brain drives
        self.ffa_mode = False

    def check_ffa(self):
        w = self.h.w
        if not self.ffa_mode and w.ffa and w.t <= self.ffa_max_s:
            self.ffa_mode = True
            print(f"free-for-all detected at {w.t:.0f}s: our brain drives", file=sys.stderr)
            if self.drv is not None:
                self.drv.close()
                self.drv = None

    def act(self, obs):
        if self.drv is not None and not self.ffa_mode:
            return self.act_child(obs)
        act, agent, note = self.h.act(obs)          # our brain always runs, so it knows the match when it takes over
        w = self.h.w
        self.check_ffa()
        if self.lance is not None and not self.ffa_mode:
            try:
                la = self.lance.act(obs)
                mf.draw.label(f"{VARIANT} practice:lance hp {w.me['hp']:.0f}")
                return la
            except Exception as e:
                print("lance act failed, our brain takes over:", e, file=sys.stderr)
                self.lance = None
        mf.draw.label(f"{VARIANT}/{self.h.kit_id} {agent} {note} hp {w.me['hp']:.0f} seen{len(w.seen_by)} sh{len(w.shooters)}")
        return act

    def act_child(self, obs):
        """Practice with a child-process driver: child first, then the cheap half of our brain."""
        t0 = time.perf_counter()
        a = self.drv.act(obs, self.child_timeout)
        h = self.h
        h.observe(obs)                              # world + tracker + FFA detector, every tick
        w = h.w
        for ev in w.events:                         # upgrades the child picked count for our kit list later
            if ev.get("type") == "upgrade" and ev.get("id"):
                h.upg_taken.append(ev["id"])
        self.check_ffa()
        if self.ffa_mode:                           # child killed: our brain drives from this tick on
            act, agent, note = h.decide(obs)
            mf.draw.label(f"{VARIANT}/{h.kit_id} {agent} {note} hp {w.me['hp']:.0f}")
            return act
        if a is not None:
            self.fails = 0
            self.child_last = {k: a[k] for k in ("move", "aim", "boost") if k in a}
            mf.draw.label(f"{VARIANT} practice:{self.practice} hp {w.me['hp']:.0f}")
            return a
        # the child missed this tick (late, crashed or bad line): our brain answers if there is time left
        self.fails += 1
        if self.drv.dead or self.fails >= self.max_fails:
            print(f"{self.practice} driver dropped at {w.t:.1f}s ({self.drv.why or str(self.fails) + ' misses in a row'}):"
                  " our brain drives from now on", file=sys.stderr)
            self.drv.close()
            self.drv = None
        if (time.perf_counter() - t0) * 1000 < self.fallback_ms:
            act, agent, note = h.decide(obs)
            mf.draw.label(f"{VARIANT}/{h.kit_id} fallback {agent} hp {w.me['hp']:.0f}")
            return act
        return dict(self.child_last)                # no time for our brain: keep moving the way the child was


if __name__ == "__main__":
    mf.run(Bot)
