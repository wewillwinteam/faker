# Mech Royale bot (team 2.3%)

A Python bot for the Builders Buildathon Mech Royale track.

- `bot.py`: entry point. Loads `params.json` (kit, agent settings, `hybrid`) and runs the brain. While a match looks
  like practice, a practice driver (below) plays; when it turns out to be a free-for-all, our brain takes over.
- `brain/`: the mech's agents. `world.py` (who sees us, cover, the free-for-all detector), `tracker.py` (where every
  enemy it has seen or heard is now), `reasoner.py` (one intent and one target), `situation.py` (fight read: run or
  stay from who has a line on us and who wins the trade), `agents.py` (zone, escape, combat, loot, stealth, farm,
  drift), `harness.py` (picks one agent per decision).
- `driver.py`: runs a practice driver as a child process and relays the mechfight/1 protocol to it (standard library).
- `lance_bot.py`: the public MIT bot by LanceHuang245 ([mech-royale](https://github.com/LanceHuang245/mech-royale),
  commit 6bdf03b), unchanged apart from a header comment. License in `LICENSE-lance`.
- `lance_plus.py`: our fork of it. Every change sits behind a switch: `arm` (find a laser or lightning before trading
  with the gun), `focus` (shoot laser carriers first), `range` (hold 140 m with a laser), plus Lance's own `jink`.
- `nav.py`: map model built from `/arena` at start: terrain height, buildings, line of sight, a path field.
- `mechfight.py`: the stdin/stdout wrapper from the Builders mechfight kit, unchanged.
- `Dockerfile`: multi-stage. Builds the Go practice driver and bundles the TypeScript one with their pinned images,
  then python:3.13-slim (pinned) with our files, the driver binary, the bundle and `bun`. No network needed at runtime.
- `test_hybrid.py`: offline test of the practice driver hand-off (child drives, FFA switch, crash, hang, no ready).

Tags: `variant-f` (board 3.15), `variant-l` (board 2.15), `variant-s8` (board 4.54). Released under the MIT License.

## Third-party code (MIT)

Code we did not write this weekend, each used unchanged under its MIT License (the license file sits next to it):

| Driver | Language | Repository | Commit | Files here | License |
| --- | --- | --- | --- | --- | --- |
| `lance` | Python | https://github.com/LanceHuang245/mech-royale | `6bdf03b` | `lance_bot.py` | `LICENSE-lance` |
| `lance_plus` | Python | our fork of `lance` above, every change behind a switch | — | `lance_plus.py` | `LICENSE-lance` |
| `giddy` | Go | https://github.com/justnau/giddygiddyboy-mech-royale | `345c0de2f43f5f27a4ecf74c1c25c789d00fbc49` | `drivers/giddy/` (go.mod, cmd/, mech/) | `drivers/giddy/LICENSE` |
| `ttun` | TypeScript (Bun) | https://github.com/varchaskukreja/TTUN (folder `mech-royale/bot/`) | `11350d33091f01878c12fa2d63e942d12230566f` | `drivers/ttun/` (bot.ts, map.ts, mechfight.ts) | `drivers/ttun/LICENSE` |

Plus the Builders mechfight kit wrapper (`mechfight.py`, and TTUN's copy `drivers/ttun/mechfight.ts`). The Dockerfile
builds `giddy` with `golang:1.26.1-bookworm` and bundles `ttun` with `oven/bun:1.4.4-slim` (both pinned by digest) and
copies the `bun` runtime into our image. `params.json` `hybrid.practice` picks the practice driver (`"lance"`,
`"lance_plus"`, `"giddy"`, `"ttun"`, or anything else for our own brain throughout).
