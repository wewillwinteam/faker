"""Mech Royale bot, team 2.3%. Variant S7: practice is driven by the public MIT bot (lance_bot.py, LanceHuang245) until the
match turns out to be a free-for-all, then our own brain (below) takes over.
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
bot_g.py is the first single-file assassin pass (variant G), kept for comparison.
"""
import json
import os
import sys

import mechfight as mf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import nav
except Exception:
    nav = None

from brain.harness import Harness  # noqa: E402

try:
    import lance_bot  # practice driver (MIT, LanceHuang245): see lance_bot.py
except Exception as e:
    print("lance_bot import failed:", e, file=sys.stderr)
    lance_bot = None

VARIANT = "S8"


def load_params():
    with open(os.environ.get("MECH_PARAMS") or os.path.join(HERE, "params.json"), encoding="utf-8") as f:
        return json.load(f)


class Bot:
    name = "2.3%-" + VARIANT

    def __init__(self, hello):
        m = None
        if nav is not None:
            try:
                m = nav.Map(hello["map"]["dir"])
            except Exception as e:
                print("map load failed:", e, file=sys.stderr)
        P = load_params()
        self.h = Harness(hello, m, P)
        # hybrid: who drives while the match looks like practice (house bots), and how late we may still decide it's a
        # free-for-all (house bots only die to us or the zone; later zone deaths must not flip practice to FFA)
        self.hy = P.get("hybrid", {})
        self.lance = None
        if lance_bot is not None and self.hy.get("practice") == "lance":
            try:
                self.lance = lance_bot.Bot(hello)
            except Exception as e:
                print("lance init failed:", e, file=sys.stderr)
        self.ffa_mode = False

    def act(self, obs):
        act, agent, note = self.h.act(obs)          # our brain always runs, so it knows the match when it takes over
        w = self.h.w
        if not self.ffa_mode and w.ffa and w.t <= self.hy.get("ffa_switch_max_s", 90):
            self.ffa_mode = True
            print(f"free-for-all detected at {w.t:.0f}s: our brain drives", file=sys.stderr)
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

if __name__ == "__main__":
    mf.run(Bot)
