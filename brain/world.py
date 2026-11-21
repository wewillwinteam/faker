"""World model for the mech brain: one place that reads the observation and answers the questions every agent asks.
Who can see me? Who has a line on me? How likely am I to stay unseen if I stand there? Is there a way out?
Standard library only."""
import math

EYE, CHEST = 8.0, 5.0


def ang_diff(a, b):
    return (a - b + math.pi) % (2 * math.pi) - math.pi


def unit(dx, dy):
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-6 else (0.0, 0.0)


def role(e):
    """Guess what kind of bot it is from what it carries (the observation has no bot type)."""
    w = set(e.get("weapons") or [])
    if "laser" in w:
        return "sniper"     # holds range, keeps long lanes open
    if "lightning" in w or "mortar" in w:
        return "brawler"    # closes in
    return "hunter"         # gun only: closes to mid range


class World:
    def __init__(self, hello, nav_map):
        cfg = hello["config"]
        v = cfg.get("vision", {})
        self.cfg = cfg
        self.near = v.get("near_range", 45.0)
        self.vrange = v.get("range", 200.0)
        self.fov_half = math.radians(v.get("fov_deg", 120)) / 2
        self.memory = v.get("memory_s", 5.0)
        self.reach = cfg["loot"]["pickup_radius"]
        self.map = nav_map
        self.flow_target = None
        self.known = {}          # enemy id -> last seen copy
        self.spotted_t = -99.0
        self.dmg_t = -99.0       # last damage from a mech
        self.threat_yaw = None
        self.threat_until = -1.0
        self.kills = 0
        self.last_alive = None
        self.unexplained = 0
        self.ffa = False
        self.events = []
        self._tree_cache = {}
        self.mode_p = {}

    # ---------------------------------------------------------------- geometry
    def los(self, a, b):
        if self.map is None:
            return True
        try:
            return self.map.los(a, b)
        except Exception:
            return True

    def ground(self, x, y, z_hint):
        if self.map is None:
            return z_hint
        try:
            g = self.map.ground(x, y)
            return z_hint if z_hint > g + 3 else g
        except Exception:
            return z_hint

    def walkable(self, x, y):
        if self.map is None:
            return True
        try:
            return self.map.blk[self.map.wcell(x, y)] != 1
        except Exception:
            return True

    def goto(self, tx, ty):
        x, y = self.x, self.y
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

    # ---------------------------------------------------------------- vision
    def sees(self, e, px, py, pz, near_pad=3.0, cone_pad=0.15):
        """Would enemy e see a mech standing at (px, py, pz feet)?"""
        ex, ey, ez = e["pos"]
        dx, dy = px - ex, py - ey
        d = math.hypot(dx, dy)
        if d > self.vrange + 5:
            return False
        far = d > self.near + near_pad
        if far and abs(ang_diff(math.atan2(dy, dx), e["aim"][0])) > self.fov_half + cone_pad:
            return False
        a, b = (ex, ey, ez + EYE), (px, py, pz + CHEST)
        if not self.los(a, b):
            return False
        return self.tree_sight(a, b, far)

    def tree_sight(self, a, b, far=True):
        """Trees: trunks always block sight; canopy only beyond 45 m (proximity sensors ignore canopy)."""
        if self.map is None or not getattr(self.map, "tgrid", None):
            return True
        if not far:
            return True
        key = (int(a[0] // 4), int(a[1] // 4), int(a[2] // 4), int(b[0] // 4), int(b[1] // 4), int(b[2] // 4))
        c = self._tree_cache
        if key in c:
            return c[key]
        try:
            v = self.map.tree_sight(a, b)
        except Exception:
            v = True
        if len(c) > 4000:
            c.clear()
        c[key] = v
        return v

    def has_line(self, e, px, py, pz, rng=230.0):
        ex, ey, ez = e["pos"]
        if math.hypot(px - ex, py - ey) > rng:
            return False
        return self.los((ex, ey, ez + EYE), (px, py, pz + CHEST))

    def p_seen_by(self, e, px, py, pz):
        """Chance enemy e notices a mech at that spot in the next second or so."""
        ex, ey, ez = e["pos"]
        d = math.hypot(px - ex, py - ey)
        if d > 230 or not self.has_line(e, px, py, pz):
            return 0.0
        if d <= self.near + 5:
            return 0.95
        if d > self.vrange:
            return 0.03
        if not self.tree_sight((ex, ey, ez + EYE), (px, py, pz + CHEST)):
            return 0.05          # dense canopy between us: concealed (shots still pass)
        off = abs(ang_diff(math.atan2(py - ey, px - ex), e["aim"][0]))
        if off <= self.fov_half:
            return 0.9
        # outside the cone: it may turn toward us; closer and nearer the cone edge = likelier
        return 0.45 * math.exp(-(off - self.fov_half) / 0.6) * (1.0 - d / 260.0)

    def p_undetected(self, px, py, pz, ignore=None):
        """Chance that no remembered enemy (except `ignore`) notices us at that spot."""
        p = 1.0
        for e in self.recent:
            if e["id"] == ignore:
                continue
            p *= 1.0 - self.p_seen_by(e, px, py, pz)
        # mechs we have not seen at all: a small flat risk per one unaccounted for
        unseen = max(0, self.alive - 1 - len(self.recent))
        return p * (0.995 ** unseen)

    def others_with_line(self, px, py, pz, ignore=None):
        return [e for e in self.recent if e["id"] != ignore and self.has_line(e, px, py, pz)]

    def predict(self, e, dt=1.5):
        ex, ey, ez = e["pos"]
        vx, vy = (e.get("vel") or [0, 0, 0])[:2]
        r = role(e)
        if r == "sniper":
            return ex + vx * dt * 0.5, ey + vy * dt * 0.5, ez
        d = math.hypot(self.x - ex, self.y - ey)
        hold = 30.0 if r == "brawler" else 70.0
        if d > hold:
            ux, uy = unit(self.x - ex, self.y - ey)
            step = min(d - hold, 11.7 * dt)
            return ex + ux * step, ey + uy * step, ez
        return ex + vx * dt, ey + vy * dt, ez

    def unseen_step(self, mv, watchers=None, ignore=None, look=4.0):
        """Turn a move so the next few meters stay out of every watcher's view; (0, 0) if every heading is seen."""
        watchers = self.watchers if watchers is None else watchers
        if not watchers or (mv[0] == 0 and mv[1] == 0):
            return list(mv)
        x, y, z = self.x, self.y, self.z
        base = math.atan2(mv[1], mv[0])
        L = min(1.0, math.hypot(mv[0], mv[1]))
        for off in (0.0, 0.5, -0.5, 1.0, -1.0, 1.5, -1.5):
            a = base + off
            px, py = x + math.cos(a) * look, y + math.sin(a) * look
            if not self.walkable(px, py):
                continue
            pz = self.ground(px, py, z)
            if not any(self.sees(w, px, py, pz) for w in watchers if w["id"] != ignore):
                return [math.cos(a) * L, math.sin(a) * L]
        return [0.0, 0.0]

    def find_cover(self, threats, max_r=38.0):
        """Nearest walkable spot hidden from every threat (now and where it will be in 1.5 s)."""
        x, y, z = self.x, self.y, self.z
        nc, nr = self.zone["next_center"], self.zone["next_radius"]
        pts = []
        for e in threats[:3]:
            px, py, pz = self.predict(e)
            pts.append((e, (e["pos"][0], e["pos"][1], e["pos"][2] + EYE), (px, py, pz + EYE)))
        best, bs = None, 1e9
        for r in (12.0, 24.0, 38.0, 50.0, 60.0):
            if r > max_r:
                break
            for k in range(12):
                a = k * math.pi / 6
                cx, cy = x + math.cos(a) * r, y + math.sin(a) * r
                if not self.walkable(cx, cy):
                    continue
                cz = self.ground(cx, cy, z) + CHEST
                if any(self.los(now, (cx, cy, cz)) or self.los(nxt, (cx, cy, cz)) for _, now, nxt in pts):
                    continue
                s = r
                for e, now, _ in pts:
                    s -= (0.35 if role(e) == "brawler" else 0.15) * math.hypot(cx - now[0], cy - now[1])
                dz = math.hypot(cx - nc[0], cy - nc[1])
                if dz > nr:
                    s += (dz - nr) * 0.5
                if s < bs:
                    best, bs = (cx, cy), s
            if best is not None:
                break
        return best

    # ---------------------------------------------------------------- per-tick update
    def update(self, obs):
        me = obs["self"]
        self.obs, self.me = obs, me
        self.x, self.y, self.z = me["pos"]
        self.t, self.tick = obs["t"], obs["tick"]
        self.stats = me.get("stats") or {}
        self.walk = self.stats.get("walk_speed", 11.7)
        self.muz = (self.x, self.y, self.z + self.stats.get("muzzle_height", 7.0))
        self.weapons = me["weapons"]
        self.held = {w["kind"] for w in self.weapons if w}
        self.alive = obs["alive"]
        self.events = obs["events"]
        sec = self.weapons[1]
        self.sec = sec["kind"] if sec else None
        self.lrange = (sec.get("stats") or {}).get("range", 45.0) if sec and sec["kind"] == "lightning" else None

        kills_now = 0
        for ev in obs["events"]:
            if ev["type"] == "kill":
                self.kills += 1
                kills_now += 1
            elif ev["type"] == "damaged":
                if ev.get("by") is not None and ev["by"] < 100:
                    self.dmg_t = self.t
                if ev.get("bearing") is not None:
                    self.threat_yaw, self.threat_until = ev["bearing"], self.t + 3.0
                # hit by a mech we cannot see: remember a ghost threat out along the bearing
                by = ev.get("by")
                if by is not None and by < 100 and ev.get("bearing") is not None and not any(m["id"] == by for m in obs["mechs"]):
                    b = ev["bearing"]
                    gd = 60.0 if ev.get("weapon") == "lightning" else 120.0
                    prev = self.known.get(by)
                    if prev is None or prev.get("ghost"):
                        self.known[by] = {"id": by, "kind": "mech", "ghost": True, "t": self.t,
                                          "pos": [self.x + math.cos(b) * gd, self.y + math.sin(b) * gd, self.z],
                                          "aim": [b + math.pi, 0.0], "vel": [0, 0, 0], "weapons": [ev.get("weapon") or "gun"],
                                          "hp": 1400, "max_hp": 1400}
        if self.last_alive is not None and self.alive < self.last_alive:
            drop = self.last_alive - self.alive
            if drop > kills_now:
                self.unexplained += drop - kills_now
                if self.unexplained >= 2:
                    self.ffa = True
        self.last_alive = self.alive

        # zone and flow field toward the next circle (a slice per tick)
        self.zone = z = obs["zone"]
        nc = z["next_center"]
        if self.map is not None and (self.flow_target is None or math.dist(self.flow_target, nc) > 5):
            if self.map._job is None or math.dist(self.map._job[2], nc) > 5:
                self.map.start_flow(nc[0], nc[1])
            self.map.step_flow(4000)
            if self.map._job is None:
                self.flow_target = tuple(nc)
        self.d_next = math.dist((self.x, self.y), nc)
        need = max(0.0, self.d_next - z["next_radius"] * 0.6) / max(self.walk, 1.0)
        self.zone_urgent = (not me["in_zone"]) or (self.t + need + 12 > z["shrink_start"] and self.d_next > z["next_radius"] * 0.75)
        self.zone_edge = math.dist((self.x, self.y), z["center"]) > z["radius"] - 15

        # enemies: visible now, plus memory
        self.enemies = [m for m in obs["mechs"] if m["kind"] == "mech"]
        self.npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        pm = self.__dict__.setdefault("pickup_mem", {})
        for p in obs.get("pickups") or []:
            pm[p["id"]] = (p["pos"], p["kind"], self.t)
        for ev in obs["events"]:
            if ev["type"] in ("took", "take_failed"):
                pm.pop(ev.get("id"), None)
        for key in [k for k, v in pm.items() if math.dist(v[0][:2], (self.x, self.y)) < 6 and self.t - v[2] > 1.0]:
            pm.pop(key, None)          # stood on its spot and it's not there: gone
        fm = self.__dict__.setdefault("farm_mem", {})
        for n in self.npcs:
            fm[("n", n["id"])] = (n["pos"], "npc", self.t)
        for c in obs.get("crates") or []:
            fm[("c", c["id"])] = (c["pos"], "crate", self.t)
        for ev in obs["events"]:
            if ev["type"] in ("kill", "destroyed", "crate"):
                for key in [k for k in fm if k[1] == ev.get("target") or k[1] == ev.get("id")]:
                    fm.pop(key, None)
        for key in [k for k, v in fm.items() if math.dist(v[0][:2], (self.x, self.y)) < 25 and self.t - v[2] > 2]:
            fm.pop(key, None)          # we're there and it's gone
        for e in self.enemies:
            self.known[e["id"]] = dict(e, t=self.t)
        for k in [k for k, v in self.known.items() if self.t - v["t"] > self.memory]:
            del self.known[k]
        self.recent = [dict(v) for v in self.known.values() if self.t - v["t"] < 3.0]
        for e in self.enemies + self.recent:
            e["_d"] = math.dist(e["pos"][:2], (self.x, self.y))
        self.enemies.sort(key=lambda m: m["_d"])
        self.recent.sort(key=lambda m: m["_d"])
        self.watchers = [e for e in self.recent if e["_d"] < self.vrange + 40][:4]
        self.seen_by = [e for e in self.enemies if self.sees(e, self.x, self.y, self.z, near_pad=0.0, cone_pad=0.0)]
        if self.seen_by:
            self.spotted_t = self.t
        self.alerted = (self.t - self.spotted_t < 2.5) or (self.t - self.dmg_t < 2.5)
        # squad vision: once we are spotted, everyone with a clear line counts as a shooter
        self.shooters = [e for e in self.recent if e.get("ghost") or self.has_line(e, self.x, self.y, self.z)] if self.alerted else []
