"""The mech's agents. Each one looks at the world model and proposes what to do, with a bid (0..1) for how much
it should be in control right now. The harness picks one. Agents never act on their own.

zone    get inside the circle in time
escape  2+ enemies have a line on us (or we are low and under fire): break line of sight, wait at the corner
combat  strike an unaware target from behind, but only when the gate passes: the chance no one else notices is high,
        or a way out exists; or finish a fight we are already in
loot    weapons first (the kit's order), then cubes and supply drops, walking only where no one sees us
stealth enemies in view and none on us: circle outside their cone and 45 m, get behind one, stay unseen
farm    drones and crates for cubes, only where no one would see us
drift   nothing else to do: ease toward the next circle out of sight, look around
"""
import math
import random
from .world import ang_diff, unit, role


class Plan(dict):
    """move, boost, jump, aim, fire, note"""


def plan(move=(0.0, 0.0), aim=None, fire=(False, False), boost=False, jump=False, note=""):
    return Plan(move=list(move), aim=aim, fire=list(fire), boost=boost, jump=jump, note=note)


class Agent:
    name = "agent"

    def __init__(self, p, kit):
        self.p = p
        self.kit = kit

    def propose(self, w):
        return None


def gun_fire_ok(w, tx, ty, tz, d, yaw):
    g = w.weapons[0]
    if not g or g.get("reload", 0) != 0 or d > (g.get("stats") or {}).get("range", 230):
        return False
    err = abs(ang_diff(w.me["aim"][0], yaw))
    return err < math.atan2(2.2, max(d, 1.0)) + 0.04 and w.los(w.muz, (tx, ty, tz))


def lead_aim(w, e):
    ex, ey, ez = e["pos"]
    vx, vy = (e.get("vel") or [0, 0, 0])[:2]
    g = w.weapons[0]
    spd = g["stats"]["speed"] if g and g.get("stats") else 252.0
    px, py = ex, ey
    for _ in range(2):
        lead = math.dist((px, py), (w.x, w.y)) / spd
        px, py = ex + vx * lead, ey + vy * lead
    tz = ez + 4.5
    d = math.dist((ex, ey), (w.x, w.y))
    return math.atan2(py - w.y, px - w.x), math.atan2(tz - w.muz[2], max(d, 1.0)), tz


def keep_in_front(w, e):
    """Mistake M-005 (flanked): with a second threat behind or beside us, slide off the line between the two so
    both end up inside our front arc. Returns a unit move (0, 0 if nothing to fix)."""
    ex, ey = e["pos"][0], e["pos"][1]
    fy = math.atan2(ey - w.y, ex - w.x)
    worst, wa = None, 1.2
    for tr in w.tracker.tracks.values():
        if tr["id"] == e["id"] or w.t - tr["seen_t"] > 4.0:
            continue
        tx, ty, unc, age = w.tracker.est(tr)
        if math.hypot(tx - w.x, ty - w.y) > 160:
            continue
        a = abs(ang_diff(math.atan2(ty - w.y, tx - w.x), fy))
        if a > wa:
            worst, wa = (tx, ty), a
    if worst is None:
        return 0.0, 0.0
    tx, ty = worst
    mx, my = (ex + tx) / 2, (ey + ty) / 2
    lx, ly = unit(tx - ex, ty - ey)
    nx, ny = -ly, lx
    if (w.x - mx) * nx + (w.y - my) * ny < 0:
        nx, ny = -nx, -ny
    return nx, ny


def laser_still(w, d):
    """Stand still with the laser (owner idea; #1 practice bot parks 100-180 m out with a laser): a planted mech
    charges and lands the beam; the dodge reflex still steps us off incoming fire."""
    sec = w.weapons[1]
    if not w.mode_p.get("laser_still") or sec is None or sec["kind"] != "laser":
        return False
    if d < w.mode_p.get("still_min", 80.0) or d > 275:
        return False
    return sec.get("cooldown", 0) == 0 or (sec.get("charge") or 0) > 0


def secondary_fire(w, e, d, yaw, aim, tol):
    """Fire control for slot 1. Returns (fire1, aim)."""
    sec = w.weapons[1]
    if not sec:
        return False, aim
    k = sec["kind"]
    err = abs(ang_diff(w.me["aim"][0], yaw))
    ex, ey, ez = e["pos"]
    clear = w.los(w.muz, (ex, ey, ez + 4.5))
    if k == "lightning":
        return d < (w.lrange or 45.0) - 1 and err < 0.25 and w.me["energy"] > 4, aim
    if k == "laser":
        ch = sec.get("charge", 0) or 0
        full = (sec.get("stats") or {}).get("charge", 0.75) - 0.01
        if clear and d < 275:
            if sec.get("cooldown", 0) > 0 and ch == 0:
                return False, aim
            return not (ch >= full and err < tol), aim
        return 0 < ch < 2.5, aim
    if k == "mortar" and 25 < d < 225 and sec.get("cooldown", 0) == 0:
        v, gr = 72.0, 22.0
        h = (ez + 2.0) - (w.z + 9.5)
        disc = v ** 4 - gr * (gr * d * d + 2 * h * v * v)
        if disc > 0 and err < 0.05:
            root = math.sqrt(disc)
            ang = math.atan2(v * v - root, gr * d) if clear else math.atan2(v * v + root, gr * d)
            return abs(w.me["aim"][1] - ang) < 0.05, [aim[0], ang]
    return False, aim


# ------------------------------------------------------------------------------------------------------------
class ZoneAgent(Agent):
    name = "zone"

    def propose(self, w):
        if not (w.zone_urgent or w.zone_edge):
            return None
        nc = w.zone["next_center"]
        mv = list(w.goto(nc[0], nc[1]))
        out = not w.me["in_zone"]
        if not out:
            m2 = w.unseen_step(mv)
            if m2 != [0.0, 0.0]:
                mv = m2
        bid = self.p["bid"] if out else self.p["bid"] * 0.7
        return bid, plan(mv, boost=out and w.me["energy"] > 20, note="out" if out else "soon")


# ------------------------------------------------------------------------------------------------------------
class EscapeAgent(Agent):
    name = "escape"

    def __init__(self, p, kit):
        super().__init__(p, kit)
        self.until = -99.0
        self.cover = None
        self.calc_t = -99.0
        self.reached = False

    def propose(self, w):
        sh = w.shooters
        rd = getattr(w, "fight_read", None) or {}
        if rd.get("verdict") == "run":
            self.until = w.t + self.p["hold_s"]
        running = getattr(w, "stance", None) == "run"
        if running:
            self.until = max(self.until, w.stance_until)
        if w.t >= self.until or not (running or (w.alerted and w.recent)):
            self.cover, self.reached = None, False
            return None
        threats = sh or w.recent[:3]
        if not threats:   # running from someone we cannot see: head away from where the shots came from
            b = w.threat_yaw if w.threat_yaw is not None else w.me["aim"][0]
            mv = [-math.cos(b), -math.sin(b)]
            m2 = w.unseen_step(mv)
            return self.p["bid"], plan(m2 if m2 != [0.0, 0.0] else mv, [b, 0.0], boost=w.me["energy"] > 15, note="run blind")
        if self.cover is None or w.t - self.calc_t > 0.5:
            self.calc_t = w.t
            self.cover = w.find_cover(threats)
        mv, boost, jump = [0.0, 0.0], False, False
        x, y = w.x, w.y
        if self.cover:
            if math.dist(self.cover, (x, y)) > 2.5:
                mv = list(unit(self.cover[0] - x, self.cover[1] - y))
                boost = w.me["energy"] > 15
            elif not self.reached:
                self.reached = True
                w.log("cover")
        else:
            ax = sum(unit(x - e["pos"][0], y - e["pos"][1])[0] for e in threats[:3])
            ay = sum(unit(x - e["pos"][0], y - e["pos"][1])[1] for e in threats[:3])
            zv = w.goto(*w.zone["next_center"])
            mv = list(unit(ax + zv[0] * 0.5, ay + zv[1] * 0.5))
            boost = w.me["energy"] > 15
            jump = w.me["grounded"] and w.tick % 30 == 0
        # face the chaser's way in (brawlers and hunters come to us; snipers hold), shoot whoever is in reach
        chaser = min(threats, key=lambda e: (role(e) == "sniper", e["_d"]))
        px, py, _ = w.predict(chaser)
        aim = [math.atan2(py - y, px - x), 0.0]
        fire = [False, False]
        near = min(threats, key=lambda e: e["_d"])
        if w.los(w.muz, (near["pos"][0], near["pos"][1], near["pos"][2] + 4.5)):
            yaw, pitch, tz = lead_aim(w, near)
            aim = [yaw, pitch]
            fire[0] = gun_fire_ok(w, near["pos"][0], near["pos"][1], tz, near["_d"], yaw)
            if w.lrange and near["_d"] < w.lrange - 2:
                fire[1] = abs(ang_diff(w.me["aim"][0], yaw)) < 0.25
        return self.p["bid"], plan(mv, aim, fire, boost, jump, note="to cover" if self.cover else "back off")


# ------------------------------------------------------------------------------------------------------------
class CombatAgent(Agent):
    name = "combat"

    def __init__(self, p, kit):
        super().__init__(p, kit)
        self.strafe = 1.0
        self.strafe_t = 0
        self.striking = None

    def strike_range(self, w):
        s = self.kit.get("strike", "close")
        if s == "far" and w.sec == "laser":
            return self.p["far_range"]
        if w.lrange:
            return w.lrange - 3.0
        return 150.0 if s in ("gun", "far") else 38.0

    def gate(self, w, e):
        """Engage only if no one else is likely to notice, or a way out exists. Returns (ok, p, n_others, escape)."""
        p = w.p_undetected(w.x, w.y, w.z, ignore=e["id"])
        others = w.others_with_line(w.x, w.y, w.z, ignore=e["id"])
        esc = w.find_cover([e] + others, max_r=self.p["escape_r"]) is not None
        ok = (p >= self.p["engage_p"] and (not others or esc)) or (esc and p >= self.p["engage_p_escape"])
        return ok, p, len(others), esc

    def propose(self, w):
        R = getattr(w, "intent", None) or {}
        it, fid = R.get("intent"), R.get("focus")
        if it not in ("strike", "fight") or fid is None:
            self.striking = None
            return None
        e = next((m for m in w.enemies if m["id"] == fid), None)
        if e is None:
            return None
        if it == "fight":
            return self.p["bid"], self.duel(w, e, R.get("fire_ok", True))
        if self.striking != e["id"]:
            self.striking = e["id"]
            others = w.others_with_line(w.x, w.y, w.z, ignore=e["id"])
            w.log("strike", p=round(w.p_undetected(w.x, w.y, w.z, ignore=e["id"]), 2), others=len(others),
                  esc=True, tgt=e["id"], d=round(e["_d"]), ttk=R.get("ttk"))
        kr = w.reasoner.kill_range(w, w.tracker.tracks.get(e["id"], e)) or 40.0
        return self.p["bid"], self.strike(w, e, kr, R.get("fire_ok", False))

    def strike(self, w, e, kr, fire_ok):
        ex, ey, ez = e["pos"]
        d = e["_d"]
        yaw, pitch, tz = lead_aim(w, e)
        aim = [yaw, pitch]
        tol = math.atan2(2.2, max(d, 1.0)) + 0.02
        f1, aim = secondary_fire(w, e, d, yaw, aim, tol)
        fire = [gun_fire_ok(w, ex, ey, tz, d, yaw) and fire_ok, f1 and fire_ok]
        face = e["aim"][0]
        bx, by = math.cos(face), math.sin(face)
        vx, vy = (e.get("vel") or [0, 0, 0])[:2]
        hold = max(20.0, kr * 0.6)   # close in: shorter range = faster kill
        px, py = ex - bx * hold + vx * 0.4, ey - by * hold + vy * 0.4
        mv = list(unit(px - w.x, py - w.y)) if math.dist((px, py), (w.x, w.y)) > 3 else [vx / 12.0, vy / 12.0]
        rear = abs(ang_diff(math.atan2(w.y - ey, w.x - ex), face))
        boost = w.lrange is not None and rear < 1.75 and w.me["energy"] > 30
        if laser_still(w, d):
            return plan([0.0, 0.0], aim, fire, False, note="strike: still laser")
        return plan(mv, aim, fire, boost, note="strike")

    def duel(self, w, e, fire_ok=True):
        ex, ey, ez = e["pos"]
        d = e["_d"]
        yaw, pitch, tz = lead_aim(w, e)
        aim = [yaw, pitch]
        tol = math.atan2(2.2, max(d, 1.0)) + 0.02
        f1, aim = secondary_fire(w, e, d, yaw, aim, tol)
        f1 = f1 and fire_ok
        g = w.weapons[0]
        f0 = fire_ok and bool(g) and g.get("reload", 0) == 0 and d < (g.get("stats") or {}).get("range", 230) \
            and abs(ang_diff(w.me["aim"][0], yaw)) < tol * 2.5 and w.los(w.muz, (ex, ey, tz))
        prefer = 30.0 if w.sec == "lightning" else w.mode_p.get("duel_range", 55.0)   # close in: kills come faster up close
        ux, uy = unit(ex - w.x, ey - w.y)
        if (w.tick - self.strafe_t) > 14 + (w.tick % 7):
            self.strafe, self.strafe_t = -self.strafe, w.tick
        s = self.strafe
        if w.sec == "lightning":   # circle toward its back while closing
            s = 1.0 if ang_diff(math.atan2(w.y - ey, w.x - ex), e["aim"][0]) > 0 else -1.0
        radial = -0.6 if d < prefer * 0.6 else (0.6 if d > prefer * 1.3 else 0.0)
        zv = w.goto(*w.zone["next_center"]) if (w.zone_urgent or w.zone_edge) else (0.0, 0.0)
        cv = (0.0, 0.0)
        if w.tick % 10 == 0 or not hasattr(self, "duel_cover"):
            th = [x for x in w.recent if not x.get("ghost")][:3] or [e]
            self.duel_cover = w.find_cover(th, max_r=60.0)
        if getattr(self, "duel_cover", None) is not None and w.sec != "lightning" and w.mode_p.get("duel_cover", True):
            cx, cy = self.duel_cover
            if math.dist((cx, cy), (w.x, w.y)) > 3:
                cv = unit(cx - w.x, cy - w.y)     # work toward cover while trading, never turn our back
        kx, ky = keep_in_front(w, e)
        if kx or ky:
            s = 1.0 if (-uy * kx + ux * ky) > 0 else -1.0      # strafe the way that brings the flanker in front
        mv = [-uy * s * 0.85 + ux * radial + zv[0] * 0.6 + cv[0] * 0.9 + kx * 0.8,
              ux * s * 0.85 + uy * radial + zv[1] * 0.6 + cv[1] * 0.9 + ky * 0.8]
        boost = w.sec == "lightning" and d > prefer and w.me["energy"] > 35
        if not w.me["in_zone"]:
            mv, boost = [zv[0] - uy * s * 0.3, zv[1] + ux * s * 0.3], w.me["energy"] > 25
        jump = (w.tick % 34 == 0) and d > 40
        if laser_still(w, d) and w.me["in_zone"]:
            return plan([0.0, 0.0], aim, [f0, f1], False, False, note="duel: still laser")
        return plan(mv, aim, [f0, f1], boost, jump, note="duel")


# ------------------------------------------------------------------------------------------------------------
class LootAgent(Agent):
    name = "loot"

    def propose(self, w):
        order = self.kit.get("secondary", [])
        rank = {k: i for i, k in enumerate(order)}
        cur = rank.get(w.sec, 99) if w.sec else 99
        best = None
        for p in w.obs["pickups"]:
            k = p["kind"]
            if k not in rank or rank[k] >= cur or k in w.held:
                continue
            dd = math.dist(p["pos"][:2], (w.x, w.y))
            if dd > self.p["see_r"] or abs(p["pos"][2] - w.z) > 6:
                continue
            rush = (getattr(w, "intent", None) or {}).get("intent") == "get weapon"
            if dd > 25 and not rush and w.p_undetected(p["pos"][0], p["pos"][1], p["pos"][2]) < self.p["min_p"]:
                continue
            s = dd + rank[k] * 40
            if best is None or s < best[0]:
                best = (s, p, dd)
        bid = self.p["bid"] + (0.15 if w.sec is None else 0.0)
        if (getattr(w, "intent", None) or {}).get("intent") == "get weapon":
            bid += 0.35
        if best is not None:
            _, p, dd = best
            mv = list(w.goto(p["pos"][0], p["pos"][1]))
            if dd > 20:
                m2 = w.unseen_step(mv)
                if m2 != [0.0, 0.0]:
                    mv = m2
            return bid, plan(mv, [math.atan2(p["pos"][1] - w.y, p["pos"][0] - w.x), 0.0], note="weapon " + p["kind"])
        # cubes and supply drops nearby (drops: only if hidden there)
        cand = []
        cc, cr = w.zone["center"], w.zone["radius"]
        mp = w.mode_p
        for c in w.obs["cubes"]:
            dd = math.dist(c["pos"][:2], (w.x, w.y))
            if dd < mp.get("cube_r", 80.0) and abs(c["pos"][2] - w.z) < 6 and math.dist(c["pos"][:2], cc) < cr * 0.92:
                cand.append((dd * 0.6, c["pos"], "cube"))
        for dr in w.obs["drops"]:
            dp = dr["pos"]
            dd = math.dist(dp[:2], (w.x, w.y))
            if dr.get("holder") not in (None, w.me["id"]) or dd > mp.get("drop_r", 220.0) or math.dist(dp[:2], cc) > cr * 0.92:
                continue
            if mp.get("drop_any") or (dr.get("lands_in", 99) < dd / 11.0 + 6 and w.p_undetected(dp[0], dp[1], dp[2]) > self.p["min_p"]):
                cand.append((dd * 0.5, dp, "drop"))
        if not cand:
            return None
        cand.sort(key=lambda c: c[0])
        _, gp, kind = cand[0]
        mv = list(w.goto(gp[0], gp[1]))
        if kind == "drop" and math.dist(gp[:2], (w.x, w.y)) < 4:
            mv = [0.0, 0.0]
        lb = mp.get("loot_cube_bid", bid * 0.8)
        return lb, plan(mv, [math.atan2(gp[1] - w.y, gp[0] - w.x), 0.0], boost=math.dist(gp[:2], (w.x, w.y)) > 50 and w.me["energy"] > mp.get("farm_boost_energy", 101), note=kind)


# ------------------------------------------------------------------------------------------------------------
class StealthAgent(Agent):
    name = "stealth"

    def __init__(self, p, kit):
        super().__init__(p, kit)
        self.prey = None

    def pick_prey(self, w):
        best, bs = None, 1e9
        for e in w.enemies:
            if e["_d"] > self.p["prey_r"]:
                continue
            ex, ey, _ = e["pos"]
            facing = abs(ang_diff(math.atan2(w.y - ey, w.x - ex), e["aim"][0]))
            s = e["_d"] + (math.pi - facing) * 25.0 + 0.02 * e.get("hp", 1400)
            for o in w.watchers:
                if o["id"] != e["id"] and math.dist(o["pos"][:2], (ex, ey)) < self.p["crowd_r"]:
                    s += 60.0
            if e["id"] == self.prey:
                s -= 30.0
            if s < bs:
                best, bs = e, s
        self.prey = best["id"] if best else None
        return best

    def propose(self, w):
        R = getattr(w, "intent", None) or {}
        if R.get("intent") != "stalk" or R.get("focus") is None:
            return None
        e = next((m for m in w.enemies if m["id"] == R["focus"]), None)
        if e is None:
            return None
        ex, ey, ez = e["pos"]
        d = e["_d"]
        face = e["aim"][0]
        rear = abs(ang_diff(math.atan2(w.y - ey, w.x - ex), face))
        kr = w.reasoner.kill_range(w, w.tracker.tracks.get(e["id"], e)) or 40.0
        if rear < 1.9:   # in front or to the side: swing wide, outside 45 m and its cone
            side = 1.0 if ang_diff(math.atan2(w.y - ey, w.x - ex), face) > 0 else -1.0
            a = face + side * 2.1
            r = max(w.near + 20.0, min(d, 90.0))
            gx, gy = ex + math.cos(a) * r, ey + math.sin(a) * r
        else:
            gx, gy = ex - math.cos(face) * kr * 0.8, ey - math.sin(face) * kr * 0.8
        mv = list(w.goto(gx, gy))
        m2 = w.unseen_step(mv)
        mv = m2 if m2 != [0.0, 0.0] else [mv[0] * 0.4, mv[1] * 0.4]   # never freeze: creep if every step is seen
        boost = d > self.p["quiet_r"] and w.me["energy"] > 40 and m2 != [0.0, 0.0]
        yaw = math.atan2(ey - w.y, ex - w.x)
        return self.p["bid"], plan(mv, [yaw, math.atan2(ez + 4.5 - w.muz[2], max(d, 1.0))], boost=boost,
                                   note="stalk #%d to %d m" % (e["id"], kr))


# ------------------------------------------------------------------------------------------------------------
class FarmAgent(Agent):
    name = "farm"

    def propose(self, w):
        mp = w.mode_p
        if w.me["cubes"] >= mp.get("farm_max_cubes", self.p["max_cubes"]) or (w.ffa and w.t > 120):
            return None
        if w.enemies and min(e["_d"] for e in w.enemies) < mp.get("farm_enemy_r", 1e9):
            return None
        it = (getattr(w, "intent", None) or {}).get("intent")
        if it not in (None, "rotate", "get weapon") and not (mp.get("farm_over_fights") and it in ("stalk", "strike") and not w.alerted):
            return None
        if w.tracker.near(w.x, w.y, w.mode_p.get("farm_clear_r", self.p.get("clear_r", 120.0)), max_age=8.0):
            return None
        if w.watchers and w.p_undetected(w.x, w.y, w.z) < self.p.get("min_p", 0.0):
            return None
        cc, cr = w.zone["center"], w.zone["radius"]
        cand = []
        nr_, cr_ = mp.get("farm_npc_r", 170.0), mp.get("farm_crate_r", 120.0)
        for n in w.npcs:
            dd = math.dist(n["pos"][:2], (w.x, w.y))
            if dd < nr_ and math.dist(n["pos"][:2], cc) < cr * 0.92:
                cand.append((dd, n["pos"], "npc"))
        for c in w.obs["crates"]:
            dd = math.dist(c["pos"][:2], (w.x, w.y))
            if dd < cr_ and abs(c["pos"][2] - w.z) < 8 and math.dist(c["pos"][:2], cc) < cr * 0.92:
                cand.append((dd * 1.2 + 20, c["pos"], "crate"))
        if mp.get("farm_memory"):            # crates/drones seen earlier, still out of view
            for k, (pos, kind, t) in list(getattr(w, "farm_mem", {}).items()):
                dd = math.dist(pos[:2], (w.x, w.y))
                if w.t - t < 90 and dd < mp.get("farm_mem_r", 300.0) and math.dist(pos[:2], cc) < cr * 0.92:
                    cand.append((dd * 1.4 + 40, pos, kind))
        if not cand:
            return None
        cand.sort(key=lambda c: c[0])
        _, gp, kind = cand[0]
        gd = math.dist(gp[:2], (w.x, w.y))
        tz = gp[2] + (1.75 if kind == "npc" else 1.2)
        yaw = math.atan2(gp[1] - w.y, gp[0] - w.x)
        aim = [yaw, math.atan2(tz - w.muz[2], max(gd, 1.0))]
        fire = [False, False]
        if w.weapons[0] and gd < 150:
            err = abs(ang_diff(w.me["aim"][0], yaw))
            fire[0] = err < 0.06 and w.weapons[0].get("reload", 0) == 0 and w.los(w.muz, (gp[0], gp[1], tz))
            if w.lrange and gd < w.lrange - 3:
                fire[1] = err < 0.2
        mv = [0.0, 0.0]
        if gd > 35:
            mv = list(w.goto(gp[0], gp[1]))
        elif gd < 15:
            ax, ay = unit(w.x - gp[0], w.y - gp[1])
            mv = [ax * 0.5, ay * 0.5]
        boost = gd > 60 and w.me["energy"] > mp.get("farm_boost_energy", 101)
        return mp.get("farm_bid", self.p["bid"]), plan(mv, aim, fire, boost=boost, note=kind)


# ------------------------------------------------------------------------------------------------------------
class DriftAgent(Agent):
    """Rotate: never camp. Head for the next circle, then keep moving through it on fresh waypoints; boost when
    no one is near so we cover ground fast; keep out of known enemies' view."""
    name = "drift"

    def __init__(self, p, kit):
        super().__init__(p, kit)
        self.scan = 0.0
        self.wp = None
        self.wp_t = -99.0
        self.rng = random.Random(7)

    def propose(self, w):
        nc, nr = w.zone["next_center"], w.zone["next_radius"]
        x, y = w.x, w.y
        if w.d_next > nr * 0.45:
            tx, ty = nc[0], nc[1]
            self.wp = None
        else:
            if self.wp is None or math.dist(self.wp, (x, y)) < 8 or w.t - self.wp_t > 14 or math.dist(self.wp, nc) > nr * 0.7:
                cx, cy = nc[0], nc[1]
                for _ in range(12):
                    a = self.rng.random() * 2 * math.pi
                    r = nr * 0.6 * math.sqrt(self.rng.random())
                    cx, cy = nc[0] + math.cos(a) * r, nc[1] + math.sin(a) * r
                    if w.walkable(cx, cy) and math.dist((cx, cy), (x, y)) > 40:
                        break
                self.wp, self.wp_t = (cx, cy), w.t
            tx, ty = self.wp
        mv = list(w.goto(tx, ty))
        m2 = w.unseen_step(mv)
        if m2 != [0.0, 0.0]:
            mv = m2
        near = w.tracker.near(x, y, 120.0, max_age=8.0)
        boost = not near and w.me["energy"] > 45
        if w.t < w.threat_until and w.threat_yaw is not None:
            aim = [w.threat_yaw, 0.0]
        else:
            self.scan += 0.35
            aim = [math.atan2(ty - y, tx - x) + math.sin(self.scan) * 1.2, 0.0]
        bid = self.p["bid"]
        if (getattr(w, "intent", None) or {}).get("intent") in ("rotate", "get weapon"):
            bid += 0.3
        return bid, plan(mv, aim, boost=boost, note="to circle" if self.wp is None else "patrol")


ALL = [ZoneAgent, EscapeAgent, CombatAgent, LootAgent, StealthAgent, FarmAgent, DriftAgent]
