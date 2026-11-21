# Mech Royale bot (team 2.3%)

A Python bot for the Builders Buildathon Mech Royale track.

- `bot.py`: entry point. Loads `params.json` (kit, agent settings and the match driver) and runs it. Since S9 every
  match is driven by `lance_plus.py` with the switches in `params.json` (`hybrid.lance_flags`).
- `lance_bot.py`: the public MIT bot by LanceHuang245 ([mech-royale](https://github.com/LanceHuang245/mech-royale),
  commit 6bdf03b), unchanged apart from a header comment. License in `LICENSE-lance`.
- `lance_plus.py`: our fork of it. Every change sits behind a switch: `arm` (find a laser or lightning before trading
  with the gun), `focus` (shoot laser carriers first), `range` (hold 140 m with a laser), plus Lance's own `jink` and
  `sneak`. S10 runs it with every switch off: Lance's bot without quiet walking.
- `brain/`: the mech's agents. `world.py` (who sees us, cover), `tracker.py` (where every enemy it has seen or
  heard is now), `reasoner.py` (one intent and one target), `situation.py` (fight read: run or stay from who has a
  line on us and who wins the trade), `agents.py` (zone, escape, combat, loot, stealth, farm, drift),
  `harness.py` (picks one agent per decision).
- `nav.py`: map model built from `/arena` at start: terrain height, buildings, line of sight, a path field.
- `mechfight.py`: the stdin/stdout wrapper from the Builders mechfight kit, unchanged.
- `Dockerfile`: python:3.13-slim, standard library only, no network needed.

Tags: `variant-f` (board 3.15), `variant-l` (board 2.15), `variant-s8` (board 4.54), `variant-s9` (board 4.50). Prior code used: the Builders kit
wrapper and LanceHuang245/mech-royale (MIT), credited above.
Released under the MIT License.
