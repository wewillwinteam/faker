"""Reasoning agent: reads the awareness picture and picks ONE intent for the whole mech, with a reason, a single
focus target it sticks with, and whether it may open fire.

Intents: get weapon | rotate (cover ground, never camp) | stalk (close in unseen on the focus) |
         strike (focus is killable fast: fire) | fight (we are found: kill the focus) | get out (stance says run)
Trigger discipline: fire only when the estimated time to kill the focus at this range is short (ttk_max), few other
enemies would be drawn in, and (unless we are already found) the chance no one else notices is high or a way out
exists. Otherwise close the distance first. Early in the match: weapon first, cover ground, skip all but easy kills."""
import math
from .world import ang_diff


class Reasoner:
    def __init__(self, p):
        self.p = p
        self.focus = None
        self.intent = "rotate"
        self.why = ""
        self.out = {}

    # ------------------------------------------------------------------ kill math
    def dps(self, w, d, rear):
        mult = 1.0 + 0.04 * min(w.me.get("cubes", 0), 10)
        total = 0.0
        for wpn in w.weapons:
            if not wpn:
                continue
            st = wpn.get("stats") or {}
            k = wpn["kind"]
            if k == "gun" and d <= st.get("range", 230):
                acc = max(0.2, min(1.0, 1.15 - d / 220.0))     # spread + target strafing at range
                total += st.get("damage", 45) / max(st.get("cooldown", 0.24), 0.05) * acc * 0.8   # 0.8: reloads
            elif k == "laser" and d <= st.get("range", 280):
                total += st.get("damage", 315) / (st.get("charge", 0.75) + st.get("cooldown", 1.65)) * 0.9
            elif k == "lightning" and d <= st.get("range", 45) - 1:
                total += st.get("damage", 60) / max(st.get("cooldown", 0.25), 0.05)
            elif k == "mortar" and 25 < d < 225:
                total += (st.get("damage", 200) + 0.5 * st.get("splash", 160)) / st.get("cooldown", 2.6) * 0.35
        if rear:
            total *= 1.5
        return total * mult

    def ttk(self, w, tr, d, rear):
        dps = self.dps(w, d, rear)
        return (tr.get("hp") or 1400) / dps if dps > 0 else None

    def kill_range(self, w, tr, rear=True):
        """Farthest distance at which the focus dies inside ttk_max (from behind)."""
        best = None
        for d in (25, 35, 44, 55, 70, 90, 110, 140, 170, 200):
            k = self.ttk(w, tr, d, rear)
            if k is not None and k <= getattr(self, "cur_p", self.p)["ttk_max"] * 0.85:
                best = d
        return best

    # ------------------------------------------------------------------ decide
    def decide(self, w, trk, stance):
        # practice (squad) and free-for-all play differently: modes.<squad|ffa> overrides the base numbers
        p = dict(self.p, **self.p.get("modes", {}).get("ffa" if w.ffa else "squad", {}))
        self.cur_p = p
        t = w.t
        intent, why, focus, ttk, fire_ok = "rotate", "", None, None, False
        visible = {e["id"]: e for e in w.enemies}
        if self.focus is not None:
            tr = trk.tracks.get(self.focus)
            if tr is None or trk.est(tr)[3] > 4.0:
                self.focus = None

        def look(e):
            ex, ey, _ = e["pos"]
            d = e["_d"]
            behind = abs(ang_diff(math.atan2(w.y - ey, w.x - ex), e["aim"][0])) > 1.75
            unaware = not w.sees(e, w.x, w.y, w.z, near_pad=0.0, cone_pad=0.0)
            k = self.ttk(w, trk.tracks.get(e["id"], e), d, behind or unaware)
            crowd = len(trk.near(ex, ey, p["crowd_r"], max_age=8.0, exclude=e["id"]))
            return d, behind, unaware, k, crowd

        if stance == "run":
            intent, why = "get out", "the fight read says: to cover, fight from its side"
        elif w.alerted and w.shooters:
            shooters = [e for e in w.shooters if e["id"] in visible] or [e for e in w.enemies if e["_d"] < 120]
            if shooters:
                rear_by = [ev.get("by") for ev in w.events if ev["type"] == "damaged" and ev.get("rear") and ev.get("by") in visible]
                if rear_by:                     # M-005: whoever hits us from behind becomes the focus
                    self.focus = rear_by[-1]
                elif self.focus not in [e["id"] for e in shooters]:
                    self.focus = min(shooters, key=lambda e: (look(e)[3] or 99))["id"]
                e = visible.get(self.focus)
                if e is not None:
                    d, behind, unaware, ttk, crowd = look(e)
                    fire_ok = ttk is not None and ttk <= p["ttk_max"] * 2.0 or d < 60
                    intent, why = "fight", f"#{e['id']} is shooting us, {d:.0f} m"
                    focus = e["id"]
            if focus is None:
                intent, why = "rotate", "hit, attacker out of sight: keep moving"
        else:
            cands = []
            for e in w.enemies:
                d, behind, unaware, k, crowd = look(e)
                if k is None:
                    k = 99.0
                s = k + 2.5 * crowd + (0.0 if e["id"] == self.focus else 1.5)
                cands.append((s, e, d, behind, unaware, k, crowd))
            cands.sort(key=lambda c: c[0])
            early = t < p["quiet_until"]
            if w.sec is None and t < p["weapon_rush_s"]:
                easy = [c for c in cands if c[5] <= p["ttk_easy"] and c[4] and c[6] == 0]
                if easy:
                    _, e, d, behind, unaware, ttk, crowd = easy[0]
                    focus, intent, fire_ok = e["id"], "strike", True
                    why = f"easy kill on #{e['id']} ({ttk:.1f} s) before the weapon"
                else:
                    intent, why = "get weapon", "no second weapon yet"
            elif cands:
                _, e, d, behind, unaware, ttk, crowd = cands[0]
                focus = e["id"]
                quick = ttk <= p["ttk_max"]
                quiet = crowd <= p["max_crowd"]
                hidden = unaware or behind
                if quick and quiet and hidden:
                    pu = w.p_undetected(w.x, w.y, w.z, ignore=e["id"])
                    if pu >= p["engage_p"] or w.find_cover([e], max_r=30.0) is not None:
                        fire_ok = True
                if early and not (fire_ok and ttk <= p["ttk_easy"] * 1.5):
                    intent, why, fire_ok = "rotate", f"early: skip #{e['id']} ({ttk:.1f} s to kill), keep moving", False
                    focus = None
                elif fire_ok:
                    intent, why = "strike", f"#{e['id']} dies in {ttk:.1f} s from here, {crowd} near it"
                elif not hidden and d < p.get("fight_close", 70.0):
                    intent, why, fire_ok = "fight", f"#{e['id']} sees us at {d:.0f} m: take the fight", True
                else:
                    reason = "too slow" if not quick else ("too crowded" if not quiet else "it can see us")
                    intent, why = "stalk", f"close in on #{e['id']}: {reason} ({ttk:.1f} s, {d:.0f} m)"
            else:
                intent, why = "rotate", "no one in sight: cover ground"
        self.focus = focus
        changed = intent != self.intent or focus != self.out.get("focus")
        self.intent, self.why = intent, why
        self.out = {"intent": intent, "why": why, "focus": focus, "ttk": round(ttk, 1) if ttk is not None and ttk < 99 else None,
                    "fire_ok": bool(fire_ok)}
        return self.out, changed
