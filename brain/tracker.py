"""Awareness agent: keeps track of every enemy the mech has seen, heard, or been shot by, and keeps estimating
where each one is now (dead reckoning from its last speed, with an uncertainty ring that grows while it is out of
sight). It also remembers whether that enemy has seen us. The reasoning agent reads this picture every decision."""
import math
from .world import role, ang_diff

HEARD_KINDS = {"gun": 1, "laser": 1, "laser_charge": 1, "lightning": 1, "mortar": 1, "boost": 1, "jet": 1, "step": 1, "reload": 1}
DIST = {"near": 40.0, "mid": 110.0, "far": 230.0}


class Tracker:
    def __init__(self, world, keep_s=25.0):
        self.w = world
        self.keep_s = keep_s
        self.tracks = {}      # id -> track dict
        self.heard_n = 0

    def update(self):
        w = self.w
        t = w.t
        seen_ids = set()
        for e in w.enemies:
            i = e["id"]
            seen_ids.add(i)
            tr = self.tracks.get(i, {})
            vx, vy = (e.get("vel") or [0, 0, 0])[:2]
            tr.update(id=i, x=e["pos"][0], y=e["pos"][1], z=e["pos"][2], vx=vx, vy=vy, aim=e["aim"][0], seen_t=t,
                      hp=e.get("hp", tr.get("hp", 1400)), max_hp=e.get("max_hp", 1400), weapons=e.get("weapons") or [],
                      role=role(e), heard=False, unc0=2.0)
            if w.sees(e, w.x, w.y, w.z, near_pad=0.0, cone_pad=0.0):
                tr["aware_t"] = t
            try:
                tr["trees"] = bool(w.map is not None and w.map.under_canopy(e["pos"][0], e["pos"][1]))
            except Exception:
                tr["trees"] = False
            self.tracks[i] = tr
        # mechs that shot us from out of sight (the world model drops a ghost along the bearing)
        for i, v in w.known.items():
            if v.get("ghost") and i not in seen_ids:
                tr = self.tracks.get(i)
                if tr is None or tr["seen_t"] < v["t"] - 0.01:
                    tr = tr or {"id": i, "role": "?", "hp": 1400, "max_hp": 1400, "weapons": v.get("weapons", [])}
                    tr.update(x=v["pos"][0], y=v["pos"][1], z=v["pos"][2], vx=0.0, vy=0.0, aim=v["aim"][0], seen_t=v["t"],
                              heard=True, unc0=35.0, aware_t=v["t"])
                    self.tracks[i] = tr
        # sounds: refresh the track along that bearing, or start a 'heard only' track
        for s in w.obs.get("sounds", []):
            if s.get("kind") not in HEARD_KINDS or s.get("bearing") is None:
                continue
            b, d = s["bearing"], DIST.get(s.get("dist"), 110.0)
            px, py = w.x + math.cos(b) * d, w.y + math.sin(b) * d
            best, bd = None, 0.4
            for tr in self.tracks.values():
                if t - tr["seen_t"] > 12:
                    continue
                ex, ey, _, _ = self.est(tr)
                a = abs(ang_diff(math.atan2(ey - w.y, ex - w.x), b))
                if a < bd:
                    best, bd = tr, a
            if best is not None:
                if t - best["seen_t"] > 0.5:   # out of sight: pull the estimate toward the sound
                    ex, ey, _, _ = self.est(best)
                    best.update(x=(ex + px) / 2, y=(ey + py) / 2, vx=0.0, vy=0.0, seen_t=t, unc0=d * 0.3 + 10, heard=True)
            else:
                self.heard_n += 1
                hid = "s%d" % self.heard_n
                self.tracks[hid] = {"id": hid, "x": px, "y": py, "z": w.z, "vx": 0.0, "vy": 0.0, "aim": b + math.pi,
                                    "seen_t": t, "role": "?", "hp": 1400, "max_hp": 1400, "weapons": [], "heard": True,
                                    "unc0": d * 0.35 + 15}
        # our kills leave the picture; old tracks fade out
        for ev in w.events:
            if ev["type"] == "kill":
                self.tracks.pop(ev.get("target"), None)
        for k in [k for k, tr in self.tracks.items() if t - tr["seen_t"] > self.keep_s]:
            del self.tracks[k]
        # heard-only tracks that sit on a seen mech are the same mech
        for k in [k for k, tr in self.tracks.items() if isinstance(k, str)]:
            hx, hy, hu, _ = self.est(self.tracks[k])
            for i in seen_ids:
                s = self.tracks[i]
                if math.hypot(s["x"] - hx, s["y"] - hy) < hu + 10:
                    self.tracks.pop(k, None)
                    break

    def est(self, tr):
        """(x, y, uncertainty m, age s) of where the track is now."""
        age = self.w.t - tr["seen_t"]
        if tr.get("trees"):      # went into canopy: it hides, it doesn't vanish; it moves slowly in there
            dt = min(age, 2.0)
            return tr["x"] + tr["vx"] * dt, tr["y"] + tr["vy"] * dt, tr.get("unc0", 2.0) + 2.5 * age, age
        dt = min(age, 6.0)
        return tr["x"] + tr["vx"] * dt, tr["y"] + tr["vy"] * dt, tr.get("unc0", 2.0) + 5.0 * age, age

    def aware(self, tr, within=6.0):
        return self.w.t - tr.get("aware_t", -99) < within

    def near(self, x, y, r, max_age=10.0, exclude=None):
        out = []
        for k, tr in self.tracks.items():
            if k == exclude:
                continue
            ex, ey, unc, age = self.est(tr)
            if age <= max_age and math.hypot(ex - x, ey - y) <= r + min(unc, 40.0):
                out.append(tr)
        return out

    def view(self):
        rows = []
        for k, tr in self.tracks.items():
            x, y, unc, age = self.est(tr)
            rows.append([k, round(x, 1), round(y, 1), round(age, 1), round(min(unc, 150.0), 1),
                         1 if self.aware(tr) else 0, {"sniper": "s", "brawler": "b", "hunter": "h"}.get(tr.get("role"), "?")])
        return rows
