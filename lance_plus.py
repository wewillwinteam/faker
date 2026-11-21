# Practice driver: our fork of the public MIT bot by LanceHuang245 (github.com/LanceHuang245/mech-royale, commit
# 6bdf03b), used under the MIT License (see LICENSE-lance). Every change from the original sits behind a strategy flag
# (FLAGS below; env MR_FLAGS overrides for tests) so each one can be raced against plain Lance in his sim.
# Our flags: arm (find a laser/lightning before trading with the gun), focus (shoot laser carriers first),
# anchor (wait between fights next to walls near the zone centre), farcover (look for cover up to 120 m, not 75),
# brave (break off to cover only at 4+ visible threats, 3+ when hurt, or heavy incoming fire),
# range (hold 140 m with a laser, not 110), greedy (grab kill cubes from farther and with 2 threats in view),
# front (prefer the target that keeps the other visible enemies in our front arc: rear hits do 1.5x).
"""mech-royale bot.

Survival-first fighter for the 16-mech battle royale:
- map-aware line of sight and wall avoidance (rasterized building prisms + terrain)
- loadout goal: laser + lightning (gun as fallback), never mortar (laser cook-off risk)
- leading gun aim, laser charge/release, lightning in its cone
- seeks cover to break line of sight and regenerate when hurt, outnumbered or outranged
- stays ahead of the zone, farms crates / NPC drones / cubes when no enemy is around
"""
import math
import os
import random
import heapq
import sys
import time
from array import array

import mechfight as mf

TAU = 2 * math.pi
FLAGS = set(x for x in os.environ.get("MR_FLAGS", "sneak").split(",") if x)  # strategy switches (env MR_FLAGS overrides; dev only)
WANT_KINDS = ("laser", "lightning")
UPGRADE_PRIORITY = [
    "plating", "armor", "nanites", "laser_damage", "lightning_efficiency", "gun_rate",
    "laser_charge", "gun_damage", "capacitor", "reactor", "gyros", "lightning_range",
    "gun_mag", "sensors", "gun_velocity", "servos", "jets", "lightning_chain", "laser_pierce",
    "mortar_reload", "mortar_velocity", "mortar_splash",
]
SOUND_THREAT = {"gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost", "jet", "reload"}


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------------------------
# World model
# ----------------------------------------------------------------------------------------------
class World:
    RES = 2.0

    def __init__(self, map_dir):
        self.ok = False
        self.min_x, self.min_y, self.nx, self.ny = -513.0, -334.0, 1, 1
        self.t_nx = self.t_ny = 1
        self.terrain = array("h", [0])
        self.top = array("f", [-1e9])
        self.base = array("f", [1e9])
        try:
            self._load(map_dir)
            self.ok = True
        except Exception as e:  # play blind rather than crash
            log("map load failed:", repr(e))

    def _load(self, map_dir):
        import json
        with open(os.path.join(map_dir, "v2", "world.json")) as f:
            w = json.load(f)
        t = w["terrain"]
        self.t_min_x, self.t_min_y, self.t_step = t["minX"], t["minY"], t["step"]
        self.t_nx, self.t_ny = t["nx"], t["ny"]
        terr = array("h")
        with open(os.path.join(map_dir, "v2", "terrain.i16"), "rb") as f:
            terr.frombytes(f.read())
        if sys.byteorder != "little":
            terr.byteswap()
        self.terrain = terr
        b = w["bounds"]
        self.bx0, self.bx1, self.by0, self.by1 = b["minX"], b["maxX"], b["minY"], b["maxY"]
        r = self.RES
        self.min_x, self.min_y = self.t_min_x, self.t_min_y
        self.nx = int((self.t_nx * self.t_step) / r) + 1
        self.ny = int((self.t_ny * self.t_step) / r) + 1
        n = self.nx * self.ny
        top = array("f", [-1e9]) * n
        base = array("f", [1e9]) * n
        for p in w["prisms"]:
            polys = [p["ring"]] + [h for h in p.get("holes") or [] if h]
            edges = []
            ys = p["ring"][1::2]
            for poly in polys:
                m = len(poly) // 2
                for k in range(m):
                    x1, y1 = poly[2 * k], poly[2 * k + 1]
                    x2, y2 = poly[(2 * k + 2) % (2 * m)], poly[(2 * k + 3) % (2 * m)]
                    if y1 != y2:
                        edges.append((x1, y1, x2, y2))
            j0 = max(0, int((min(ys) - self.min_y) / r))
            j1 = min(self.ny - 1, int((max(ys) - self.min_y) / r) + 1)
            pt, pb = p["top"], p["base"]
            for j in range(j0, j1 + 1):
                yc = self.min_y + (j + 0.5) * r
                xs = []
                for x1, y1, x2, y2 in edges:
                    if (y1 <= yc < y2) or (y2 <= yc < y1):
                        xs.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
                if len(xs) < 2:
                    continue
                xs.sort()
                row = j * self.nx
                for k in range(0, len(xs) - 1, 2):
                    i0 = max(0, int(math.ceil((xs[k] - self.min_x) / r - 0.5)))
                    i1 = min(self.nx - 1, int(math.floor((xs[k + 1] - self.min_x) / r - 0.5)))
                    for i in range(i0, i1 + 1):
                        c = row + i
                        if pt > top[c]:
                            top[c] = pt
                        if pb < base[c]:
                            base[c] = pb
        self.top, self.base = top, base

    # --- queries -------------------------------------------------------------------------------
    def ground(self, x, y):
        if not self.ok:
            return -1e9
        i = int((x - self.t_min_x) / self.t_step + 0.5)
        j = int((y - self.t_min_y) / self.t_step + 0.5)
        if i < 0:
            i = 0
        elif i >= self.t_nx:
            i = self.t_nx - 1
        if j < 0:
            j = 0
        elif j >= self.t_ny:
            j = self.t_ny - 1
        return self.terrain[j * self.t_nx + i] * 0.01

    def cell(self, x, y):
        i = int((x - self.min_x) / self.RES)
        j = int((y - self.min_y) / self.RES)
        if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
            return -1
        return j * self.nx + i

    def los(self, a, b, step=2.5):
        """True if the segment a->b (3D points) is not blocked by buildings or terrain."""
        if not self.ok:
            return True
        ax, ay, az = a
        dx, dy, dz = b[0] - ax, b[1] - ay, b[2] - az
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        n = int(d / step)
        if n < 2:
            return True
        top, base, nx, ny, r = self.top, self.base, self.nx, self.ny, self.RES
        mx, my = self.min_x, self.min_y
        terr, tnx, tny, tmx, tmy = self.terrain, self.t_nx, self.t_ny, self.t_min_x, self.t_min_y
        inv = 1.0 / n
        for k in range(1, n):
            f = k * inv
            x = ax + dx * f
            y = ay + dy * f
            z = az + dz * f
            i = int((x - mx) / r)
            j = int((y - my) / r)
            if 0 <= i < nx and 0 <= j < ny:
                c = j * nx + i
                if base[c] <= z <= top[c]:
                    return False
            ti = int(x - tmx + 0.5)
            tj = int(y - tmy + 0.5)
            if 0 <= ti < tnx and 0 <= tj < tny and terr[tj * tnx + ti] * 0.01 > z + 0.5:
                return False
        return True

    def blocked(self, x, y, z, d=0.0):
        """Would a mech with feet at height z bump into something at (x, y)?"""
        if not self.ok:
            return False
        if x < self.bx0 + 4 or x > self.bx1 - 4 or y < self.by0 + 4 or y > self.by1 - 4:
            return True
        c = self.cell(x, y)
        if c < 0:
            return True
        if self.top[c] > z + 1.3 and self.base[c] < z + 9.0:
            return True
        g = self.ground(x, y)
        return g > z + 1.4 + 0.55 * d

    def obstacle_top(self, x, y):
        c = self.cell(x, y)
        if c < 0 or not self.ok:
            return -1e9
        return max(self.top[c], self.ground(x, y))


# ----------------------------------------------------------------------------------------------
# Navigation grid + incremental A*
# ----------------------------------------------------------------------------------------------
NAV = 4.0
_NB = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
       (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142)]


class Nav:
    """Ground-level walkability grid (mech radius included) and A* over it."""

    def __init__(self, world):
        self.w = world
        self.x0, self.y0 = world.bx0, world.by0
        self.nx = int((world.bx1 - world.bx0) / NAV) + 1
        self.ny = int((world.by1 - world.by0) / NAV) + 1
        free = bytearray(self.nx * self.ny)
        blocked, ground = world.blocked, world.ground
        for j in range(self.ny):
            y = self.y0 + (j + 0.5) * NAV
            row = j * self.nx
            for i in range(self.nx):
                x = self.x0 + (i + 0.5) * NAV
                g = ground(x, y)
                if blocked(x, y, g, 0.0):
                    continue
                if (blocked(x + 2.5, y, g, 2.5) or blocked(x - 2.5, y, g, 2.5) or
                        blocked(x, y + 2.5, g, 2.5) or blocked(x, y - 2.5, g, 2.5)):
                    continue
                free[row + i] = 1
        self.free = free

    def cell(self, x, y):
        i = int((x - self.x0) / NAV)
        j = int((y - self.y0) / NAV)
        return (min(max(j, 0), self.ny - 1)) * self.nx + min(max(i, 0), self.nx - 1)

    def center(self, c):
        return (self.x0 + (c % self.nx + 0.5) * NAV, self.y0 + (c // self.nx + 0.5) * NAV)

    def nearest_free(self, c, max_r=10):
        if self.free[c]:
            return c
        ci, cj = c % self.nx, c // self.nx
        for r in range(1, max_r + 1):
            best, bd = None, 1e9
            for dj in range(-r, r + 1):
                for di in range(-r, r + 1):
                    if max(abs(di), abs(dj)) != r:
                        continue
                    i, j = ci + di, cj + dj
                    if 0 <= i < self.nx and 0 <= j < self.ny and self.free[j * self.nx + i]:
                        d = di * di + dj * dj
                        if d < bd:
                            best, bd = j * self.nx + i, d
            if best is not None:
                return best
        return None

    def line_clear(self, x0, y0, x1, y1):
        d = math.hypot(x1 - x0, y1 - y0)
        n = int(d / 2.0) + 1
        free = self.free
        for k in range(n + 1):
            f = k / n
            if not free[self.cell(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)]:
                return False
        return True


class Search:
    """Incremental A* on the Nav grid: call step(deadline) until .done."""

    def __init__(self, nav, start, goal):
        self.nav, self.start, self.goal = nav, start, goal
        self.gi, self.gj = goal % nav.nx, goal // nav.nx
        self.g = {start: 0.0}
        self.parent = {start: None}
        self.open = [(0.0, 0.0, start)]
        self.done = False
        self.path = None
        self.n = 0

    def step(self, deadline):
        nav = self.nav
        nx, ny, free = nav.nx, nav.ny, nav.free
        g_, parent, op = self.g, self.parent, self.open
        gi, gj = self.gi, self.gj
        while op:
            if time.perf_counter() > deadline:
                return
            for _ in range(40):
                if not op:
                    break
                f, g, c = heapq.heappop(op)
                if g > g_.get(c, 1e18):
                    continue
                if c == self.goal:
                    pts = []
                    while c is not None:
                        pts.append(nav.center(c))
                        c = parent[c]
                    pts.reverse()
                    self.path = pts
                    self.done = True
                    return
                self.n += 1
                if self.n > 25000:
                    self.done = True
                    return
                ci, cj = c % nx, c // nx
                for di, dj, cost in _NB:
                    ni, nj = ci + di, cj + dj
                    if ni < 0 or nj < 0 or ni >= nx or nj >= ny:
                        continue
                    n = nj * nx + ni
                    if not free[n]:
                        continue
                    if di and dj and not (free[cj * nx + ni] and free[nj * nx + ci]):
                        continue
                    ng = g + cost
                    if ng < g_.get(n, 1e18):
                        g_[n] = ng
                        parent[n] = c
                        hx, hy = abs(ni - gi), abs(nj - gj)
                        heapq.heappush(op, (ng + hx + hy - 0.5858 * min(hx, hy), ng, n))
        self.done = True  # open list exhausted: unreachable


# ----------------------------------------------------------------------------------------------
# Bot
# ----------------------------------------------------------------------------------------------
class Bot:
    name = "mech-royale"

    def __init__(self, hello):
        cfg = hello["config"]
        self.cfg = cfg
        self.me_id = hello.get("slot", -1)
        self.rng = random.Random(hello.get("seed", 0))
        self.world = World(hello.get("map", {}).get("dir", "/arena"))
        self.reach = cfg["loot"]["pickup_radius"]
        self.vision = cfg["vision"]
        self.last_dmg_t = -99.0
        self.last_dmg_bearing = None
        self.last_sound = None  # (t, bearing)
        self.strafe_sign = 1
        self.strafe_until = 0.0
        self.prev_dir = (1.0, 0.0)
        self.stuck_t = 0.0
        self.climb_until = 0.0
        self.detour_until = 0.0
        self.detour_dir = None
        self.last_pos = None
        self.target_id = None
        self.cover = None
        self.cover_vis = 99
        self.cover_calc_tick = -99
        self.retreating = False
        self.scan_yaw = None
        self.attackers = {}  # id -> last time it damaged us
        self.dmg_log = []  # (t, amount, by, bearing, rear)
        self.mode = "roam"
        self.tick_n = 0
        self.t0 = time.perf_counter()
        self.slow_ticks = 0
        self.nav = None
        if self.world.ok:
            try:
                tn = time.perf_counter()
                self.nav = Nav(self.world)
                log("nav built in %.2fs" % (time.perf_counter() - tn))
            except Exception as e:  # keep playing with local steering only
                log("nav failed:", repr(e))
        self.path = None
        self.path_goal = None
        self.path_i = 0
        self.search = None
        self.search_goal = None
        self.nav_cool = 0.0
        self.force_replan = False
        self.prog_pos = None
        self.noprog = 0
        self.boosting = False
        self.flags = set(FLAGS)  # strategy switches (tools/arena.py sets them per instance)
        self.hide_until = 0.0
        self.known_crates = {}   # id -> pos (seen, maybe out of sight now)
        self.known_picks = {}    # id -> (kind, pos)
        self.dodge_side = {}     # enemy id -> +1/-1 side we sidestep to
        self.last_kill_t = -99.0
        self.mortar_g = cfg["weapons"]["mortar"].get("gravity", 22.0)
        self._searching = False   # arm: unarmed and no weapon known
        self.explore_goal = None
        self.explore_until = 0.0
        self.looked = []          # arm: places we already searched
        self.anchor = None        # anchor: wall-hugging spot to wait at between fights
        self.anchor_until = 0.0

    def _late(self, ms):
        """True once this decision has used more than `ms` milliseconds (hard budget is 50)."""
        return (time.perf_counter() - self.t0) * 1000.0 > ms

    # --- helpers -------------------------------------------------------------------------------
    @staticmethod
    def _wpn(me, kind):
        for i, w in enumerate(me["weapons"]):
            if w and w["kind"] == kind:
                return i, w
        return None, None

    def _upgrade_pick(self, offer):
        best, best_rank = 0, 1e9
        for i, ch in enumerate(offer["choices"]):
            cid = ch.get("id", "")
            rank = UPGRADE_PRIORITY.index(cid) if cid in UPGRADE_PRIORITY else 50
            if rank < best_rank:
                best, best_rank = i, rank
        return best

    def _zone_state(self, obs, pos):
        z = obs["zone"]
        t = obs["t"]
        c, r = z["center"], z["radius"]
        nc, nr = z["next_center"], z["next_radius"]
        ss, se = z["shrink_start"], z["shrink_end"]
        if t >= ss and se > ss:
            f = min(1.0, (t - ss) / (se - ss))
            c = [c[0] + (nc[0] - c[0]) * f, c[1] + (nc[1] - c[1]) * f]
            r = r + (nr - r) * f
        margin = min(30.0, nr * 0.3)
        d_next = math.dist(pos[:2], nc)
        d_cur = math.dist(pos[:2], c)
        walk = 11.7
        need = max(0.0, d_next - (nr - margin))
        time_left = ss - t if t < ss else se - t
        urgent = (not obs["self"]["in_zone"]) or d_cur > r - 8
        must_go = urgent or (need > 0 and need / walk + (24 if "early" in self.flags else 12) > time_left)
        return {"c": c, "r": r, "nc": nc, "nr": nr, "margin": margin, "must_go": must_go,
                "urgent": urgent, "need": need, "dps": z.get("dps", 0)}

    def _safe_point(self, zs, p):
        """Is p inside the circle we want to be in soon?"""
        return math.dist(p[:2], zs["nc"]) <= max(zs["nr"] - zs["margin"] * 0.5, 3.0) or \
            (not zs["must_go"] and math.dist(p[:2], zs["c"]) <= zs["r"] - 25)

    # --- memory --------------------------------------------------------------------------------
    def _update_memory(self, obs, pos, eye):
        w = self.world
        cur_c = {c["id"]: c for c in obs["crates"]}
        for cid, c in cur_c.items():
            self.known_crates[cid] = c["pos"]
        for cid in list(self.known_crates):
            if cid in cur_c:
                continue
            p = self.known_crates[cid]
            if math.dist(p[:2], pos[:2]) < 28 and abs(p[2] - pos[2]) < 14 and w.los(eye, (p[0], p[1], p[2] + 1.2)):
                del self.known_crates[cid]
        cur_p = {p["id"]: p for p in obs["pickups"]}
        for pid, p in cur_p.items():
            self.known_picks[pid] = (p["kind"], p["pos"])
        for pid in list(self.known_picks):
            if pid in cur_p:
                continue
            kind, p = self.known_picks[pid]
            if math.dist(p[:2], pos[:2]) < 28 and abs(p[2] - pos[2]) < 14 and w.los(eye, (p[0], p[1], p[2] + 1.0)):
                del self.known_picks[pid]

    # --- free-for-all etiquette ----------------------------------------------------------------
    def _engage_ok(self, obs, me, target, t):
        """Should we open fire on this target? In a free-for-all every death before ours is worth the same,
        so we only start fights that are cheap: close range, weakened or distracted targets, ambushes from
        outside their view, or when they already shot at us."""
        m, d = target["m"], target["d"]
        if d < 55 or obs["alive"] <= 4:
            return True
        if m["hp"] < 0.55 * m["max_hp"]:
            return True
        if t - self.attackers.get(m["id"], -99.0) < 6.0 or m.get("charging"):
            return True
        x, y = me["pos"][0], me["pos"][1]
        brg = math.atan2(y - m["pos"][1], x - m["pos"][0])
        if abs(wrap(m["aim"][0] - brg)) > math.radians(75):
            return True  # its back/side is turned to us: free rear-armor hits
        return False

    # --- incoming fire -------------------------------------------------------------------------
    def _dodge(self, obs, me, enemies, t, can_zap):
        """Sidestep laser lines, bullets and mortar shells. Returns (vx, vy, strength, boost) or None."""
        x, y, z = me["pos"]
        cx, cy, cz = x, y, z + 4.5
        ax = ay = 0.0
        strength = 0.0
        boost = False
        # 1) charging lasers aimed near us
        for e in enemies:
            m = e["m"]
            if not m.get("charging") or not e["vis"]:
                continue
            d = e["d"]
            if d > 290 or d < 5:
                continue
            if can_zap and d < 40:
                continue  # lightning shorts the laser: shoot instead of running
            yaw = m["aim"][0]
            dx, dy = math.cos(yaw), math.sin(yaw)
            rx, ry = x - m["pos"][0], y - m["pos"][1]
            along = rx * dx + ry * dy
            if along <= 0:
                continue
            cross = dx * ry - dy * rx
            if abs(cross) > 7.0 + 0.03 * along:
                continue
            side = self.dodge_side.get(m["id"])
            if side is None:
                side = 1 if cross > 0.4 else (-1 if cross < -0.4 else self.strafe_sign)
                self.dodge_side[m["id"]] = side
            ax += -dy * side
            ay += dx * side
            strength = max(strength, 0.9)
            boost = boost or me["energy"] > 30
        for k in list(self.dodge_side):
            if not any(e["m"]["id"] == k and e["m"].get("charging") for e in enemies):
                del self.dodge_side[k]
        # 2) bullets about to arrive / mortar shells about to land
        for pr in obs["projectiles"]:
            kind = pr.get("kind")
            px, py, pz = pr["pos"]
            vx, vy, vz = pr["vel"]
            if kind == "gun":
                sp2 = vx * vx + vy * vy + vz * vz
                if sp2 < 1.0:
                    continue
                rx, ry, rz = px - cx, py - cy, pz - cz
                tc = -(rx * vx + ry * vy + rz * vz) / sp2
                if tc <= 0.02 or tc > 0.45:
                    continue
                mx_, my_, mz_ = rx + vx * tc, ry + vy * tc, rz + vz * tc
                if mx_ * mx_ + my_ * my_ + mz_ * mz_ > 3.8 * 3.8:
                    continue
                hs = math.hypot(vx, vy) or 1.0
                nx, ny = -vy / hs, vx / hs
                side = 1.0 if (nx * (x - px) + ny * (y - py)) >= 0 else -1.0
                ax += nx * side * 0.6
                ay += ny * side * 0.6
                strength = max(strength, 0.5)
            elif kind == "mortar":
                g = self.mortar_g
                wx, wy, wz, tt = px, py, pz, 0.0
                landed = False
                while tt < 6.0:
                    wx += vx * 0.1
                    wy += vy * 0.1
                    vz -= g * 0.1
                    wz += vz * 0.1
                    tt += 0.1
                    if wz <= self.world.obstacle_top(wx, wy) + 0.3:
                        landed = True
                        break
                if not landed or tt > 3.5:
                    continue
                dd = math.hypot(wx - x, wy - y)
                if dd > 13.0:
                    continue
                if dd < 0.5:
                    ux, uy = -vy, vx
                    n = math.hypot(ux, uy) or 1.0
                    ux, uy = ux / n, uy / n
                else:
                    ux, uy = (x - wx) / dd, (y - wy) / dd
                ax += ux * 2.0
                ay += uy * 2.0
                strength = max(strength, 1.0)
                boost = boost or (tt < 1.2 and dd < 9.0 and me["energy"] > 20)
        n = math.hypot(ax, ay)
        if strength <= 0 or n < 1e-6:
            return None
        return (ax / n, ay / n, strength, boost)

    # --- navigation ----------------------------------------------------------------------------
    def _drop_path(self):
        self.path = None
        self.path_goal = None
        self.search = None

    def _navigate(self, goal, pos, t):
        """Return the point to steer towards: the goal itself, or the next waypoint of an A* route around obstacles."""
        nav = self.nav
        if goal is None or nav is None:
            return goal
        x, y, z = pos
        if z > self.world.ground(x, y) + 3.0:
            return goal  # on a roof or in the air: local steering only
        gx, gy = goal[0], goal[1]
        if math.hypot(gx - x, gy - y) < 8.0 or nav.line_clear(x, y, gx, gy):
            self._drop_path()
            return goal
        gc = nav.cell(gx, gy)
        if self.force_replan:
            self._drop_path()
            self.force_replan = False
        if self.path is not None and self.path_goal is not None and \
                abs(gc % nav.nx - self.path_goal % nav.nx) + abs(gc // nav.nx - self.path_goal // nav.nx) <= 4:
            pass
        elif self.search is not None and \
                abs(gc % nav.nx - self.search_goal % nav.nx) + abs(gc // nav.nx - self.search_goal // nav.nx) <= 4:
            pass
        elif t >= self.nav_cool:
            sc = nav.nearest_free(nav.cell(x, y))
            ec = nav.nearest_free(gc)
            self.path = None
            if sc is None or ec is None:
                self.nav_cool = t + 2.0
                self.search = None
            else:
                self.search = Search(nav, sc, ec)
                self.search_goal = gc
        if self.search is not None and not self._late(20):
            self.search.step(self.t0 + 0.026)
            if self.search.done:
                self.path = self.search.path
                self.path_goal = self.search_goal
                self.path_i = 0
                if self.path is None:
                    self.nav_cool = t + 2.0
                self.search = None
        if self.path:
            return self._follow(pos)
        return goal

    def _follow(self, pos):
        nav, path = self.nav, self.path
        x, y = pos[0], pos[1]
        n = len(path)
        lo = max(0, self.path_i - 2)
        hi = min(n - 1, self.path_i + 12)
        bi, bd = lo, 1e18
        for k in range(lo, hi + 1):
            d = (path[k][0] - x) ** 2 + (path[k][1] - y) ** 2
            if d < bd:
                bi, bd = k, d
        if bd > 28.0 ** 2:  # far off the route: plan again
            self.force_replan = True
            return path[min(bi + 1, n - 1)]
        self.path_i = bi
        target = path[min(bi + 1, n - 1)]
        acc = 0.0
        for k in range(bi + 1, n):
            acc += math.hypot(path[k][0] - path[k - 1][0], path[k][1] - path[k - 1][1])
            if acc > 32.0:
                break
            if nav.line_clear(x, y, path[k][0], path[k][1]):
                target = path[k]
        return target

    # --- steering ------------------------------------------------------------------------------
    def _steer(self, pos, goal_dir, t):
        """Pick a walkable direction close to goal_dir. Returns (dx, dy)."""
        w = self.world
        x, y, z = pos
        gx, gy = goal_dir
        if t < self.detour_until and self.detour_dir:
            gx, gy = self.detour_dir
        best, best_s = (gx, gy), -1e9
        for k in range(16):
            a = k * TAU / 16
            dx, dy = math.cos(a), math.sin(a)
            s = dx * gx + dy * gy
            s += 0.15 * (dx * self.prev_dir[0] + dy * self.prev_dir[1])
            for d, pen in ((3.0, 2.5), (6.0, 1.2), (10.0, 0.5)):
                if w.blocked(x + dx * d, y + dy * d, z, d):
                    s -= pen
                    break
            if s > best_s:
                best, best_s = (dx, dy), s
        return best

    # --- main ----------------------------------------------------------------------------------
    def act(self, obs):
        self.tick_n += 1
        self.t0 = time.perf_counter()
        t = obs["t"]
        me = obs["self"]
        pos = me["pos"]
        x, y, z = pos
        st = me["stats"]
        act = {"tick": obs["tick"]}
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        eye = (x, y, z + st.get("eye_height", 8))
        muzzle_h = st.get("muzzle_height", 7)

        # ---- events / memory
        for e in obs["events"]:
            if e["type"] == "damaged" and e.get("by") is not None:
                self.last_dmg_t = t
                self.last_dmg_bearing = e.get("bearing")
                self.attackers[e["by"]] = t
                self.dmg_log.append((t, e.get("amount", 0), e["by"], e.get("bearing"), bool(e.get("rear"))))
            elif e["type"] == "kill":
                self.last_kill_t = t
            elif e["type"] in ("took", "take_failed"):
                self.known_picks.pop(e.get("id"), None)
        self.dmg_log = [d for d in self.dmg_log if t - d[0] < 2.5]
        dps_in = sum(d[1] for d in self.dmg_log) / 2.5
        for s in obs["sounds"]:
            if s["kind"] in SOUND_THREAT and s["dist"] != "far":
                self.last_sound = (t, s["bearing"])

        if obs["offer"]:
            act["pick"] = self._upgrade_pick(obs["offer"])

        zs = self._zone_state(obs, pos)
        self._update_memory(obs, pos, eye)

        # ---- enemies
        enemies = []
        for m in obs["mechs"]:
            if m["kind"] != "mech" or m["id"] == me["id"]:
                continue
            mp = m["pos"]
            d = math.dist(mp[:2], pos[:2])
            scale = 1 + 0.05 * m.get("cubes", 0)
            center = (mp[0], mp[1], mp[2] + 4.5 * scale)
            if d < 300 and not (d > 60 and self._late(15)):
                vis = self.world.los(eye, (mp[0], mp[1], mp[2] + 8 * scale))
                shoot = vis or self.world.los((x, y, z + muzzle_h), center)
            else:
                vis = shoot = False
            enemies.append({"m": m, "d": d, "vis": vis, "shoot": shoot, "center": center, "scale": scale})
        threats = [e for e in enemies if e["vis"] and e["d"] < 240]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        # attackers we cannot see: place a pseudo-threat along the damage bearing
        known = {e["m"]["id"] for e in enemies}
        hidden = {}
        for (dt_, amt, by, brg, rear) in self.dmg_log:
            if by not in known and brg is not None and t - dt_ < 1.5:
                hidden[by] = (brg, rear)
        for by, (brg, rear) in hidden.items():
            px, py = x + math.cos(brg) * 120, y + math.sin(brg) * 120
            threats.append({"m": {"id": by, "pos": [px, py, z], "weapons": [], "hp": 1, "max_hp": 1},
                            "d": 120.0, "vis": True, "shoot": False, "center": (px, py, z + 4.5),
                            "scale": 1.0, "pseudo": True})
        rear_hidden = [brg for (brg, rear) in hidden.values()]

        # ---- loadout
        held = [w["kind"] if w else None for w in me["weapons"]]
        li, lw = self._wpn(me, "laser")
        gi, gw = self._wpn(me, "gun")
        zi, zw = self._wpn(me, "lightning")
        has_laser, has_light = li is not None, zi is not None

        # ---- pickups we want
        want_pick = None
        seen_ids = {p["id"] for p in obs["pickups"]}
        picks_all = list(obs["pickups"]) + [{"id": i, "kind": k, "pos": pp} for i, (k, pp) in self.known_picks.items()
                                            if i not in seen_ids]
        for p in picks_all:
            if p["kind"] not in WANT_KINDS or p["kind"] in held:
                continue
            if abs(p["pos"][2] - z) > 25:
                continue
            d = math.dist(p["pos"][:2], pos[:2])
            if d > (240 if not (has_laser or has_light) else 160) or not self._safe_point(zs, p["pos"]) and d > 30:
                continue
            if want_pick is None or d < want_pick[1]:
                want_pick = (p, d)
        if want_pick and want_pick[1] < self.reach - 0.3 and abs(want_pick[0]["pos"][2] - z) < 3.8:
            p = want_pick[0]
            if None in held:
                act["take"] = p["id"]
            elif gi is not None and not (lw and lw.get("charge", 0) > 0):
                act["take"] = p["id"]
                act["drop"] = gi
            elif "mortar" in held:
                act["take"] = p["id"]
                act["drop"] = held.index("mortar")

        # ---- target choice
        target = None
        best = 1e9
        for e in enemies:
            m = e["m"]
            s = e["d"] + 0.2 * m["hp"]
            if m["hp"] <= 0.3 * m["max_hp"]:
                s -= 100
            if not e["shoot"]:
                s += 400
            if m["id"] == self.target_id:
                s -= 120
            if m["id"] in self.attackers and t - self.attackers[m["id"]] < 4:
                s -= 40
            if e["d"] > 285:
                s += 1000
            if "focus" in self.flags and "laser" in m.get("weapons", []):
                s -= 90  # laser carriers (house snipers) deal three quarters of the damage we take: kill them first
            if "front" in self.flags and e["shoot"]:
                # aiming at this one puts every visible enemy more than 85 deg off its bearing behind us (1.5x damage)
                b0 = math.atan2(m["pos"][1] - pos[1], m["pos"][0] - pos[0])
                for o in enemies:
                    if o is not e and o["vis"] and o["d"] < 200:
                        bo = math.atan2(o["m"]["pos"][1] - pos[1], o["m"]["pos"][0] - pos[0])
                        if abs(wrap(bo - b0)) > math.radians(85):
                            s += 45
            if s < best:
                best, target = s, e
        self.target_id = target["m"]["id"] if target else None
        hide = False
        if "patient" in self.flags and target is not None and not self._engage_ok(obs, me, target, t):
            hide = True
            target = None
            self.hide_until = t + 2.0

        # secondary targets for cubes (crates / NPCs) when no enemy can be shot
        farm = None
        if target is None or not target["shoot"]:
            cands = []
            for c in obs["crates"]:
                d = math.dist(c["pos"][:2], pos[:2])
                if d < 150 and c["pos"][2] < z + 6:
                    cands.append((d, "crate", c, (c["pos"][0], c["pos"][1], c["pos"][2] + 1.2)))
            if hp_frac > 0.55:
                for n in npcs:
                    d = math.dist(n["pos"][:2], pos[:2])
                    if d < 140:
                        cands.append((d + 30, "npc", n, (n["pos"][0], n["pos"][1], n["pos"][2] + 1.75)))
            cands.sort(key=lambda c: c[0])
            for c in cands[:4]:
                if self.world.los((x, y, z + muzzle_h), c[3]) and self._safe_point(zs, c[2]["pos"]):
                    farm = c
                    break

        # ---- aiming & firing
        fire = [False, False]
        aim_yaw, aim_pitch = me["aim"][0], 0.0
        cur_yaw, cur_pitch = me["aim"]
        shoot_at = None
        if target and target["shoot"]:
            shoot_at = ("mech", target["center"], target["m"].get("vel", [0, 0, 0]), target["d"], 2.5 * target["scale"])
        elif farm:
            d, kind, obj, ctr = farm
            vel = obj.get("vel", [0, 0, 0]) if kind == "npc" else [0, 0, 0]
            shoot_at = (kind, ctr, vel, d, 1.4 if kind == "crate" else 1.8)

        # hit by someone we cannot see and the current target is not in our face: turn to look
        if rear_hidden and not (target and target["shoot"] and target["d"] < 45):
            if not (lw and (lw.get("charge", 0) or 0) > 0):
                shoot_at = None
        if shoot_at:
            kind, ctr, vel, d, rad = shoot_at
            mx, my, mz = x, y, z + muzzle_h
            # gun lead
            gspeed = gw["stats"]["speed"] if gw else 252.0
            px, py, pz = ctr
            for _ in range(2):
                tt = math.dist((mx, my, mz), (px, py, pz)) / gspeed + 0.05
                px, py, pz = ctr[0] + vel[0] * tt, ctr[1] + vel[1] * tt, ctr[2] + max(-6.0, min(6.0, vel[2] * tt))
            g_yaw = math.atan2(py - my, px - mx)
            g_pitch = math.atan2(pz - mz, math.hypot(px - mx, py - my))
            l_yaw = math.atan2(ctr[1] + vel[1] * 0.1 - my, ctr[0] + vel[0] * 0.1 - mx)
            l_pitch = math.atan2(ctr[2] - mz, math.hypot(ctr[0] - mx, ctr[1] - my))
            tol_y = math.atan2(rad * 1.6, max(d, 1.0)) + 0.012
            tol_p = math.atan2(3.0, max(d, 1.0)) + 0.004

            laser_ready = False
            if lw:
                ls = lw["stats"]
                full = ls.get("charge", 0.75)
                charge = lw.get("charge", 0) or 0
                in_range = d < ls.get("range", 280) - 5
                if in_range and (kind == "mech" or kind == "npc" or (kind == "crate" and not gw)):
                    if charge > 0:
                        laser_ready = charge >= full - 0.02
                        tol_l = (math.atan2(rad * 0.85, max(d, 1.0)) + 0.004) if "laser_tight" in self.flags else tol_y
                        err = abs(wrap(l_yaw - cur_yaw)) <= tol_l and abs(l_pitch - cur_pitch) <= tol_p * 1.5
                        if laser_ready and err:
                            fire[li] = False  # release -> beam
                        elif charge > full + ls.get("hold_s", 2) - 0.25 and not err:
                            fire[li] = False  # would auto-fire wildly anyway; let go
                        else:
                            fire[li] = True
                    elif lw.get("cooldown", 0) <= 0.05:
                        fire[li] = True  # start charging
            if laser_ready:
                aim_yaw, aim_pitch = l_yaw, l_pitch
            else:
                aim_yaw, aim_pitch = g_yaw, g_pitch

            if gw and d < gw["stats"].get("range", 230) - 3:
                if gw.get("reload", 0) <= 0 and gw.get("ammo", 1) > 0:
                    if abs(wrap(g_yaw - cur_yaw)) <= tol_y and abs(g_pitch - cur_pitch) <= tol_p:
                        fire[gi] = True
            if zw:
                zs_ = zw["stats"]
                if d < zs_.get("range", 45) - 1 and me["energy"] >= zs_.get("energy", 3) + 1:
                    half = math.radians(zs_.get("cone_deg", 30)) * 0.4
                    if abs(wrap(math.atan2(ctr[1] - y, ctr[0] - x) - cur_yaw)) < half:
                        fire[zi] = True
        else:
            # no target: face threats / sounds / where we go
            look = None
            # unarmed and no weapon in sight: sweep the view to find one unless an enemy is close
            searching = "arm" in self.flags and not (has_laser or has_light) and want_pick is None
            if rear_hidden:
                look = rear_hidden[0]
            elif self.last_dmg_bearing is not None and t - self.last_dmg_t < 3:
                look = self.last_dmg_bearing
            elif enemies and not (searching and min(e["d"] for e in enemies) > 70):
                e0 = min(enemies, key=lambda e: e["d"])
                look = math.atan2(e0["m"]["pos"][1] - y, e0["m"]["pos"][0] - x)
            elif self.last_sound and t - self.last_sound[0] < 2:
                look = self.last_sound[1]
            if look is not None:
                aim_yaw = look
                self.scan_yaw = None
            else:
                if self.scan_yaw is None:
                    self.scan_yaw = cur_yaw
                self.scan_yaw = wrap(self.scan_yaw + 0.35)
                aim_yaw = self.scan_yaw
            aim_pitch = 0.0
            if gw and gw.get("reload", 0) <= 0 and gw.get("ammo", 12) < gw["stats"].get("mag", 12) * 0.6:
                act["reload"] = True
            if lw and (lw.get("charge", 0) or 0) > 0:
                # keep the charge while someone is tracked (it auto-fires after hold_s anyway)
                fire[li] = bool(enemies) and (lw.get("charge", 0) or 0) < lw["stats"].get("charge", 0.75) + 1.5

        # ---- incoming fire
        dodge = self._dodge(obs, me, enemies, t, bool(zw) and me["energy"] > 6)

        # ---- cubes in sight (kills drop theirs + 2)
        want_cube = None
        for c in obs["cubes"]:
            d = math.dist(c["pos"][:2], pos[:2])
            if d < (95 if "greedy" in self.flags else 70) and abs(c["pos"][2] - z) < 4.0 and self._safe_point(zs, c["pos"]):
                if want_cube is None or d < want_cube[0]:
                    want_cube = (d, c["pos"])
        real_threats = [e for e in threats if not e.get("pseudo")]
        finishing = bool(target and target["shoot"] and target["m"]["hp"] <= 0.3 * target["m"]["max_hp"]
                         and target["d"] < 130 and hp_frac > 0.3 and len(real_threats) <= 2)

        # ---- movement decision
        charge = False
        goal = None   # (x, y) point
        mode = "roam"
        boost = False
        outranged = any("laser" in e["m"].get("weapons", []) and e["d"] > 70 for e in threats) and not has_laser
        hurt = hp_frac < 0.38
        if self.retreating and (hp_frac > 0.8 or (t - self.last_dmg_t > 12 and hp_frac > 0.6)):
            self.retreating = False
        if hurt and threats:
            self.retreating = True
        if "brave" in self.flags:  # running to cover costs more HP/s than fighting (27 vs 21.5 in the sim): stay longer
            many = len(threats) >= 4 or (len(threats) >= 3 and hp_frac < 0.6) or dps_in > 160 or (bool(hidden) and hp_frac < 0.6)
        else:
            many = len(threats) >= 3 or (len(threats) >= 2 and hp_frac < 0.6) or dps_in > 110 or bool(hidden)
        close_fight = (target is not None and target["shoot"] and target["d"] < 40 and (has_light or hp_frac > 0.5)) \
            or finishing

        self._searching = "arm" in self.flags and not (has_laser or has_light) and want_pick is None
        if zs["urgent"]:
            mode = "zone"
        elif "arm" in self.flags and want_pick and not (has_laser or has_light) and hp_frac > 0.2 \
                and want_pick[1] < 140 and not (target and target["shoot"] and target["d"] < 25):
            mode = "loot"  # arm first: a laser or lightning is worth more than any gun trade (unarmed = 0.3 pts)
        elif (self.retreating or many or outranged or (hide and not zs["must_go"])) and not close_fight:
            mode = "cover"
        elif zs["must_go"]:
            mode = "zone"
        elif "patient" in self.flags and t < self.hide_until and not threats and not zs["urgent"]:
            mode = "hold"  # just broke line of sight: stay put and listen instead of wandering back out
        elif want_cube and hp_frac > (0.3 if "greedy" in self.flags else 0.35) and (
                want_cube[0] < 10 or target is None or not target["shoot"] or target["d"] > 60) and                 len(real_threats) <= (2 if "greedy" in self.flags else 1):
            mode = "cube"
        elif want_pick and not (has_laser or has_light) and t - self.last_dmg_t > 1.5 and \
                (target is None or target["d"] > 50 or want_pick[1] < 20):
            mode = "loot"  # arm up before taking fights with the gun alone
        elif target and target["shoot"]:
            mode = "fight"
        elif want_pick:
            mode = "loot"
        else:
            mode = "roam"

        self.mode = mode
        if mode == "zone":
            goal = zs["nc"] if zs["nr"] > 0 else zs["nc"]
            boost = zs["urgent"] and me["energy"] > 25 or (zs["need"] > 60 and me["energy"] > 60)
            if want_pick and want_pick[1] < 25:
                goal = want_pick[0]["pos"][:2]
        if mode == "cover":
            th_list = threats or enemies
            keep = False
            if self.cover is not None and self.cover_vis == 0 and self.tick_n - self.cover_calc_tick < 40:
                cp = self.cover
                if math.dist(cp[:2], pos[:2]) > 1.5 and self.tick_n % 3 == 0:
                    keep = self._cover_vis(self._threat_eyes(th_list), (cp[0], cp[1], cp[2] + 7)) == 0
                else:
                    keep = True
            if not keep and (self.tick_n - self.cover_calc_tick >= 4 or self.cover is None):
                res = self._find_cover(pos, th_list, zs, st)
                self.cover, self.cover_vis = res if res else (None, 99)
                self.cover_calc_tick = self.tick_n
            if self.cover is not None and (self.cover_vis == 0 or not (target and target["shoot"])):
                goal = self.cover[:2]
                boost = (t - self.last_dmg_t < 2) and me["energy"] > 30
            elif want_pick and not (has_laser or has_light) and want_pick[1] < 90 \
                    and hp_frac > 0.3:
                mode = "loot"  # no cover here: go and arm up instead of trading with a gun
            elif target and target["shoot"]:
                mode = "fight"  # nowhere to hide: stand and trade
                if outranged and hp_frac > 0.5 and len(real_threats) <= 1:
                    charge = True
            else:
                goal = None
            self.mode = mode
        if mode == "fight":
            d = target["d"]
            pref = (140 if "range" in self.flags else 110) if has_laser else (28 if has_light else 85)
            if has_light and has_laser and d < 60:
                pref = 28
            if finishing:
                pref = 28 if has_light else 55
            if charge:
                pref = 22
                boost = d > 45 and me["energy"] > 35
            mp = target["m"]["pos"]
            ang = math.atan2(mp[1] - y, mp[0] - x)
            if t > self.strafe_until:
                self.strafe_sign = -self.strafe_sign if self.rng.random() < (1.0 if "jink" in self.flags else 0.7) else self.strafe_sign
                self.strafe_until = t + ((0.35 + self.rng.random() * 0.55) if "jink" in self.flags else (1.0 + self.rng.random() * 1.6))
            radial = max(-1.0, min(1.0, (d - pref) / 30.0))
            side = 0.85 * self.strafe_sign
            mvx = math.cos(ang) * radial + math.cos(ang + math.pi / 2) * side
            mvy = math.sin(ang) * radial + math.sin(ang + math.pi / 2) * side
            goal = (x + mvx * 20, y + mvy * 20)
        elif mode == "loot":
            goal = want_pick[0]["pos"][:2]
        elif mode == "cube":
            goal = want_cube[1][:2]
        if mode in ("roam", "fight") and goal is None or mode == "roam":
            goal = self._roam_goal(obs, pos, zs, farm, hp_frac, threats)

        # keep the goal inside the zone we are heading to
        if goal is not None and mode not in ("zone",) and zs["must_go"]:
            if math.dist(goal, zs["nc"]) > zs["nr"]:
                goal = zs["nc"]

        # ---- progress watchdog (jitter in a pocket does not trip the speed-based stuck check)
        if self.tick_n % 25 == 0:
            if self.prog_pos is not None and goal is not None and mode in ("zone", "loot", "cube", "roam", "cover") \
                    and math.dist(goal[:2], pos[:2]) > 20 and math.dist(pos[:2], self.prog_pos) < 5.0 \
                    and t - self.last_dmg_t > 1.5:
                self.force_replan = True
                self.noprog += 1
                if self.noprog >= 2 and me["energy"] > 35:
                    self.climb_until = t + 2.5
                    self.noprog = 0
            else:
                self.noprog = 0
            self.prog_pos = (pos[0], pos[1])
        nav_goal = self._navigate(goal, pos, t) if mode != "fight" else goal

        # ---- steering
        move = [0.0, 0.0]
        goal_raw = goal
        goal = nav_goal
        if goal is not None:
            gdx, gdy = goal[0] - x, goal[1] - y
            gd = math.hypot(gdx, gdy)
            if gd > 1.2:
                gdx, gdy = gdx / gd, gdy / gd
                dx, dy = self._steer(pos, (gdx, gdy), t)
                gd_final = math.hypot(goal_raw[0] - x, goal_raw[1] - y)
                speed = 1.0 if gd_final > 4 else max(0.3, gd_final / 4)
                move = [dx * speed, dy * speed]
                self.prev_dir = (dx, dy)
                # climbing onto roofs for loot
                if mode == "loot" and gd_final < 14 and want_pick[0]["pos"][2] > z + 1.5 and me["energy"] > 20:
                    self.climb_until = max(self.climb_until, t + 0.6)
                # hurry to far loot / cubes when nobody is close (boost drains energy, which regenerates)
                if mode in ("loot", "cube") and gd_final > 30 and (not any(e["d"] < 80 for e in real_threats) or (
                        "arm" in self.flags and mode == "loot" and not (has_laser or has_light))):
                    if self.boosting and me["energy"] > 22 or me["energy"] > 60:
                        boost = True
                        self.boosting = True
                    else:
                        self.boosting = False
                else:
                    self.boosting = False

        if dodge:
            dvx, dvy, dstr, dboost = dodge
            if zs["urgent"]:
                dstr *= 0.5
            if self.world.blocked(x + dvx * 4.0, y + dvy * 4.0, z, 4.0):
                dvx, dvy = -dvx, -dvy
                if self.world.blocked(x + dvx * 4.0, y + dvy * 4.0, z, 4.0):
                    dstr = 0.0
            if dstr > 0:
                mx_ = dvx * dstr + move[0] * (1 - dstr)
                my_ = dvy * dstr + move[1] * (1 - dstr)
                n = math.hypot(mx_, my_) or 1.0
                move = [mx_ / n, my_ / n]
                boost = boost or (dboost and me["grounded"])
                self.prev_dir = (move[0], move[1])

        # quiet movement: at or below step_speed (5 m/s) a mech makes no sound at all
        if "sneak" in self.flags and mode in ("roam", "cube", "hold", "cover") and not boost and dodge is None \
                and not zs["must_go"] and not zs["urgent"] and t - self.last_dmg_t > 3.0 \
                and (enemies or (self.last_sound and t - self.last_sound[0] < 8.0)):
            sc = 4.6 / max(1.0, st.get("walk_speed", 11.7))
            nm = math.hypot(move[0], move[1])
            if nm > sc:
                move = [move[0] / nm * sc, move[1] / nm * sc]

        # stuck detection
        hs = math.hypot(me["vel"][0], me["vel"][1])
        if math.hypot(*move) > 0.3 and hs < 2.5 and me["grounded"]:
            self.stuck_t += 0.1
        else:
            self.stuck_t = max(0.0, self.stuck_t - 0.05)
        jump = False
        if self.stuck_t > 0.6:
            self.stuck_t = 0.0
            ahead_top = self.world.obstacle_top(x + move[0] * 4, y + move[1] * 4)
            if ahead_top - z < 40 and me["energy"] > 35:
                self.climb_until = t + 2.5
            else:
                a = math.atan2(move[1], move[0]) + self.rng.choice((-1, 1)) * (math.pi / 2 + self.rng.random())
                self.detour_dir = (math.cos(a), math.sin(a))
                self.detour_until = t + 1.5
        if t < self.climb_until:
            if me["grounded"]:
                jump = True
            elif me["energy"] > 5:
                boost = True
            if not me["grounded"] and goal is not None:
                gdx, gdy = goal[0] - x, goal[1] - y
                gd = math.hypot(gdx, gdy) or 1.0
                move = [gdx / gd, gdy / gd]
        elif not me["grounded"]:
            boost = False if mode != "zone" else boost

        if boost and me["grounded"] and me["energy"] < 20:
            boost = False

        act["move"] = move
        act["aim"] = [wrap(aim_yaw), max(-0.69, min(1.29, aim_pitch))]
        act["fire"] = fire
        act["boost"] = bool(boost)
        act["jump"] = jump
        self.dbg = {"pos": [round(x), round(y), round(z, 1)], "goal": [round(goal[0]), round(goal[1])] if goal else None, "mv": [round(move[0], 2), round(move[1], 2)], "vel": [round(me["vel"][0], 1), round(me["vel"][1], 1)], "en": round(me["energy"]), "clm": round(self.climb_until - t, 1), "thr": len(threats), "enm": len(enemies), "vis": [bool(e["vis"]) for e in enemies][:3], "outr": outranged, "many": many, "close": close_fight, "ret": self.retreating, "cov": self.cover_vis if self.cover else None}
        return act

    def _roam_goal(self, obs, pos, zs, farm, hp_frac, threats):
        x, y, z = pos
        # cubes first
        best = None
        for c in obs["cubes"]:
            d = math.dist(c["pos"][:2], pos[:2])
            if d < 120 and c["pos"][2] < z + 6 and self._safe_point(zs, c["pos"]):
                if best is None or d < best[0]:
                    best = (d, c["pos"][:2])
        if best and (not threats or best[0] < 20):
            return best[1]
        # supply drops when calm
        if not threats and hp_frac > 0.45:
            for dr in obs.get("drops", []):
                d = math.dist(dr["pos"][:2], pos[:2])
                if d < 170 and dr.get("lands_in", 99) < d / 11.7 + 8 and self._safe_point(zs, dr["pos"]):
                    if dr.get("holder") not in (None, obs["self"]["id"]) and dr.get("progress", 0) > 1.5:
                        continue
                    return dr["pos"][:2]
        if farm:
            d, kind, obj, ctr = farm
            stand = 22 if kind == "crate" else 50
            if d > stand:
                return obj["pos"][:2]
            return None
        # remembered crates (cube + 50% weapon each)
        if hp_frac > 0.6 and not threats and self.known_crates:
            bc = None
            for cid, cp in self.known_crates.items():
                d = math.dist(cp[:2], pos[:2])
                if d < 260 and cp[2] < z + 8 and self._safe_point(zs, cp) and (bc is None or d < bc[0]):
                    bc = (d, cp)
            if bc and bc[0] > 20:
                return bc[1][:2]
        if self._searching:
            g = self._explore_goal(pos, zs, threats, obs["t"])
            if g is not None:
                return g
        if "anchor" in self.flags:
            g = self._anchor(pos, zs, obs["t"])
            if g is not None:
                return g if math.dist(g, pos[:2]) > 3.0 else None
        # drift toward the next zone centre, settle inside it
        nc, nr = zs["nc"], zs["nr"]
        dn = math.dist(pos[:2], nc)
        if dn > max(nr * 0.55, 15):
            return nc
        return None

    def _anchor(self, pos, zs, t):
        """anchor: wait between fights at a spot hugging walls (blocked on many sides) near the centre of the circle we
        want to be in, so the next fight starts with cover in reach instead of in an open field."""
        nc, nr = zs["nc"], zs["nr"]
        a = self.anchor
        if a is not None and t < self.anchor_until and math.dist(a, nc) <= max(nr * 0.6, 20):
            return a
        w = self.world
        if not w.ok:
            return None
        R = min(max(nr * 0.55, 20.0), 140.0)
        best, best_s = None, -1e9
        for k in range(48):
            if self._late(20):
                break
            ang, rr = self.rng.random() * TAU, math.sqrt(self.rng.random()) * R
            px, py = nc[0] + math.cos(ang) * rr, nc[1] + math.sin(ang) * rr
            gz = w.ground(px, py)
            if w.blocked(px, py, gz, 0.0):
                continue
            cov = 0
            for j in range(12):
                aa = j * TAU / 12
                for dd in (5.0, 9.0):
                    if w.blocked(px + math.cos(aa) * dd, py + math.sin(aa) * dd, gz, 0.0):
                        cov += 1
                        break
            if cov >= 11:
                continue  # boxed in: slow to reach and to leave
            s = cov * 10 - math.dist((px, py), pos[:2]) * 0.08 - rr * 0.05
            if s > best_s:
                best, best_s = (px, py), s
        self.anchor = best
        self.anchor_until = t + 20.0
        return best

    def _explore_goal(self, pos, zs, threats, t):
        """Unarmed with no weapon known: walk to fresh ground (away from known threats, inside the circle we want,
        not where we already looked) so the view sweep can spot floor loot."""
        g = self.explore_goal
        if g is not None and (math.dist(g, pos[:2]) < 18 or t > self.explore_until):
            self.looked.append(tuple(g))
            g = None
        if g is None:
            x, y = pos[0], pos[1]
            if threats:
                base = math.atan2(sum(y - e["m"]["pos"][1] for e in threats), sum(x - e["m"]["pos"][0] for e in threats))
            else:
                nc = zs["nc"]
                base = math.atan2(nc[1] - y, nc[0] - x) + (self.rng.random() - 0.5) * 2.0
            best, best_s = None, 1e9
            for k in range(12):
                a = base + (k * TAU / 12 if k < 12 else 0)
                off = abs(wrap(a - base))
                p = (x + math.cos(a) * 110, y + math.sin(a) * 110)
                if not self._safe_point(zs, (p[0], p[1], pos[2])):
                    continue
                s = off * 40 + sum(60 for q in self.looked[-12:] if math.dist(q, p) < 90)
                if s < best_s:
                    best, best_s = p, s
            g = best
            self.explore_goal = g
            self.explore_until = t + 14.0
            self.looked.append((x, y))
        return g

    def _find_cover(self, pos, threats, zs, st):
        if not threats:
            return None
        w = self.world
        x, y, z = pos
        eh = st.get("eye_height", 8)
        th = sorted(threats, key=lambda e: e["d"])[:3]
        teyes = [(e["m"]["pos"][0], e["m"]["pos"][1], e["m"]["pos"][2] + 8 * e["scale"]) for e in th]
        avg_ang = math.atan2(sum(e["m"]["pos"][1] - y for e in th), sum(e["m"]["pos"][0] - x for e in th))
        best, best_s, best_vis = None, 1e9, 99
        prev_s = 0.0
        for k in range(16):
            a = k * TAU / 16
            ca, sa = math.cos(a), math.sin(a)
            prev_s = 0.0
            if self._late(28) and best is not None:
                break
            for dist in ((7.0, 14.0, 22.0, 32.0, 44.0, 58.0, 75.0, 95.0, 120.0) if "farcover" in self.flags
                         else (7.0, 14.0, 22.0, 32.0, 44.0, 58.0, 75.0)):
                if self._late(33) and best is not None:
                    break
                ok = True
                s = prev_s + 3.5
                while s <= dist:
                    if w.blocked(x + ca * s, y + sa * s, z, s):
                        ok = False
                        break
                    s += 3.5
                if not ok:
                    break
                prev_s = dist
                px, py = x + ca * dist, y + sa * dist
                pz = max(w.ground(px, py), z - 6) if w.ok else z
                vis = self._cover_vis(teyes, (px, py, pz + eh * 0.9))
                sc = vis * 100 + dist * 0.8
                # prefer moving away from threats
                sc -= 12 * math.cos(wrap(a - avg_ang + math.pi))
                if math.dist((px, py), zs["nc"]) > zs["nr"] and zs["must_go"]:
                    sc += 150
                elif math.dist((px, py), zs["c"]) > zs["r"] - 5:
                    sc += 300
                if sc < best_s:
                    best, best_s, best_vis = (px, py, pz), sc, vis
        return best, best_vis

    def _cover_vis(self, teyes, p):
        vis = 0
        for te in teyes:
            if self.world.los(te, p, 4.0):
                vis += 1
        return vis

    def _threat_eyes(self, threats):
        th = sorted(threats, key=lambda e: e["d"])[:3]
        return [(e["m"]["pos"][0], e["m"]["pos"][1], e["m"]["pos"][2] + 8 * e["scale"]) for e in th]


if __name__ == "__main__":
    mf.run(Bot)
