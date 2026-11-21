"""Mech Royale bot, team 2.3%.

Variant F ("farmer"): map-aware (wall-aware paths and line of sight from /arena), farms NPC drones, crates and
cubes early for max HP, damage and upgrades, takes supply drops, then fights with every weapon it holds.

Two modes. Practice runs are 15 house bots that never fight each other, so `alive` only drops when we kill.
If `alive` drops without our kill, it is a free-for-all of other teams' bots: switch to FFA mode
(stay quiet, let others fight, finish the weak, keep max HP for the final circle).
Earlier variants (A-E) are in git history.
"""
import math
import sys
import mechfight as mf

try:
    import nav
except Exception:  # never let the map module stop the bot
    nav = None

VARIANT = "F"
FLAGS = {
    "farm": True,          # hunt NPC drones / crates / cubes when no enemy mech is in sight
    "drops": True,         # capture supply drops inside the next circle
    "ffa_detect": True,    # switch to FFA mode on an unexplained drop in `alive`
    "force_ffa": False,    # test FFA behavior on the practice board
}

UPGRADE_PREF = ["armor", "plating", "nanites", "gun_damage", "lightning_damage", "laser_damage", "gun_rate",
                "gun_mag", "capacitor", "reactor", "gyros", "servos", "sensors", "gun_velocity", "jets"]


def ang_diff(a, b):
    return (a - b + math.pi) % (2 * math.pi) - math.pi


def unit(dx, dy):
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-6 else (0.0, 0.0)


class Bot:
    name = "2.3%-" + VARIANT

    def __init__(self, hello):
        cfg = hello["config"]
        self.cfg = cfg
        self.reach = cfg["loot"]["pickup_radius"]
        self.cube_r = cfg["cubes"]["pickup_radius"]
        self.map = None
        if nav is not None:
            try:
                self.map = nav.Map(hello["map"]["dir"])
            except Exception as e:
                print("map load failed:", e, file=sys.stderr)
        self.flow_target = None
        self.scan = 0.0
        self.threat_yaw = None
        self.threat_until = -1.0
        self.my_kills = 0
        self.last_alive = None
        self.unexplained = 0
        self.ffa = FLAGS["force_ffa"]
        self.last_pos = None
        self.stuck_t = 0.0
        self.unstick_until = -1.0
        self.unstick_dir = (1.0, 0.0)
        self.last_hit_by = {}     # mech id -> time
        self.last_dmg_t = -99.0
        self.strafe = 1.0
        self.strafe_t = 0

    # ------------------------------------------------------------------ helpers
    def los(self, a, b):
        if self.map is None:
            return True
        try:
            return self.map.los(a, b)
        except Exception:
            return True

    def goto(self, x, y, tx, ty):
        """Direction toward (tx, ty): straight if the path is open, else follow the flow field."""
        dx, dy = unit(tx - x, ty - y)
        if self.map is None:
            return dx, dy
        d = math.hypot(tx - x, ty - y)
        g = self.map.ground(x, y)
        if d < 8 or self.map.los((x, y, g + 3.0), (tx, ty, self.map.ground(tx, ty) + 3.0)):
            return dx, dy
        if self.flow_target is not None and math.dist(self.flow_target, (tx, ty)) < 30:
            f = self.map.flow_dir(x, y)
            if f:
                return f
        return dx, dy

    # ------------------------------------------------------------------ main
    def act(self, obs):
        me = obs["self"]
        x, y, z = me["pos"]
        t, tick = obs["t"], obs["tick"]
        act = {"tick": tick}
        stats = me.get("stats") or {}
        walk = stats.get("walk_speed", 11.7)

        # --- events and mode detection
        kills_now = 0
        for ev in obs["events"]:
            ty = ev["type"]
            if ty == "kill":
                self.my_kills += 1
                kills_now += 1
            elif ty == "damaged":
                self.last_dmg_t = t
                if ev.get("bearing") is not None:
                    self.threat_yaw, self.threat_until = ev["bearing"], t + 3.0
                if ev.get("by") is not None:
                    self.last_hit_by[ev["by"]] = t
        alive = obs["alive"]
        if self.last_alive is not None and alive < self.last_alive:
            drop = self.last_alive - alive
            if drop > kills_now:
                self.unexplained += drop - kills_now
                if FLAGS["ffa_detect"] and self.unexplained >= 2:
                    self.ffa = True
        self.last_alive = alive

        # --- upgrade pick
        if obs["offer"]:
            ids = [c["id"] for c in obs["offer"]["choices"]]
            act["pick"] = min(range(len(ids)), key=lambda i: UPGRADE_PREF.index(ids[i]) if ids[i] in UPGRADE_PREF else 99)

        # --- flow field toward the next circle (built a slice per tick)
        zone = obs["zone"]
        nc, nr, cc, cr = zone["next_center"], zone["next_radius"], zone["center"], zone["radius"]
        if self.map is not None:
            if self.flow_target is None or math.dist(self.flow_target, nc) > 5:
                if self.map._job is None or math.dist(self.map._job[2], nc) > 5:
                    self.map.start_flow(nc[0], nc[1])
                self.map.step_flow(4000)
                if self.map._job is None:
                    self.flow_target = tuple(nc)

        d_next = math.dist((x, y), nc)
        d_cur = math.dist((x, y), cc)
        zstart = zone["shrink_start"]
        need = max(0.0, d_next - nr * 0.6) / max(walk, 1.0)
        urgent = (not me["in_zone"]) or (t + need + 12 > zstart and d_next > nr * 0.75)
        outside_soon = d_cur > cr - 15

        weapons = me["weapons"]
        held = {w["kind"] for w in weapons if w}
        muz = (x, y, z + stats.get("muzzle_height", 7.0))
        enemies = [m for m in obs["mechs"] if m["kind"] == "mech"]
        for e in enemies:
            e["_d"] = math.dist(e["pos"][:2], (x, y))
        enemies.sort(key=lambda m: m["_d"])

        mv = [0.0, 0.0]
        boost = False
        aim = None
        fire = [False, False]
        jump = False
        mode = "idle"

        if urgent or outside_soon:
            zv = self.goto(x, y, nc[0], nc[1])
            boost = (not me["in_zone"]) and me["energy"] > 20
        else:
            zv = (0.0, 0.0)

        # --- weapon pickups within reach: take any kind we lack (prefer lightning, laser)
        pref = {"lightning": 0, "laser": 1, "mortar": 2}
        free = [p for p in obs["pickups"] if p["kind"] in pref and p["kind"] not in held]
        if free:
            p = min(free, key=lambda q: (pref[q["kind"]], math.dist(q["pos"][:2], (x, y))))
            dp = math.dist(p["pos"][:2], (x, y))
            if dp < self.reach and abs(p["pos"][2] - z) < 4:
                if weapons[1] is None:
                    act["take"] = p["id"]
                elif weapons[1]["kind"] == "mortar" and p["kind"] in ("lightning", "laser"):
                    act["take"], act["drop"] = p["id"], 1

        target = None
        if enemies:
            for e in enemies[:4]:
                if self.los(muz, (e["pos"][0], e["pos"][1], e["pos"][2] + 4.5)):
                    target = e
                    break
            if target is None:
                target = enemies[0]

        engage = target is not None
        if engage and self.ffa:
            hurt_by_it = (t - self.last_hit_by.get(target["id"], -99)) < 6
            weak = target.get("hp", 1e9) < 0.35 * target.get("max_hp", 1400)
            engage = hurt_by_it or target["_d"] < 60 or (weak and target["_d"] < 160)

        if engage:
            mode = "fight"
            aim, fire, mv, jump, b2 = self.fight(obs, me, target, zv, muz)
            boost = boost or b2
        else:
            mv = [zv[0], zv[1]]
            if target is not None:
                e = target
                aim = [math.atan2(e["pos"][1] - y, e["pos"][0] - x), 0.0]
                mode = "watch"
            else:
                goal = None
                if not urgent and not outside_soon:
                    goal = self.pick_goal(obs, me, cc, cr, held)
                if goal is not None:
                    kind, gp, shoot = goal
                    mode = kind
                    gd = math.dist(gp[:2], (x, y))
                    if kind in ("npc", "crate"):
                        tz = gp[2] + (1.75 if kind == "npc" else 1.2)
                        aim = [math.atan2(gp[1] - y, gp[0] - x), math.atan2(tz - muz[2], max(gd, 1.0))]
                        if shoot and weapons[0] and gd < 150:
                            err = abs(ang_diff(me["aim"][0], aim[0]))
                            fire[0] = err < 0.06 and weapons[0].get("reload", 0) == 0 and self.los(muz, (gp[0], gp[1], tz))
                            if weapons[1] and weapons[1]["kind"] == "lightning" and gd < 40:
                                fire[1] = err < 0.2
                        if gd > 35:
                            mv = list(self.goto(x, y, gp[0], gp[1]))
                        elif gd < 15:
                            ax_, ay_ = unit(x - gp[0], y - gp[1])
                            mv = [ax_ * 0.5, ay_ * 0.5]
                    else:
                        mv = list(self.goto(x, y, gp[0], gp[1]))
                        if kind == "drop" and gd < 4:
                            mv = [0.0, 0.0]
                if aim is None:
                    if t < self.threat_until and self.threat_yaw is not None:
                        aim = [self.threat_yaw, 0.0]
                    else:
                        self.scan += 0.35
                        base = math.atan2(nc[1] - y, nc[0] - x) if d_next > 5 else 0.0
                        aim = [base + math.sin(self.scan) * 1.7, 0.0]
                if goal is None and not (urgent or outside_soon) and d_next > nr * 0.5:
                    gx, gy = self.goto(x, y, nc[0], nc[1])
                    mv = [gx * 0.8, gy * 0.8]
                    mode = "drift"

        # --- unstick: commanded to move but not moving
        if self.last_pos is not None and math.hypot(mv[0], mv[1]) > 0.5:
            if math.dist(self.last_pos, (x, y)) < 0.25:
                self.stuck_t += 0.1
            else:
                self.stuck_t = max(0.0, self.stuck_t - 0.2)
            if self.stuck_t > 1.2:
                a = math.atan2(mv[1], mv[0]) + (1.2 if (tick // 40) % 2 else -1.2)
                self.unstick_dir = (math.cos(a), math.sin(a))
                self.unstick_until = t + 1.2
                self.stuck_t = 0.0
        else:
            self.stuck_t = 0.0
        self.last_pos = (x, y)
        if t < self.unstick_until and mode != "fight":
            mv = [self.unstick_dir[0] * 0.6 + mv[0] * 0.4, self.unstick_dir[1] * 0.6 + mv[1] * 0.4]
            if me["grounded"]:
                jump = True
            elif me["energy"] > 25:
                boost = True

        gun = weapons[0]
        if mode != "fight" and gun and gun.get("ammo", 12) < 7 and gun.get("reload", 0) == 0 and not fire[0]:
            act["reload"] = True

        act.update(move=mv, boost=boost, aim=aim, fire=fire, jump=jump)
        mf.draw.label(f"{VARIANT}{'*' if self.ffa else ''} {mode} hp {me['hp']:.0f} c{me['cubes']}")
        return act

    # ------------------------------------------------------------------ goals when no enemy is in sight
    def pick_goal(self, obs, me, cc, cr, held):
        x, y, z = me["pos"]
        t = obs["t"]
        cand = []

        def ok(p):
            return math.dist(p[:2], cc) < cr * 0.92

        if FLAGS["drops"]:
            for dr in obs["drops"]:
                dp = dr["pos"]
                dd = math.dist(dp[:2], (x, y))
                if dr.get("holder") not in (None, me["id"]):
                    continue
                if dd < 220 and ok(dp) and dr.get("lands_in", 99) < dd / 11.0 + 6:
                    cand.append((dd * 0.5, ("drop", dp, False)))
        for c in obs["cubes"]:
            dd = math.dist(c["pos"][:2], (x, y))
            if dd < 80 and abs(c["pos"][2] - z) < 6 and ok(c["pos"]):
                cand.append((dd * 0.6, ("cube", c["pos"], False)))
        if me["weapons"][1] is None:
            for p in obs["pickups"]:
                if p["kind"] in ("lightning", "laser", "mortar") and p["kind"] not in held:
                    dd = math.dist(p["pos"][:2], (x, y))
                    if dd < 90 and abs(p["pos"][2] - z) < 6 and ok(p["pos"]):
                        cand.append((dd * 0.8, ("pickup", p["pos"], False)))
        if FLAGS["farm"] and (not self.ffa or t < 120) and me["cubes"] < 14:
            for n in obs["mechs"]:
                if n["kind"] != "npc":
                    continue
                dd = math.dist(n["pos"][:2], (x, y))
                if dd < 170 and ok(n["pos"]):
                    cand.append((dd, ("npc", n["pos"], True)))
            for c in obs["crates"]:
                dd = math.dist(c["pos"][:2], (x, y))
                if dd < 120 and abs(c["pos"][2] - z) < 8 and ok(c["pos"]):
                    cand.append((dd * 1.2 + 20, ("crate", c["pos"], True)))
        if not cand:
            return None
        cand.sort(key=lambda c: c[0])
        return cand[0][1]

    # ------------------------------------------------------------------ fighting
    def fight(self, obs, me, e, zv, muz):
        x, y, z = me["pos"]
        tick = obs["tick"]
        weapons = me["weapons"]
        ex, ey, ez = e["pos"]
        d = e["_d"]
        vx, vy = (e.get("vel") or [0, 0, 0])[:2]
        g = weapons[0]
        spd = g["stats"]["speed"] if g and g.get("stats") else 252.0
        lead = d / spd
        px, py = ex + vx * lead, ey + vy * lead
        lead = math.dist((px, py), (x, y)) / spd
        px, py = ex + vx * lead, ey + vy * lead
        tz = ez + 4.5
        yaw = math.atan2(py - y, px - x)
        pitch = math.atan2(tz - muz[2], max(d, 1.0))
        aim = [yaw, pitch]
        err = abs(ang_diff(me["aim"][0], yaw))
        tol = math.atan2(2.2, max(d, 1.0)) + 0.02
        seen_line = self.los(muz, (ex, ey, tz))
        fire = [False, False]
        if g and d < (g.get("stats") or {}).get("range", 230) and seen_line and err < tol * 2.5:
            fire[0] = g.get("reload", 0) == 0
        w1 = weapons[1]
        boost = False
        prefer = 70.0
        if w1:
            k = w1["kind"]
            if k == "lightning":
                prefer = 28.0
                fire[1] = d < 44 and err < 0.25 and me["energy"] > 4
            elif k == "laser":
                prefer = 90.0
                ch = w1.get("charge", 0) or 0
                if seen_line and d < 275:
                    fire[1] = not (ch >= 0.74 and err < tol)
                    if w1.get("cooldown", 0) > 0 and ch == 0:
                        fire[1] = False
                else:
                    fire[1] = 0 < ch < 2.5
            elif k == "mortar":
                prefer = 110.0
                if 25 < d < 225 and w1.get("cooldown", 0) == 0:
                    v, gr = 72.0, 22.0
                    h = (ez + 2.0) - (z + 9.5)
                    disc = v ** 4 - gr * (gr * d * d + 2 * h * v * v)
                    if disc > 0:
                        root = math.sqrt(disc)
                        ang = math.atan2(v * v - root, gr * d) if seen_line else math.atan2(v * v + root, gr * d)
                        tfl = d / max(v * math.cos(ang), 1.0)
                        mx, my = ex + vx * tfl, ey + vy * tfl
                        if err < 0.05:
                            aim = [math.atan2(my - y, mx - x), ang]
                            fire[1] = abs(me["aim"][1] - ang) < 0.05
        ux, uy = unit(ex - x, ey - y)
        if (tick - self.strafe_t) > 14 + (tick % 7):
            self.strafe = -self.strafe
            self.strafe_t = tick
        s = self.strafe
        radial = -0.6 if d < prefer * 0.6 else (0.6 if d > prefer * 1.3 else 0.0)
        mv = [-uy * s * 0.85 + ux * radial + zv[0] * 0.6, ux * s * 0.85 + uy * radial + zv[1] * 0.6]
        if not me["in_zone"]:
            mv = [zv[0] - uy * s * 0.3, zv[1] + ux * s * 0.3]
            boost = me["energy"] > 25
        if me["hp"] < 0.3 * me["max_hp"] and d > 60 and not self.ffa:
            mv = [-ux * 0.8 + zv[0] * 0.5, -uy * 0.8 + zv[1] * 0.5]
        jump = (tick % 34 == 0) and d > 40
        return aim, fire, mv, jump, boost


mf.run(Bot)
