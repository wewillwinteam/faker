# Mech Royale bot (team 2.3%)

A Python bot for the Builders Buildathon Mech Royale track.

- `bot.py`: entry point. Loads `params.json` (kit, agent settings, `hybrid`). With `hybrid.practice` `"giddy"` the
  giddy practice driver (below) plays while the match looks like practice and our brain takes over when it turns out
  to be a free-for-all (or when the driver fails); anything else and our brain plays the whole match.
- `brain/`: our brain. `world.py` (who sees us, cover, the free-for-all detector), `tracker.py` (where every enemy it
  has seen or heard is now), `reasoner.py` (one intent and one target), `situation.py` (fight read: run or stay from
  who has a line on us and who wins the trade), `agents.py` (zone, escape, combat, loot, stealth, farm, drift),
  `cover.py` (cover, heal, reposition, hold, third-party), `sound.py` + `thirdparty.py` (place fights by ear and come
  in on the second party), `dodge.py` (dodge reflex), `harness.py` (picks one agent per decision, adds the reflexes).
- `driver.py`: runs the practice driver as a child process and relays the mechfight/1 protocol to it (standard
  library).
- `nav.py`: map model built from `/arena` at start: terrain height, buildings, trees, line of sight, a path field.
- `mechfight.py`: the stdin/stdout wrapper from the Builders mechfight kit, unchanged.
- `Dockerfile`: multi-stage. Builds the Go practice driver with its pinned image, then python:3.13-slim (pinned) with
  our files and the driver binary. No network needed at runtime.
- `test_hybrid.py` (practice driver hand-off: child drives, FFA switch, crash, hang, no ready), `test_slotsalts.py`
  (per-slot seed salts), `test_thirdparty.py` (third-party planner): offline tests.

Tags: `variant-f` (board 3.15), `variant-l` (board 2.15), `variant-s8` (board 4.54). Released under the MIT License.

## Third-party code (MIT)

Code we did not write this weekend, used unchanged under its MIT License (the license file sits next to it):

| Driver | Language | Repository | Commit | Files here | License |
| --- | --- | --- | --- | --- | --- |
| `giddy` | Go | https://github.com/justnau/giddygiddyboy-mech-royale | `e19e3c8d45d980df35c2fa5318353aaeb7c515bd` | `drivers/giddy/` (go.mod, cmd/, mech/) | `drivers/giddy/LICENSE` |

The practice late-hide rule in `giddy` (`EndGhost`, `drivers/giddy/mech/brain.go`) is from SandeepMadhavarapu/BattleRoyale (https://github.com/SandeepMadhavarapu/BattleRoyale, MIT, commit `b532564`).

Plus the Builders mechfight kit wrapper (`mechfight.py`). The Dockerfile builds `giddy` with
`golang:1.26.1-bookworm` (pinned by digest). `params.json` `hybrid.practice` picks who plays practice: `"giddy"`, or
anything else for our own brain throughout.
