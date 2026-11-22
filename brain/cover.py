"""Fight from cover, reset to heal, move after a kill.

CoverAgent (mistake M-001: never turn your back in the open): when the fight read says take cover, or we are in a
fight at range with cover close by, it takes a post on a spot hidden from the threats and fights from its SIDE:
  hide   behind the post; with a laser, start charging about 0.8 s before the next shot (a full charge holds 2 s)
  peek   step to the side edge until we have a line, shoot (laser release or a gun burst), back in; at most peek_s,
         and never the same edge twice in a row
  jump   when the target is in laser range but looking away for a while: charged laser, jump at the post, release at
         the top of the jump (0.4 to 0.65 s after take-off), drop back down (wiki: jump-shot-laser)
A ground post is held at most ground_hold_s, then we shift to a new one (shoot and move). High ground (post at least
4 m above the threats) may be held up to high_hold_s while it pays.

HealAgent (wiki: reset-to-heal): hurt, nobody on us: get out of every known line and sit still 5 s so regen starts.
Combat's reposition after a kill lives here too (wiki: reposition-after-kill)."""
import math
from .world import ang_diff, unit
from .agents import Agent, plan, lead_aim, gun_fire_ok


def target_of(w, fid):
    """Enemy dict for an id: visible, remembered (game memory gives the true position for 5 s), or tracker guess."""
    for e in w.enemies:
        if e["id"] == fid:
            return e
    for e in w.recent:
        if e["id"] == fid:
            return e
    tr = w.tracker.tracks.get(fid)
    if tr is not None:
        x, y, unc, age = w.tracker.est(tr)
        if age < 8.0:
            return {"id": fid, "pos": [x, y, w.z], "aim": [tr.get("aim", 0.0), 0.0], "vel": [tr["vx"], tr["vy"], 0],
                    "weapons": tr.get("weapons") or [], "hp": tr.get("hp", 1400), "_d": math.hypot(x - w.x, y - w.y),
                    "guess": True, "age": age}
    return None


class CoverAgent(Agent):
    name = "cover"

    def __init__(self, p, kit):
        super().__init__(p, kit)
        self.post = None        # (x, y, z, high)
        self.post_t = -99.0
        self.fid = None
        self.phase = "move"
        self.phase_t = 0.0
        self.side = 1.0
        self.last_side = 0.0
        self.jump_t = -99.0
        self.away_since = None
        self.shots = 0
        self.bad_posts = []

    # ------------------------------------------------------------------ choosing a post
    def threats(self, w):
        th = [e for e in w.recent if not e.get("ghost")] or list(w.enemies)
        return th[:3]

    def pick_post(self, w, th):
        c = w.find_cover(th, max_r=self.p.get("cover_r", 38.0)) if th else None
        if c is None:
            return None
        for b in self.bad_posts[-4:]:
            if math.dist(b, c) < 6:
                return None
        z = w.ground(c[0], c[1], w.z)
        high = all(z > e["pos"][2] + 4.0 for e in th) if th else False
        return (c[0], c[1], z, high)

    def want_post(self, w):
        """Should we be fighting from cover right now?"""
        st = getattr(w, "stance", None)
        if st == "run":
            return True
        R = getattr(w, "intent", None) or {}
        if R.get("intent") == "fight" and R.get("focus") is not None:
            e = target_of(w, R["focus"])
            if e is not None and e["_d"] > self.p.get("min_range", 45.0) and w.sec != "lightning":
                return True
        return False

    # ------------------------------------------------------------------ propose
    def propose(self, w):
        th = self.threats(w)
        R = getattr(w, "intent", None) or {}
        if self.post is None:
            if not self.want_post(w):
                return None
            self.post = self.pick_post(w, th)
            if self.post is None:
                return None
            self.post_t, self.phase, self.phase_t = w.t, "move", w.t
            self.fid = R["focus"] if R.get("focus") is not None else (th[0]["id"] if th else None)
            w.log("post", x=round(self.post[0]), y=round(self.post[1]), high=self.post[3], tgt=self.fid)
        if R.get("focus") is not None:
            self.fid = R["focus"]
        e = target_of(w, self.fid) if self.fid is not None else (th[0] if th else None)
        if e is None and th:
            e, self.fid = th[0], th[0]["id"]
        hold = self.p.get("high_hold_s", 30.0) if self.post[3] else self.p.get("ground_hold_s", 8.0)
        fresh = any(w.t - tr["seen_t"] < 4.0 for tr in w.tracker.tracks.values())
        compromised = self.compromised(w)
        if e is None or not fresh or w.t - self.post_t > hold or not w.me["in_zone"] or w.zone_urgent or compromised:
            why = "no target" if e is None else ("calm" if not fresh else ("zone" if (not w.me["in_zone"] or w.zone_urgent)
                  else ("seen at the post" if compromised else "held long enough")))
            w.log("post_end", why=why, held=round(w.t - self.post_t, 1), shots=self.shots)
            self.post, self.shots = None, 0
            if why in ("held long enough", "seen at the post"):
                self.bad_posts.append((w.x, w.y))
                nxt = self.pick_post(w, th)          # shoot and move: straight to the next cover
                if nxt is not None:
                    self.post, self.post_t, self.phase, self.phase_t = nxt, w.t, "move", w.t
                    w.log("post", x=round(nxt[0]), y=round(nxt[1]), high=nxt[3], tgt=self.fid, why="next cover")
                    return self.p["bid"], self.fight(w, e, th)
            return None
        return self.p["bid"], self.fight(w, e, th)

    def compromised(self, w):
        """Hiding at the post but someone has a line on us (or is hitting us): the post is no longer cover."""
        if self.phase != "hide" or self.post is None or math.dist(self.post[:2], (w.x, w.y)) > 3.0:
            return False
        if w.t - self.phase_t < 0.4:
            return False
        hit = any(ev["type"] == "damaged" and ev.get("by") is not None and ev["by"] < 100 for ev in w.events)
        seen = any(w.has_line(e, w.x, w.y, w.z) for e in w.recent if not e.get("ghost"))
        return hit or seen

    def fight(self, w, e, th):
        px, py, pz, high = self.post
        ex, ey, ez = e["pos"]
        d = math.hypot(ex - w.x, ey - w.y)
        yaw, pitch, tz = lead_aim(w, e)
        aim = [yaw, pitch]
        sec = w.weapons[1]
        laser = sec is not None and sec["kind"] == "laser"
        ready = laser and sec.get("cooldown", 0) == 0
        charge = (sec.get("charge") or 0) if laser else 0
        full = laser and charge >= (sec.get("stats") or {}).get("charge", 0.75) - 0.01
        at_post = math.dist((px, py), (w.x, w.y)) < 2.5
        line_now = w.los(w.muz, (ex, ey, tz))
        err = abs(ang_diff(w.me["aim"][0], yaw))
        tol = math.atan2(2.2, max(d, 1.0)) + 0.02
        f0 = gun_fire_ok(w, ex, ey, tz, d, yaw) and not e.get("guess")
        # 1. get to the post, facing the threat, shooting back if we have a line
        if not at_post and self.phase == "move":
            mv = list(unit(px - w.x, py - w.y))
            return plan(mv, aim, [f0, False], boost=w.me["energy"] > 15 and not full, note="to post")
        if self.phase == "move":
            self.phase, self.phase_t = "hide", w.t
        # is the target looking away from us? (for the jump-shot)
        facing = abs(ang_diff(math.atan2(w.y - ey, w.x - ex), (e.get("aim") or [0])[0]))
        if facing > self.p.get("away_rad", 1.2):
            self.away_since = self.away_since if self.away_since is not None else w.t
        else:
            self.away_since = None
        away_long = self.away_since is not None and w.t - self.away_since > self.p.get("away_s", 1.0)
        # 2. jump-shot: charged laser, target in range and looking away
        if self.phase == "jump":
            dt = w.t - self.jump_t
            release = 0.4 <= dt <= 0.7 and err < tol * 1.5
            if dt > 1.0:
                self.phase, self.phase_t = "hide", w.t
                self.shots += 1
            return plan([0.0, 0.0], aim, [f0 and dt > 0.3, laser and not release], note="jump-shot")
        if laser and full and d < 270 and away_long and at_post and w.me["grounded"]:
            self.phase, self.jump_t = "jump", w.t
            w.log("jumpshot", tgt=e["id"], d=round(d), facing=round(facing, 2))
            return plan([0.0, 0.0], aim, [False, True], jump=True, note="jump-shot")
        # 3. peek from the side
        if self.phase == "peek":
            dt = w.t - self.phase_t
            if line_now:
                fire1 = laser and not (full and err < tol)
                shot = laser and full and err < tol
                if shot or (not laser and dt > 0.6):
                    self.shots += 1
                done = dt > self.p.get("peek_s", 1.2) or (laser and charge == 0 and dt > 0.3 and sec.get("cooldown", 0) > 0)
                if done:
                    self.phase, self.phase_t, self.last_side = "hide", w.t, self.side
                return plan([0.0, 0.0], aim, [f0, fire1], note="peek")
            if dt > self.p.get("peek_s", 1.2) + 0.6:
                self.phase, self.phase_t, self.last_side = "hide", w.t, self.side
                self.side = -self.side
            ux, uy = unit(ex - px, ey - py)
            sx, sy = -uy * self.side, ux * self.side
            return plan([sx, sy], aim, [False, laser and ready], note="step out")
        # 4. hide: back to the post, charge the laser, wait for the next peek
        mv = list(unit(px - w.x, py - w.y)) if not at_post else [0.0, 0.0]
        dt = w.t - self.phase_t
        gap = self.p.get("hide_s", 0.9)
        charging = laser and ready and dt > gap - 0.85
        if dt > gap and (not laser or full or not ready):
            self.phase, self.phase_t = "peek", w.t
            self.side = -self.last_side if self.last_side else (1.0 if (w.tick // 20) % 2 else -1.0)
        return plan(mv, aim, [False, charging], note="cover" + (" high" if high else ""))


class HealAgent(Agent):
    """Reset to heal: hurt and nobody on us -> out of every known line, stay still 5 s, let regen run."""
    name = "heal"

    def __init__(self, p, kit):
        super().__init__(p, kit)
        self.spot = None

    def propose(self, w):
        me = w.me
        if me["hp"] >= self.p.get("heal_frac", 0.75) * me["max_hp"] or w.alerted or w.zone_urgent or not me["in_zone"]:
            self.spot = None
            return None
        th = [e for e in w.recent if not e.get("ghost")]
        if not th:
            for tr in w.tracker.tracks.values():
                x, y, unc, age = w.tracker.est(tr)
                if age < 10 and math.hypot(x - w.x, y - w.y) < 220:
                    th.append({"id": tr["id"], "pos": [x, y, w.z], "aim": [tr.get("aim", 0.0), 0.0], "vel": [0, 0, 0],
                               "weapons": tr.get("weapons") or []})
        hidden = not any(w.has_line(e, w.x, w.y, w.z) for e in th)
        if hidden:
            self.spot = None
            aim = [w.threat_yaw, 0.0] if w.threat_yaw is not None else None
            return self.p["bid"], plan([0.0, 0.0], aim, note="reset: healing")
        if self.spot is None or any(w.has_line(e, self.spot[0], self.spot[1], w.z) for e in th):
            self.spot = w.find_cover(th[:3], max_r=38.0)
        if self.spot is None:
            return None
        mv = list(unit(self.spot[0] - w.x, self.spot[1] - w.y))
        return self.p["bid"], plan(mv, None, boost=w.me["energy"] > 20, note="reset: to cover")


class MoveAfterKillAgent(Agent):
    """Reposition after a kill: the shots told everyone where we are. Move 30 m off the line, toward cover."""
    name = "reposition"

    def __init__(self, p, kit):
        super().__init__(p, kit)
        self.until = -1.0
        self.goal = None

    def propose(self, w):
        if any(ev["type"] == "kill" for ev in w.events):
            self.until = w.t + self.p.get("move_s", 3.0)
            a = (w.me["aim"][0] + (math.pi / 2 if (w.tick // 7) % 2 else -math.pi / 2))
            self.goal = (w.x + math.cos(a) * 30.0, w.y + math.sin(a) * 30.0)
        if w.t > self.until or self.goal is None or w.alerted:
            return None
        mv = list(w.goto(*self.goal))
        m2 = w.unseen_step(mv)
        return self.p["bid"], plan(m2 if m2 != [0.0, 0.0] else mv, None, boost=w.me["energy"] > 30, note="move after kill")


class HoldAgent(Agent):
    """Practice: outlast the house bots (rival study 2026-11-21: top practice bots hold a strong spot far from everyone
    at full HP; place = how many house bots are alive when we die, and the final zone kills them too).
    Pick a spot in the next circle, far from every known enemy, high if possible; get there unseen; hold and watch;
    move when the circle moves, someone comes within near_r, or the spot has been held hold_s."""
    name = "hold"

    def __init__(self, p, kit):
        super().__init__(p, kit)
        self.spot = None
        self.spot_t = -99.0
        self.circle = None
        self.scan = 0.0
        self.rng = __import__("random").Random(11)

    def score(self, w, x, y):
        z = w.ground(x, y, w.z)
        near = 400.0
        for tr in w.tracker.tracks.values():
            tx, ty, unc, age = w.tracker.est(tr)
            if age < 15:
                near = min(near, math.hypot(tx - x, ty - y))
        return min(near, self.p.get("far_r", 160.0)) + (z - w.z) * self.p.get("height_w", 3.0), near

    def pick(self, w):
        nc, nr = w.zone["next_center"], w.zone["next_radius"]
        best, bs = None, -1e9
        for _ in range(28):
            a = self.rng.random() * 2 * math.pi
            r = nr * self.p.get("ring", 0.6) * math.sqrt(self.rng.random())
            x, y = nc[0] + math.cos(a) * r, nc[1] + math.sin(a) * r
            if not w.walkable(x, y):
                continue
            s, near = self.score(w, x, y)
            s -= 0.15 * math.hypot(x - w.x, y - w.y)
            if s > bs:
                best, bs = (x, y), s
        return best

    def propose(self, w):
        R = getattr(w, "intent", None) or {}
        if w.alerted or w.enemies or R.get("intent") not in ("rotate", None) or w.zone_urgent or not w.me["in_zone"]:
            return None
        if w.t < self.p.get("start_s", 20.0):
            return None
        nc = tuple(w.zone["next_center"])
        close = w.tracker.near(w.x, w.y, self.p.get("near_r", 120.0), max_age=6.0)
        if (self.spot is None or self.circle is None or math.dist(self.circle, nc) > 5 or close
                or w.t - self.spot_t > self.p.get("hold_s", 60.0)
                or math.dist(self.spot, nc) > w.zone["next_radius"] * 0.8):
            sp = self.pick(w)
            if sp is None:
                return None
            if self.spot is None or math.dist(sp, self.spot) > 10:
                w.log("hold", x=round(sp[0]), y=round(sp[1]), why="close" if close else "new circle or time")
            self.spot, self.spot_t, self.circle = sp, w.t, nc
        d = math.dist(self.spot, (w.x, w.y))
        if d > 4:
            mv = list(w.goto(*self.spot))
            m2 = w.unseen_step(mv)
            mv = m2 if m2 != [0.0, 0.0] else mv
            return self.p["bid"], plan(mv, [math.atan2(mv[1], mv[0]), 0.0] if mv != [0.0, 0.0] else None,
                                       boost=d > 60 and w.me["energy"] > 50, note="to hold spot")
        # hold: watch where trouble comes from (latest threat bearing, else nearest track, else sweep)
        self.scan += 0.25
        tgt = None
        best = 1e9
        for tr in w.tracker.tracks.values():
            tx, ty, unc, age = w.tracker.est(tr)
            dd = math.hypot(tx - w.x, ty - w.y)
            if age < 20 and dd < best:
                best, tgt = dd, (tx, ty)
        if w.t < w.threat_until and w.threat_yaw is not None:
            yaw = w.threat_yaw
        elif tgt is not None:
            yaw = math.atan2(tgt[1] - w.y, tgt[0] - w.x) + math.sin(self.scan) * 0.9
        else:
            yaw = self.scan
        return self.p["bid"], plan([0.0, 0.0], [yaw, 0.0], note="holding")


class ThirdPartyAgent(Agent):
    """Free-for-all only (wiki: third-party). Listens for a fight (brain/sound.py places each fighter from crossed
    bearings and sound ranges), flanks to the side of the second party out of both fighters' view, and commits when
    the timing is right (both locked, one cracked, or one just died) (brain/thirdparty.py)."""
    name = "thirdparty"

    def propose(self, w):
        if not w.ffa or getattr(w, "soundmap", None) is None:
            return None
        from .thirdparty import third_party_plan
        plan_ = third_party_plan(w, w.soundmap, self.p)
        if not plan_:
            return None
        tgt = plan_.get("target_side") or {}
        tx, ty = tgt.get("x", w.x), tgt.get("y", w.y)
        mv = plan_["move"]
        aim = [math.atan2(ty - w.y, tx - w.x), 0.0]
        fire = [False, False]
        tid = tgt.get("track_id")
        e = next((m for m in w.enemies if m["id"] == tid), None) if tid is not None else None
        if plan_.get("fire_ok") and e is not None:
            yaw, pitch, tz = lead_aim(w, e)
            aim = [yaw, pitch]
            fire[0] = gun_fire_ok(w, e["pos"][0], e["pos"][1], tz, e["_d"], yaw)
        commit = plan_.get("approach") == "commit"
        bid = self.p.get("bid_commit", 0.9) if commit else self.p.get("bid", 0.7)
        if not hasattr(self, "_last") or self._last != plan_.get("approach"):
            self._last = plan_.get("approach")
            w.log("third", approach=self._last, why=str(plan_.get("why", ""))[:80], d=round(plan_.get("d_target") or 0))
        return bid, plan(list(mv), aim, fire, boost=not commit and w.me["energy"] > 40, note="3rd " + str(plan_.get("approach")))

