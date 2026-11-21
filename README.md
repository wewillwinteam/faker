# Mech Royale bot (team 2.3%)

A Python bot for the Builders Buildathon Mech Royale track.

- `bot.py`: entry point. Loads `params.json` (kit and agent settings) and runs the brain.
- `brain/`: the mech's agents. `world.py` (who sees us, cover), `tracker.py` (where every enemy it has seen or
  heard is now), `reasoner.py` (one intent and one target), `situation.py` (fight read: run or stay from who has a
  line on us and who wins the trade), `agents.py` (zone, escape, combat, loot, stealth, farm, drift),
  `harness.py` (picks one agent per decision).
- `nav.py`: map model built from `/arena` at start: terrain height, buildings, line of sight, a path field.
- `mechfight.py`: the stdin/stdout wrapper from the Builders mechfight kit, unchanged.
- `Dockerfile`: python:3.13-slim, standard library only, no network needed.

Tags: `variant-f` (board 3.15), `variant-l` (board 2.15). Prior code used: only the Builders kit wrapper.
Released under the MIT License.
