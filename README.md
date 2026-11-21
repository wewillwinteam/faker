# Builders Buildathon: Mech Royale bot (team 2.3%)

A Python bot for the Mech Royale track. Placement counts, not kills, so it stays inside the next
zone circle, moves quietly, backs away from enemies and faces whoever hits it.

- `bot.py`: our bot (set `VARIANT` and `FLAGS` at the top).
- `nav.py`: map model built from `/arena` at start: terrain height, building columns, line of sight, a path field.
- `mechfight.py`: the stdin/stdout wrapper from the Builders mechfight kit (`python/mechfight.py`), unchanged.
- `Dockerfile`: python:3.13-slim, standard library only, no network needed.

Prior code used: only the Builders kit wrapper above. Released under the MIT License.
