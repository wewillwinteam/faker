"""Mech Royale bot, team 2.3%. Variant L: J ("blend": I's awareness + reasoner rules with F's farming) plus the fight read (brain/situation.py:
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

VARIANT = "M"


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
        self.h = Harness(hello, m, load_params())

    def act(self, obs):
        act, agent, note = self.h.act(obs)
        w = self.h.w
        mf.draw.label(f"{VARIANT}/{self.h.kit_id} {agent} {note} hp {w.me['hp']:.0f} seen{len(w.seen_by)} sh{len(w.shooters)}")
        return act


mf.run(Bot)
