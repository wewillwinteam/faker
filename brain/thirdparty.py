"""Third-party planner (free-for-all only): come into someone else's fight on the side of the "second party".

Reads the fights the SoundMap built from what we heard (plus tracked mechs), picks one, picks the side to hit
(the fighter more likely to win and/or facing away from us), and returns where to stand and when to go in:
  flank  - moving to the approach point: beside the target, outside both fighters' view cones, inside our range,
           never across open ground they can see when a hidden route exists
  hold   - at the approach point, hidden, waiting for the read to turn
  commit - go in: both are locked on each other and we are unseen, or one side just died / went quiet
           (clean up the winner), or one side just took big damage

Use each decision (after world.update and tracker.update):
    soundmap.update()
    plan = third_party_plan(world, tracker, soundmap, params)   # None = nothing to third-party
Standard library only; one call is ~1 ms (LOS checks are capped)."""
import math
from .world import ang_diff, unit, EYE, CHEST

DEFAULTS = {
    "max_dist": 420.0,       # ignore fights farther than this
    "standoff": None,        # approach distance from the target; None = 0.6 x our best weapon range (60..170 m)
    "cone_margin_deg": 10.0, # stay this far outside a fighter's view cone
    "hold_r": 30.0,          # within this of the approach point we hold
    "lock_quiet_s": 3.0,     # both sides fired within this (covers a 2.4 s gun reload) = still locked
    "min_lock_shots": 4,     # each side fired at least this many shots
    "lock_min_s": 1.5,       # fight must have run this long before we call it locked
    "unseen_p": 0.7,         # w.p_undetected at our spot needed to commit on a lock
    "big_dmg": 300.0,        # HP lost in 2 s that counts as "just took big damage"
    "low_hp": 0.35,          # a damaged side under this HP share becomes the target (finish it)
    "commit_extra": 80.0,    # after a crack, commit from up to weapon range + this
    "fire_unc": 35.0,        # do not fire at a sound blob looser than this (m) unless the target is seen
    "self_fight_s": 3.0,     # we took mech damage this recently: our own fight comes first, return None
    "route_every_s": 0.5,    # recompute the covered route at most this often
    "pref_m": 150.0,         # metres of approach cost one point of target preference is worth
    "sticky_m": 40.0,        # metres of cost the approach point we already chose is let off (stops it wandering)
}


def _p(params):
    p = dict(DEFAULTS)
    if params:
        p.update({k: v for k, v in params.items() if k in DEFAULTS})
    return p


def weapon_range(w):
    best = 0.0
    for wp in (getattr(w, "weapons", None) or []):
        if not wp:
            continue
        r = (wp.get("stats") or {}).get("range")
        if r is None:
            r = {"gun": 230.0, "laser": 280.0, "lightning": 45.0, "mortar": 200.0}.get(wp.get("kind"), 0.0)
        best = max(best, float(r))
    return best or 230.0


def standoff_of(rng, p):
    s = p["standoff"] or max(60.0, min(170.0, 0.6 * rng))
    return min(s, 0.9 * rng)


def _fake(w, q, facing, face_unc=0.0):
    """A participant as an enemy dict World.sees / unseen_step / find_cover understand. face_unc (radians) is how
    unsure its facing is (a facing guessed from two sound fixes is only as good as those fixes)."""
    z = w.ground(q["x"], q["y"], w.z)
    return {"id": "snd%s" % q["sid"], "kind": "mech", "pos": [q["x"], q["y"], z], "aim": [facing, 0.0],
            "unc": min(float(q.get("unc") or 0.0), 40), "face_unc": face_unc,
            "vel": [0.0, 0.0, 0.0], "weapons": [k for k in q["kinds"] if k != "laser_charge"] or ["gun"]}


def _cone_depth(w, f, px, py, margin):
    """How deep (radians, 0 = outside) a spot sits in f's view: 2.0 inside its near ring, else how far inside the
    wedge. Grows with the fighter's uncertainty like _in_cone."""
    ex, ey = f["pos"][0], f["pos"][1]
    u = f.get("unc", 0.0)
    d = math.hypot(px - ex, py - ey)
    if d > w.vrange + 10 + u:
        return 0.0
    if d < w.near + 8 + u:
        return 2.0
    lim = w.fov_half + margin + f.get("face_unc", 0.0)
    return max(0.0, lim - abs(ang_diff(math.atan2(py - ey, px - ex), f["aim"][0])))


def _in_cone(w, f, px, py, margin):
    """Geometric view test (no LOS): inside f's near ring or its view wedge out to vision range. A fighter placed
    from sound could be anywhere within its uncertainty (`unc`, grows the ring and the range) and its facing is a
    guess (`face_unc`, widens the wedge)."""
    ex, ey = f["pos"][0], f["pos"][1]
    u = f.get("unc", 0.0)
    d = math.hypot(px - ex, py - ey)
    if d > w.vrange + 10 + u:
        return False
    if d < w.near + 8 + u:
        return True
    return abs(ang_diff(math.atan2(py - ey, px - ex), f["aim"][0])) < w.fov_half + margin + f.get("face_unc", 0.0)


def _seen_at(w, fakes, px, py, margin):
    n = 0
    for f in fakes:
        if _in_cone(w, f, px, py, margin):
            pz = w.ground(px, py, w.z)
            a, b = (f["pos"][0], f["pos"][1], f["pos"][2] + EYE), (px, py, pz + CHEST)
            if w.los(a, b):
                ts = getattr(w, "tree_sight", None)      # canopy hides us beyond the near ring
                far = math.hypot(px - a[0], py - a[1]) > w.near
                if ts is None or ts(a, b, far):
                    n += 1
    return n


def _exposure(w, fakes, a, b, margin):
    """How many sample points along a->b some fighter can see (0 = covered route)."""
    n = 0
    for f in (0.5, 1.0):
        n += _seen_at(w, fakes, a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, margin)
    return n


def _pick_fight(w, fights, p):
    best, bs = None, 1e9
    for f in fights:
        if f["dist"] > p["max_dist"]:
            continue
        alive = [q for q in f["participants"] if not q["dead"]]
        if not alive or w.t - f["last_t"] > 8.0:
            continue
        s = f["dist"] - 25.0 * min(f["intensity"], 4.0) - (90.0 if f["likely_cracked"] else 0.0)
        if s < bs:
            best, bs = f, s
    return best


def _facing(q, other, us):
    if q.get("facing") is not None:
        return q["facing"]
    if other is not None:
        return math.atan2(other["y"] - q["y"], other["x"] - q["x"])
    return math.atan2(us[1] - q["y"], us[0] - q["x"])      # nothing known: assume it looks at us (pessimistic)


def _face_unc(q, other):
    """Facing seen on a track: sure. Guessed 'faces its opponent': off by about the two fixes' error over their gap."""
    if q.get("facing") is not None:
        return 0.0
    if other is None:
        return 0.6
    D = max(20.0, math.hypot(other["x"] - q["x"], other["y"] - q["y"]))
    return min(0.25, 0.3 * math.atan2(q["unc"] + other["unc"], D))


def _pick_target(w, f, p):
    """(target, other, why): the second party = the side more likely to win and/or facing away from us."""
    parts = [q for q in f["participants"] if not q["dead"]]
    parts.sort(key=lambda q: -q["shots"])
    us = (w.x, w.y)
    if f["likely_cracked"] and f["winner"] is not None:
        t = next((q for q in parts if q["sid"] == f["winner"]), parts[0])
        others = [q for q in parts if q is not t]
        return t, (others[0] if others else None), "target: the side still firing"
    if len(parts) == 1:
        return parts[0], None, "lone shooter (its opponent is out of earshot)"
    a, b = parts[0], parts[1]
    # a side that just took big damage and is low: finish it, the other is still worn from the fight
    for q, o in ((a, b), (b, a)):
        hf = q.get("hp_frac")
        if q["hp_drop"] >= p["big_dmg"] and hf is not None and hf < p["low_hp"]:
            return q, o, "finish the side that just took big damage"

    def score(q, o):
        fq = _facing(q, o, us)
        away = abs(ang_diff(fq, math.atan2(us[1] - q["y"], us[0] - q["x"]))) / math.pi    # 1 = back to us
        pw = q["power"] / max(1e-6, q["power"] + o["power"])
        hq = q["hp_frac"] if q.get("hp_frac") is not None else 0.6
        ho = o["hp_frac"] if o.get("hp_frac") is not None else 0.6
        hp = hq / max(1e-6, hq + ho)
        dmg = min(1.0, q["hp_drop"] / 600.0) + 0.1 * min(q["hits"], 3)
        return 1.0 * away + 0.5 * pw + 0.7 * hp - 0.4 * dmg

    # both sides are candidates; third_party_plan weighs this preference against how good a spot each one offers
    return [(score(a, b), a, b), (score(b, a), b, a)], None, "second party: likelier winner / back to us"


def _approach_point(w, t, o, ft, fo, rng, p, refine=True, prev=None):
    """Best spot beside the target: out of both cones, our side of the fight line, in range, cheap to reach."""
    margin = math.radians(p["cone_margin_deg"])
    standoff = standoff_of(rng, p)
    facing = ft["aim"][0]
    nc, nr = w.zone["next_center"], w.zone["next_radius"]
    side_us = None
    if o is not None:
        side_us = (t["x"] - o["x"]) * (w.y - o["y"]) - (t["y"] - o["y"]) * (w.x - o["x"])
    pts = []
    for deg in (80, 100, 120, 140, 160):            # a ring round the target, off its facing
        for sgn in (1, -1):
            a = facing + sgn * math.radians(deg)
            for r in (standoff, standoff * 0.7):
                pts.append((t["x"] + math.cos(a) * r, t["y"] + math.sin(a) * r))
    if o is not None:
        # straight behind the target (it faces the other fighter) and past the other's sight range: the classic spot
        D = max(30.0, math.hypot(o["x"] - t["x"], o["y"] - t["y"]))
        r_out = w.vrange + 15.0 + min(o["unc"], 40.0) - D
        if 0.0 < r_out <= 0.9 * rng:
            away_o = math.atan2(t["y"] - o["y"], t["x"] - o["x"])
            for off in (0.0, 0.25, -0.25):
                rr = r_out / max(0.5, math.cos(off))
                if rr <= 0.9 * rng:
                    pts.append((t["x"] + math.cos(away_o + off) * rr, t["y"] + math.sin(away_o + off) * rr))
    if o is not None:
        # out to the side of the fight line, level with the target's shoulder: the one place outside both cones
        D = max(30.0, math.hypot(o["x"] - t["x"], o["y"] - t["y"]))
        mx, my = t["x"] + 0.3 * (o["x"] - t["x"]), t["y"] + 0.3 * (o["y"] - t["y"])
        nx, ny = unit(-(o["y"] - t["y"]), o["x"] - t["x"])
        for h in (0.9 * D, 1.3 * D, 1.8 * D):
            for sgn in (1, -1):
                px, py = mx + nx * sgn * h, my + ny * sgn * h
                dt_ = math.hypot(px - t["x"], py - t["y"])
                if dt_ > 0.9 * rng:
                    k = 0.9 * rng / dt_
                    px, py = t["x"] + (px - t["x"]) * k, t["y"] + (py - t["y"]) * k
                pts.append((px, py))
    sticky = None
    if prev is not None and math.hypot(prev[0] - t["x"], prev[1] - t["y"]) <= 0.9 * rng:
        pts.append(prev)                 # the spot we already chose: keep it unless something is clearly better
        sticky = prev
    cands = []
    for cx, cy in pts:
        if not w.walkable(cx, cy):
            continue
        c = math.hypot(cx - w.x, cy - w.y)
        dep = _cone_depth(w, ft, cx, cy, margin)
        if dep > 0:
            c += 150.0 + 300.0 * dep        # inside a view cone: the shallower the better
        if fo is not None:
            dep = _cone_depth(w, fo, cx, cy, margin)
            if dep > 0:
                c += 150.0 + 300.0 * dep
            if math.hypot(cx - o["x"], cy - o["y"]) < w.near + 15:
                c += 200.0
            # stay on the target's side: a spot nearer the other fighter costs the difference
            c += 1.5 * max(0.0, math.hypot(cx - t["x"], cy - t["y"]) - math.hypot(cx - o["x"], cy - o["y"]))
        if side_us is not None:
            side_c = (t["x"] - o["x"]) * (cy - o["y"]) - (t["y"] - o["y"]) * (cx - o["x"])
            if side_c * side_us < 0:
                c += 150.0          # would cross the fight line
        dz = math.hypot(cx - nc[0], cy - nc[1])
        if dz > nr:
            c += (dz - nr) * 2.0
        if sticky is not None and (cx, cy) == sticky:
            c -= p["sticky_m"]
        cands.append((c, cx, cy))
    if not cands:
        return None, 1e9
    cands.sort()
    if not refine:                       # geometry only (no LOS): enough to compare two targets
        return (cands[0][1], cands[0][2]), cands[0][0]
    best, bs = None, 1e9
    for c, cx, cy in cands[:3]:          # LOS only for the few cheapest
        cz = w.ground(cx, cy, w.z)
        tz = ft["pos"][2]
        if not w.los((cx, cy, cz + EYE), (t["x"], t["y"], tz + CHEST)):
            c += 60.0                    # cannot shoot the target from there
        if fo is not None and not w.los((cx, cy, cz + CHEST), (fo["pos"][0], fo["pos"][1], fo["pos"][2] + EYE)):
            c -= 40.0                    # hidden from the other fighter: cover
        if c < bs:
            best, bs = (cx, cy), c
    return best, bs


def _route(w, fakes, goal, p, cache, busy=False):
    """(via or None, exposure). A dogleg is taken only when it is less exposed than the straight line.
    busy: the spot search already ran this call, so reuse a route up to twice as old to spread the cost."""
    margin = math.radians(p["cone_margin_deg"])
    me = (w.x, w.y)
    key = (round(goal[0] / 15), round(goal[1] / 15))
    age = w.t - cache.get("t", -99)
    if busy and "via" in cache and age < 2 * p["route_every_s"]:
        return cache.get("via"), cache.get("exp", 0)
    if cache.get("key") == key and age < p["route_every_s"]:
        via = cache.get("via")
        if via is None or math.hypot(via[0] - w.x, via[1] - w.y) > 12:
            return via, cache.get("exp", 0)
    direct = _exposure(w, fakes, me, goal, margin)
    via, exp = None, direct
    if direct:
        L = math.hypot(goal[0] - w.x, goal[1] - w.y)
        mx, my = (w.x + goal[0]) / 2, (w.y + goal[1]) / 2
        nx, ny = unit(-(goal[1] - w.y), goal[0] - w.x)
        cx = sum(f["pos"][0] for f in fakes) / len(fakes)
        cy = sum(f["pos"][1] for f in fakes) / len(fakes)
        opts = []
        for s in (1, -1):
            vx, vy = mx + nx * s * 0.5 * L, my + ny * s * 0.5 * L
            away = math.hypot(vx - cx, vy - cy)
            opts.append((-away, vx, vy))          # try the side away from the fight first
        opts.sort()
        for _, vx, vy in opts:
            if not w.walkable(vx, vy):
                continue
            e = _exposure(w, fakes, me, (vx, vy), margin) + _exposure(w, fakes, (vx, vy), goal, margin)
            if e < exp:
                via, exp = (vx, vy), e
                if e == 0:
                    break
    cache.update(key=key, t=w.t, via=via, exp=exp)
    return via, exp


def third_party_plan(w, tracker, soundmap, params=None):
    """None, or {goal, approach, target_side, fire_ok, why, ...} for the best fight to third-party."""
    if not getattr(w, "ffa", False):
        return None
    p = _p(params)
    if w.t - getattr(w, "dmg_t", -99.0) < p["self_fight_s"] or getattr(w, "seen_by", None):
        return None                       # we are in our own fight
    f = _pick_fight(w, soundmap.fights, p)
    if f is None:
        return None
    picked, o, why_t = _pick_target(w, f, p)
    us = (w.x, w.y)
    rng = weapon_range(w)
    options = picked if isinstance(picked, list) else [(0.0, picked, o)]
    cache = soundmap.__dict__.setdefault("_tp_cache", {})
    # the spot search is the costly part: reuse it while the fight and we have not moved much
    key = (f["id"], round(w.x / 10), round(w.y / 10),
           tuple((q["sid"], round(q["x"] / 10), round(q["y"] / 10)) for _, q, _o in options), why_t)
    hit = cache.get("ap")
    reuse = bool(hit and hit[0] == key and w.t - hit[1] < p["route_every_s"])
    if reuse:
        options = [(pr, q, oo) for pr, q, oo in options if q["sid"] == hit[2]] or options
        reuse = len(options) == 1
    best = None
    for pref, t_, o_ in options:
        ft_ = _fake(w, t_, _facing(t_, o_, us), _face_unc(t_, o_))
        fo_ = _fake(w, o_, _facing(o_, t_, us), _face_unc(o_, t_)) if o_ is not None else None
        # cheap geometric score to choose the side; the LOS refinement runs once, for the chosen side only
        prev = hit[3] if hit and hit[2] == t_["sid"] else None
        g_, cost = (hit[3], 0.0) if reuse else _approach_point(w, t_, o_, ft_, fo_, rng, p, refine=len(options) == 1,
                                                              prev=prev)
        if g_ is None:
            continue
        val = pref * p["pref_m"] - cost        # a clearly better spot can outweigh a weaker preference
        if best is None or val > best[0]:
            best = (val, t_, o_, ft_, fo_, g_)
    if best is None:
        return None
    _, t, o, ft, fo, goal = best
    if not reuse and len(options) > 1:
        prev = hit[3] if hit and hit[2] == t["sid"] else None
        goal = _approach_point(w, t, o, ft, fo, rng, p, prev=prev)[0] or goal
    if not reuse:
        cache["ap"] = (key, w.t, t["sid"], goal)
    fakes = [ft] + ([fo] if fo is not None else [])
    # other live shooters in the same fight are watchers too
    for q in f["participants"]:
        if q is not t and q is not o and not q["dead"]:
            fakes.append(_fake(w, q, _facing(q, t, us)))
    approach_pt = goal
    nc, nr = w.zone["next_center"], w.zone["next_radius"]
    if getattr(w, "zone_urgent", False) and math.hypot(goal[0] - nc[0], goal[1] - nc[1]) > nr:
        return None                       # the zone comes first
    d_goal = math.hypot(goal[0] - w.x, goal[1] - w.y)
    d_t = math.hypot(t["x"] - w.x, t["y"] - w.y)
    margin = math.radians(p["cone_margin_deg"])

    # ---- timing read
    alive = [q for q in f["participants"] if not q["dead"]]
    # best-guess view (no widening for sound error): are we in a fighter's sight right now?
    exposed_now = _seen_at(w, [dict(f_, unc=0.0, face_unc=0.0) for f_ in fakes], w.x, w.y, 0.0) > 0
    unseen = (not exposed_now) and w.p_undetected(w.x, w.y, w.z) >= p["unseen_p"]
    # locked: both have been firing (a gun reload is 2.4 s of quiet, so allow that much) for a while
    q_s = p["lock_quiet_s"]
    locked = (o is not None and t["silent_s"] < q_s and o["silent_s"] < q_s and t["shots"] >= p["min_lock_shots"]
              and o["shots"] >= p["min_lock_shots"] and w.t - f["started_t"] >= p["lock_min_s"])
    cracked = f["likely_cracked"] and f["winner"] == t["sid"]
    big = next((q for q in alive if q["hp_drop"] >= p["big_dmg"]), None)
    at_point = d_goal <= p["hold_r"]
    in_range = d_t <= 0.95 * rng
    approach, why = "flank", "moving to the approach point beside the target"
    if cracked and d_t <= rng + p["commit_extra"]:
        approach, why = "commit", "one side went down/quiet: clean up the winner"
    elif big is not None and (in_range or at_point):
        approach, why = "commit", "a side just took big damage"
    elif locked and unseen and (at_point or (in_range and d_t <= standoff_of(rng, p) + p["hold_r"])):
        approach, why = "commit", "both locked on each other and we are unseen"
    elif at_point:
        approach, why = "hold", "at the approach point; waiting for the fight to turn"

    # ---- where to stand
    if approach == "commit" and math.hypot(goal[0] - t["x"], goal[1] - t["y"]) > 0.9 * rng:
        ux, uy = unit(w.x - t["x"], w.y - t["y"])
        goal = (t["x"] + ux * 0.8 * rng, t["y"] + uy * 0.8 * rng)
    if approach == "hold" and fakes:
        cover = w.find_cover(fakes)       # nearest spot hidden from the fighters (World searches around us)
        if cover is not None and math.hypot(cover[0] - goal[0], cover[1] - goal[1]) < 40:
            goal = cover

    # ---- route and next step
    via, exposure = (None, 0) if approach == "commit" else _route(w, fakes, goal, p, cache, busy=not reuse)
    nxt = via or goal
    mv = list(unit(nxt[0] - w.x, nxt[1] - w.y))
    if approach != "commit" and math.hypot(nxt[0] - w.x, nxt[1] - w.y) > 3:
        ahead = (w.x + mv[0] * 4.0, w.y + mv[1] * 4.0)
        if _seen_at(w, fakes, ahead[0], ahead[1], margin):
            mv = w.unseen_step(mv, watchers=fakes)

    # ---- fire only when committed, in range, with a line, and the target is pinned down
    fire_ok = False
    if approach == "commit" and d_t <= rng:
        sharp = t["unc"] <= p["fire_unc"] or (t.get("track_id") is not None and t["unc"] < 15)
        tz = ft["pos"][2]
        fire_ok = sharp and w.los(w.muz if hasattr(w, "muz") else (w.x, w.y, w.z + 7.0), (t["x"], t["y"], tz + CHEST))

    return {
        "goal": (round(goal[0], 1), round(goal[1], 1)),
        "approach_point": (round(approach_pt[0], 1), round(approach_pt[1], 1)),
        "approach": approach,
        "target_side": {"sid": t["sid"], "x": round(t["x"], 1), "y": round(t["y"], 1), "unc": round(t["unc"], 1),
                        "track_id": t.get("track_id"), "facing": round(ft["aim"][0], 3)},
        "other_side": ({"sid": o["sid"], "x": round(o["x"], 1), "y": round(o["y"], 1), "unc": round(o["unc"], 1),
                        "track_id": o.get("track_id")} if o is not None else None),
        "fire_ok": bool(fire_ok),
        "why": "%s; %s" % (why, why_t),
        "fight_id": f["id"],
        "via": (round(via[0], 1), round(via[1], 1)) if via else None,
        "move": [round(mv[0], 3), round(mv[1], 3)],
        "exposed_route": exposure,
        "d_target": round(d_t, 1),
    }
