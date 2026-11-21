"""Fight read: should we run or stay? Decided from the awareness picture, not from an HP line.

It plays the fight forward in a few lines: everyone with a line on us shoots, enemies that know where we are and are
close (join_r) join halfway through, and we kill them one at a time, quickest first. If we die before the last one
does, or we win with almost no margin (win_margin), the read is 'lose' and the mech runs. An attacker we cannot see at
all is always a 'run': we cannot win a fight we cannot aim at.

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


def read(w, trk, reasoner, p):
    """-> dict(verdict 'run'|'stay'|None, why, margin, shooters, joining)."""
    sh = list(w.shooters)
    hit_recently = w.t - w.dmg_t < 2.0
    if not w.alerted or not (sh or hit_recently):
        return {"verdict": None, "why": "no one on us", "margin": None, "shooters": 0, "joining": 0}
    seen = [e for e in sh if not e.get("ghost")]
    if not seen:
        return {"verdict": "run", "why": "hit by someone we can't see", "margin": 0.0, "shooters": len(sh), "joining": 0}
    ids = {e.get("id") for e in seen}
    # enemies that know where we are and are close enough to join in
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
    # play it forward: kill quickest first, take damage from everyone still up (joiners after their travel time)
    hp = w.me["hp"]
    t = 0.0
    order = sorted(fighters, key=lambda f: f["ttk"])
    alive = list(order)
    for f in order:
        dt = f["ttk"]
        inc = sum(g["dps"] * max(0.0, min(dt, t + dt - g["join"])) / dt for g in alive if dt > 0)
        hp -= inc * dt
        t += dt
        alive.remove(f)
        if hp <= 0:
            break
    margin = hp / max(1.0, w.me["max_hp"])
    need = p.get("win_margin", 0.15)
    if hp <= 0:
        verdict, why = "run", "we lose this: %d on us, %d coming" % (len(seen), len(joining))
    elif margin < need:
        verdict, why = "run", "too close to call: %d on us, %d coming" % (len(seen), len(joining))
    elif len(sh) >= p.get("min_shooters", 3):
        verdict, why = "run", "%d have a line on us" % len(sh)
    else:
        verdict, why = "stay", "we win this: %d on us, %d coming" % (len(seen), len(joining))
    return {"verdict": verdict, "why": why, "margin": round(margin, 2), "shooters": len(sh), "joining": len(joining)}
