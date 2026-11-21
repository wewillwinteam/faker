"""Fight read: stay and fight, or take cover? Decided from the awareness picture, never from an HP line.

It plays the fight forward: everyone with a line on us shoots, enemies that know where we are and are close
(join_r) join after their travel time, and we kill them one at a time, quickest first.

Mistake M-001 (variant L ran from even fights and died with its back turned, 14 of 14 mech deaths mid-run):
  - we win, or it's close            -> STAY and fight
  - we lose, cover reachable in time -> RUN = go to cover and fight from its side (brain/cover.py), never flee open
  - we lose, no cover in reach       -> STAY: turning our back is worse than trading
  - 3+ with a line and cover near    -> RUN to cover
  - hit by someone we can't see      -> RUN to cover facing the shot, if cover is near; else stay and turn to it
"Reachable in time" = we get there before half the time it takes them to kill us (38 m; 60 m tested worse: 1.17 vs 1.38 over 96).

Enemy damage is guessed from what each one carries (house bots: sniper = laser, brawler = lightning or mortar,
hunter = gun)."""
import math

GUN, LASER, LIGHT, MORTAR = 45 / 0.24, 315 / 2.4, 60 / 0.25, 280 / 2.6


def enemy_dps(weapons, d):
    """Rough damage per second one enemy puts on us at distance d."""
    ws = set(weapons or []) or {"gun"}
    total = 0.0
    if d <= 230:
        total += GUN * max(0.2, min(1.0, 1.15 - d / 220.0)) * 0.7
    if "laser" in ws and d <= 280:
        total += LASER * 0.8
    if "lightning" in ws and d <= 50:
        total += LIGHT
    if "mortar" in ws and 25 < d < 225:
        total += MORTAR * 0.3
    return total


def cover_reach(w, threats, t_die):
    """(cover point or None, seconds to get there). Cover counts only if we get there before half of t_die."""
    if not threats:
        return None, 0.0
    c = w.find_cover(threats[:3], max_r=38.0)
    if c is None:
        return None, 0.0
    spd = 19.5 if w.me["energy"] > 20 else w.walk
    tr = math.dist(c, (w.x, w.y)) / max(spd, 1.0)
    return (c if tr < 0.5 * t_die else None), tr


def read(w, trk, reasoner, p):
    """-> dict(verdict 'run'|'stay'|None, why, margin, shooters, joining, cover)."""
    sh = list(w.shooters)
    hit_recently = w.t - w.dmg_t < 2.0
    if not w.alerted or not (sh or hit_recently):
        return {"verdict": None, "why": "no one on us", "margin": None, "shooters": 0, "joining": 0}
    seen = [e for e in sh if not e.get("ghost")]
    hp = w.me["hp"]
    if not seen:
        ghosts = [e for e in sh if e.get("ghost")]
        c, tr = cover_reach(w, ghosts, hp / 150.0)
        if c is not None:
            return {"verdict": "run", "why": "hit by someone we can't see: to cover, facing it", "margin": 0.0,
                    "shooters": len(sh), "joining": 0, "cover": True}
        return {"verdict": "stay", "why": "hit by someone we can't see, no cover near: turn and find it", "margin": 0.0,
                "shooters": len(sh), "joining": 0, "cover": False}
    me_c, me_hp = w.me.get("cubes", 0), w.me["max_hp"]
    bp = getattr(w, "mode_p", {}) or {}
    big = [e for e in seen if bp.get("avoid_big", False) and (
        (e.get("cubes") or 0) >= me_c + bp.get("big_cubes", 5) or (e.get("max_hp") or 1400) >= me_hp * bp.get("big_hp", 1.3))]
    if big:
        b = min(big, key=lambda e: e.get("_d", 999))
        vx, vy = (b.get("vel") or [0, 0, 0])[:2]
        closing = (vx * (w.x - b["pos"][0]) + vy * (w.y - b["pos"][1])) > 0
        c, tr = cover_reach(w, seen, hp / 150.0)
        if closing and b.get("_d", 999) < bp.get("big_flee_r", 90.0):
            return {"verdict": "run", "why": "big powered-up enemy (%s cubes) chasing: flee" % b.get("cubes"), "margin": -1.0,
                    "shooters": len(sh), "joining": 0, "cover": False, "big": True, "flee": True}
        if c is not None:
            return {"verdict": "run", "why": "big powered-up enemy (%s cubes): cover until it leaves" % b.get("cubes"), "margin": -1.0,
                    "shooters": len(sh), "joining": 0, "cover": True, "big": True}
    ids = {e.get("id") for e in seen}
    joining = []
    for tr in trk.near(w.x, w.y, p.get("join_r", 120.0), max_age=4.0):
        if tr["id"] in ids or not trk.aware(tr, 4.0):
            continue
        x, y, _, _ = trk.est(tr)
        joining.append((tr, math.hypot(x - w.x, y - w.y)))
    fighters = []
    for e in seen:
        d = e.get("_d") or math.hypot(e["pos"][0] - w.x, e["pos"][1] - w.y)
        tr = trk.tracks.get(e.get("id")) or {"hp": e.get("hp", 1400)}
        k = reasoner.ttk(w, tr, d, False)
        fighters.append({"ttk": k if k is not None else 99.0, "dps": enemy_dps(e.get("weapons"), d), "join": 0.0})
    for tr, d in joining:
        k = reasoner.ttk(w, tr, d, False)
        fighters.append({"ttk": (k if k is not None else 99.0) + 1.0, "dps": enemy_dps(tr.get("weapons"), d) * p.get("join_w", 0.5),
                         "join": min(4.0, d / 12.0)})
    incoming_now = sum(f["dps"] for f in fighters if f["join"] == 0.0)
    t_die = hp / max(1.0, incoming_now)
    left = hp
    t = 0.0
    order = sorted(fighters, key=lambda f: f["ttk"])
    alive = list(order)
    for f in order:
        dt = f["ttk"]
        inc = sum(g["dps"] * max(0.0, min(dt, t + dt - g["join"])) / dt for g in alive if dt > 0)
        left -= inc * dt
        t += dt
        alive.remove(f)
        if left <= 0:
            break
    margin = left / max(1.0, w.me["max_hp"])
    c, t_reach = cover_reach(w, seen, t_die)
    n = len(seen)
    if left <= 0 and c is not None:
        verdict, why = "run", "we lose this in the open (%d on us, %d coming): fight from cover" % (n, len(joining))
    elif left <= 0:
        verdict, why = "stay", "we lose this, but no cover in reach: trade, don't turn our back"
    elif len(sh) >= p.get("min_shooters", 3) and c is not None:
        verdict, why = "run", "%d have a line on us: to cover" % len(sh)
    else:
        verdict, why = "stay", ("we win this: %d on us, %d coming" if margin > 0.15 else "close, but we stay: %d on us, %d coming") % (n, len(joining))
    return {"verdict": verdict, "why": why, "margin": round(margin, 2), "shooters": len(sh), "joining": len(joining),
            "cover": c is not None}
