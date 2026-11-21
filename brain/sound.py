"""Sound map: turns what the mech hears into sources with positions, then into fights between other mechs.

Each heard sound is a ray: our position, a noisy bearing (sigma 7 deg) and a distance band. The band comes from the
bucket AND the kind's carry range: a "far" gun is 160..320 m, a "mid" lightning is 60..160 m, a "far" lightning cannot
happen (carry 160). Sounds carry no source id, so rays are grouped into sources by bearing, band, kind and fire rate
(one gun cannot fire twice within 0.17 s, so two shots that close are two shooters). Each source is then placed by a
maximum-likelihood crossing of its rays (taken from the different spots we stood on over the last few seconds) with a
pull into the band, and gets an honest uncertainty radius. Fire that did not hit us is someone else's fight.

Range is the weak axis: with ~15 shots heard over a 100 m walk, a fighter 200 m out is placed to ~20-45 m, one 300 m
out to ~50-70 m (test_thirdparty.py measures this). Moving sideways while listening is what sharpens it.

Use: sm = SoundMap(world, tracker); every decision sm.update(); read sm.fights.
Standard library only; one update is well under 3 ms with ~60 rays."""
import math
from .world import ang_diff

FIRE = {"gun", "laser", "laser_charge", "lightning", "mortar"}          # made by the shooter at the shooter
MECH = FIRE | {"step", "boost", "jet", "reload"}                          # made by a mech at the mech
CARRY = {"step": 40, "boost": 90, "jet": 90, "reload": 60, "laser_charge": 140, "lightning": 160, "gun": 320,
         "laser": 380, "mortar": 420, "explosion": 420, "death": 600, "crate": 220, "npc_gun": 200, "drop": 600}
MIN_GAP = {"gun": 0.17, "lightning": 0.17, "laser": 1.0, "mortar": 1.5}   # one mech cannot fire one weapon faster
HEAVY = {"laser": 1.6, "mortar": 1.4, "lightning": 1.2, "gun": 1.0, "laser_charge": 0.0}   # win weight per shot

DEFAULTS = {
    "ray_keep_s": 8.0,        # how long a ray stays in the solve
    "ray_tau_s": 5.0,         # older rays weigh less (sources move ~12 m/s)
    "gate_sigma": 2.6,        # bearing gate for joining a source, in noise sigmas
    "merge_m": 22.0,          # two sources closer than this are one
    "source_keep_s": 12.0,    # a source with no new sound is dropped after this
    "fight_link_m": 260.0,    # two shooters this close can be fighting each other (gun range 230)
    "fight_active_s": 6.0,    # a shooter counts in a fight while it fired this recently
    "silence_s": 3.6,         # silent longer than a gun reload (2.4 s) + margin after a burst = likely down
    "min_burst": 4,           # shots before silence means anything
    "track_link_m": 25.0,     # a source this close (plus uncertainty) to a tracked mech is that mech
    "max_rays": 90,
    "band_prior_k": 1.0,      # in-band pull toward the band middle, as a multiple of the band's uniform sd
}


class Source:
    __slots__ = ("sid", "x", "y", "unc", "first_t", "last_t", "fire_t", "kinds", "shots", "rays", "dead_t",
                 "at_us_t", "track_id", "facing", "hp_frac", "hits", "solved", "cov", "lastk", "dead_alt")

    def __init__(self, sid, t):
        self.sid = sid
        self.x = self.y = 0.0
        self.unc = 999.0
        self.first_t = self.last_t = t
        self.fire_t = -99.0
        self.kinds = {}
        self.shots = []         # times of fire sounds (for rate and silence)
        self.rays = []
        self.dead_t = None
        self.dead_alt = []      # other sources the death sound could have come from: [(cost, sid)]
        self.at_us_t = -99.0
        self.track_id = None
        self.facing = None
        self.hp_frac = None
        self.hits = 0           # explosions landing on it
        self.solved = False
        self.cov = (1e4, 0.0, 1e4)   # position covariance (xx, xy, yy), m^2
        self.lastk = {}              # kind -> last time this source fired it (for the fire-rate rule)


class SoundMap:
    def __init__(self, world, tracker=None, params=None):
        self.w = world
        self.trk = tracker
        self.p = dict(DEFAULTS)
        if params:
            self.p.update({k: v for k, v in params.items() if k in DEFAULTS})
        cfg = (getattr(world, "cfg", None) or {}).get("sound", {})
        self.carry = dict(CARRY)
        self.carry.update(cfg.get("range", {}))
        self.near = float(cfg.get("near", 60))
        self.mid = float(cfg.get("mid", 160))
        self.sigma = math.radians(cfg.get("bearing_noise_deg", 7.0))
        self.rays = []          # [t, ox, oy, bearing, rmin, rmax, kind, sid]
        self.sources = {}
        self.fights = []
        self._n = 0
        self._solve_t = -99.0
        self._hp = {}           # track id -> [(t, hp)]
        self._fight_start = {}  # fight key -> first time seen

    # ---------------------------------------------------------------- bands
    def band(self, kind, bucket):
        c = float(self.carry.get(kind, 100))
        if bucket == "near":
            lo, hi = 3.0, min(self.near, c)
        elif bucket == "mid":
            lo, hi = self.near, min(self.mid, c)
        else:
            lo, hi = self.mid, c
        if hi <= lo:            # cannot happen by the rules; keep a sane band anyway
            lo, hi = max(3.0, hi * 0.6), max(hi, 10.0)
        return lo, hi

    # ---------------------------------------------------------------- per tick
    def update(self):
        w = self.w
        t = w.t
        p = self.p
        damaged = [ev for ev in w.events if ev.get("type") == "damaged" and ev.get("by") is not None and ev["by"] < 100]
        killed_now = any(ev.get("type") == "kill" for ev in w.events)
        new_fire = []
        n0 = len(self.rays)
        heard = sorted((w.obs.get("sounds") or []), key=lambda s: -float(s.get("age") or 0.0))
        for s in heard:
            k, b = s.get("kind"), s.get("bearing")
            if b is None or k is None:
                continue
            ts = t - float(s.get("age") or 0.0)
            lo, hi = self.band(k, s.get("dist"))
            if k in MECH:
                r = [ts, w.x, w.y, b, lo, hi, k, None]
                self._assign(r)
                self.rays.append(r)
                if k in FIRE:
                    new_fire.append(r)
            elif k == "death" and not killed_now:
                self._death(ts, b, lo, hi)
            elif k == "explosion":
                src = self._nearest_by_bearing(b, lo, hi, fire_only=True)
                if src is not None:
                    src.hits += 1
        added = len(self.rays) - n0
        # fire that came with damage on us: that shooter is fighting us, not someone else
        for ev in damaged:
            eb = ev.get("bearing")
            if eb is None:
                continue
            for r in new_fire:
                if abs(ang_diff(r[3], eb)) < 3 * self.sigma and r[7] in self.sources:
                    self.sources[r[7]].at_us_t = t
        # drop old rays, keep the list short
        cut = t - p["ray_keep_s"]
        if self.rays and (self.rays[0][0] < cut or len(self.rays) > p["max_rays"]):
            self.rays = [r for r in self.rays if r[0] >= cut][-p["max_rays"]:]
        # re-cluster and re-solve only when something new was heard (or now and then, as rays age out)
        if self.rays and (added or t - self._solve_t > 0.5):
            self._solve_t = t
            self._reassign()
            for s in self.sources.values():
                self._refresh(s, cut)
            self._check_deaths()
            self._solve_all()
            self._merge()
            self._prune()
        self._link_tracks()
        for k in [k for k, s in self.sources.items() if t - s.last_t > p["source_keep_s"] and not s.rays]:
            del self.sources[k]
        self._build_fights()
        return self.fights

    # ---------------------------------------------------------------- association
    def _spread(self, src, b, t):
        """(perpendicular sd, along sd) of the source's position seen along bearing b, with drift since its last sound."""
        c11, c12, c22 = src.cov
        drift = (4.0 * max(0.0, t - src.last_t)) ** 2
        ux, uy = math.cos(b), math.sin(b)
        vp = uy * uy * c11 - 2 * ux * uy * c12 + ux * ux * c22 + drift
        va = ux * ux * c11 + 2 * ux * uy * c12 + uy * uy * c22 + drift
        return math.sqrt(max(vp, 0.0)), math.sqrt(max(va, 0.0))

    def _fit(self, src, r, lastk=None):
        """Cost of ray r belonging to src (in sigmas; large = no)."""
        if not src.solved:
            return 1e9
        k = r[6]
        gap = MIN_GAP.get(k)
        if gap is not None:
            lt = (src.lastk if lastk is None else lastk).get(k)
            if lt is not None and abs(r[0] - lt) < gap:
                return 1e9          # one mech cannot fire this weapon that fast: a second shooter
        dx, dy = src.x - r[1], src.y - r[2]
        d = math.hypot(dx, dy)
        if d < 1.0:
            return 0.0
        dead_pen = 1.2 if (src.dead_t is not None and r[0] > src.dead_t) else 0.0   # it probably died: weak claim
        sp, sa = self._spread(src, r[3], r[0])
        sig = math.hypot(self.sigma, sp / d)
        c = abs(ang_diff(math.atan2(dy, dx), r[3])) / sig + dead_pen
        lo, hi = r[4], r[5]
        slack = 10.0 + 1.5 * sa
        if d < lo - slack:
            c += (lo - slack - d) / 20.0
        elif d > hi + slack:
            c += (d - hi - slack) / 20.0
        if k in FIRE and src.kinds and k not in src.kinds and k != "laser_charge":
            # a mech has at most two weapons; a new weapon kind on a two-weapon source is unlikely
            fk = [x for x in src.kinds if x in FIRE and x != "laser_charge"]
            if len(fk) >= 2:
                c += 2.0
            else:
                c += 0.4
        return c

    def _assign(self, r):
        best, bc = None, self.p["gate_sigma"]
        for s in self.sources.values():
            if not self._open(s):
                continue
            c = self._fit(s, r)
            if c < bc:
                best, bc = s, c
        if best is None:
            self._n += 1
            best = Source(self._n, r[0])
            mid = 0.5 * (r[4] + r[5])
            ux, uy = math.cos(r[3]), math.sin(r[3])
            best.x, best.y = r[1] + ux * mid, r[2] + uy * mid
            va = max(0.5 * (r[5] - r[4]), 15.0) ** 2
            vp = (mid * self.sigma + 3.0) ** 2
            best.cov = (va * ux * ux + vp * uy * uy, (va - vp) * ux * uy, va * uy * uy + vp * ux * ux)
            best.unc = 1.5 * math.sqrt(max(va, vp))
            best.solved = True
            self.sources[best.sid] = best
        self._attach(best, r)

    def _attach(self, s, r):
        r[7] = s.sid
        s.rays.append(r)
        s.last_t = max(s.last_t, r[0])
        k = r[6]
        s.kinds[k] = s.kinds.get(k, 0) + 1
        if k in FIRE and k != "laser_charge":
            s.shots.append(r[0])
            s.fire_t = max(s.fire_t, r[0])
        if k in MIN_GAP:
            s.lastk[k] = max(s.lastk.get(k, -99.0), r[0])

    def _reassign(self):
        """One Lloyd step: every ray in the window goes to the source that fits it best (fixes early mix-ups).
        Rays go in time order so the fire-rate rule (one gun cannot fire twice in 0.17 s) holds for each source."""
        live = [s for s in self.sources.values() if self._open(s)]
        if len(live) < 2:
            for s in self.sources.values():
                s.rays = [r for r in self.rays if r[7] == s.sid]
            return
        self.rays.sort(key=lambda r: r[0])
        lastk = {s.sid: {} for s in live}
        for s in live:
            s.rays = []
        for r in self.rays:
            cur = self.sources.get(r[7])
            if cur is not None and not self._open(cur):
                cur.rays.append(r)
                continue
            if cur is not None and cur.sid not in lastk:
                cur = None
            best, bc = cur, (self._fit(cur, r, lastk[cur.sid]) if cur is not None else 1e9)
            for s in live:
                if s is cur:
                    continue
                c = self._fit(s, r, lastk[s.sid])
                if c < bc - 0.3:            # small hysteresis
                    best, bc = s, c
            if best is None or bc >= 1e8:
                best = cur if cur is not None else None
                if best is None:
                    continue
            r[7] = best.sid
            best.rays.append(r)
            if r[6] in MIN_GAP:
                lastk[best.sid][r[6]] = r[0]

    def _refresh(self, s, cut):
        """Rebuild shot times and last-fire times from the rays the source holds now (rays may have moved)."""
        old = [x for x in s.shots if x < cut][-40:]
        fire = [r[0] for r in s.rays if r[6] in FIRE and r[6] != "laser_charge"]
        s.shots = old + sorted(fire)
        s.fire_t = s.shots[-1] if s.shots else -99.0
        s.lastk = {}
        for r in s.rays:
            if r[6] in MIN_GAP and r[0] > s.lastk.get(r[6], -99.0):
                s.lastk[r[6]] = r[0]
        if s.rays:
            s.last_t = max(r[0] for r in s.rays)

    # ---------------------------------------------------------------- position solve
    def _solve(self, s):
        """Place the source: Gauss-Newton on the bearing errors themselves (maximum likelihood), plus a pull of each
        ray's range into its band. (The quicker 'closest point to all rays' least squares is pulled hard toward us
        when the rays are nearly parallel, which is the normal case at 200 m with a short baseline.)"""
        rays = s.rays
        if not rays:
            return
        t = self.w.t
        tau = self.p["ray_tau_s"]
        sig2 = self.sigma * self.sigma
        cauchy = (2.5 * self.sigma) ** 2
        bk = self.p["band_prior_k"]
        x, y = s.x, s.y
        H11 = H12 = H22 = 0.0
        # the in-band pull is ONE belief about range, not evidence that grows with every shot: share it out
        wsum = sum(math.exp(-(t - r[0]) / tau) for r in rays) or 1.0
        for it in range(6):
            H11 = H12 = H22 = g1 = g2 = 0.0
            for r in rays:
                ox, oy, br, lo, hi = r[1], r[2], r[3], r[4], r[5]
                dx, dy = x - ox, y - oy
                d2 = dx * dx + dy * dy
                if d2 < 1.0:
                    continue
                d = math.sqrt(d2)
                wt = math.exp(-(t - r[0]) / tau)
                res = (math.atan2(dy, dx) - br + math.pi) % (2 * math.pi) - math.pi
                w_ = wt / (sig2 + 9.0 / d2) / (1.0 + res * res / cauchy)
                j1, j2 = -dy / d2, dx / d2
                H11 += w_ * j1 * j1; H12 += w_ * j1 * j2; H22 += w_ * j2 * j2
                g1 += w_ * j1 * res; g2 += w_ * j2 * res
                # range of this ray: hard outside its band, a soft pull toward the middle inside it
                if d < lo:
                    tgt, wa = lo, wt / 100.0
                elif d > hi:
                    tgt, wa = hi, wt / 100.0
                else:
                    sa = max((hi - lo) * 0.2887 * bk, 12.0)
                    tgt, wa = 0.5 * (lo + hi), wt / (sa * sa) / wsum
                k1, k2 = dx / d, dy / d
                rr = d - tgt
                H11 += wa * k1 * k1; H12 += wa * k1 * k2; H22 += wa * k2 * k2
                g1 += wa * k1 * rr; g2 += wa * k2 * rr
            lam = 1e-9 + 0.05 * (H11 + H22) * (1 if it == 0 else 0.2)     # light damping
            a, b_, c = H11 + lam, H12, H22 + lam
            det = a * c - b_ * b_
            if det <= 1e-30:
                return
            sx = -(c * g1 - b_ * g2) / det
            sy = -(a * g2 - b_ * g1) / det
            n = math.hypot(sx, sy)
            if n > 60.0:
                sx, sy = sx * 60.0 / n, sy * 60.0 / n
                n = 60.0
            x, y = x + sx, y + sy
            if n < 0.5:
                break
        det = H11 * H22 - H12 * H12
        if det <= 1e-30:
            return
        # covariance = H^-1; rays are not independent (the mech moves while it fires), so floor it at a few metres
        c11, c12, c22 = H22 / det + 9.0, -H12 / det, H11 / det + 9.0
        tr = c11 + c22
        major = math.sqrt(0.5 * (tr + math.sqrt(max(0.0, (c11 - c22) ** 2 + 4 * c12 * c12))))
        s.cov = (c11, c12, c22)
        s.x, s.y = x, y
        s.unc = min(250.0, 1.5 * major + 4.0 * max(0.0, t - s.last_t) + 4.0)
        s.solved = True

    def _solve_all(self):
        for s in self.sources.values():
            if self._open(s) and s.rays:
                self._solve(s)

    def _merge(self):
        live = sorted((s for s in self.sources.values() if s.dead_t is None), key=lambda s: s.first_t)
        gone = set()
        mm = self.p["merge_m"]
        for i, a in enumerate(live):
            if a.sid in gone:
                continue
            for b in live[i + 1:]:
                if b.sid in gone:
                    continue
                if math.hypot(a.x - b.x, a.y - b.y) < mm:
                    fa = {k for k in a.kinds if k in FIRE and k != "laser_charge"}
                    fb = {k for k in b.kinds if k in FIRE and k != "laser_charge"}
                    if len(fa | fb) > 2:
                        continue            # more weapons than one mech can carry
                    # two sources firing in the same instants are two shooters, not one
                    if a.shots and b.shots and _overlap(a.shots, b.shots) > 0.2:
                        continue
                    for r in b.rays:
                        r[7] = a.sid
                    a.rays.extend(b.rays)
                    for k, n in b.kinds.items():
                        a.kinds[k] = a.kinds.get(k, 0) + n
                    a.shots = sorted(a.shots + b.shots)
                    a.fire_t = max(a.fire_t, b.fire_t)
                    a.last_t = max(a.last_t, b.last_t)
                    a.at_us_t = max(a.at_us_t, b.at_us_t)
                    a.hits += b.hits
                    gone.add(b.sid)
                    self._solve(a)
        for k in gone:
            del self.sources[k]

    def _prune(self):
        """Fewest mechs that explain what we heard: a small source whose every ray fits some other source (in bearing,
        band and fire rate) is not a mech of its own; hand its rays over and drop it."""
        live = [s for s in self.sources.values() if s.dead_t is None and s.rays]
        if len(live) < 2:
            return
        big = max(len(s.rays) for s in live)
        gate = self.p["gate_sigma"]
        for s in sorted(live, key=lambda s: len(s.rays)):
            if len(s.rays) > max(6, 0.6 * big) or s.sid not in self.sources:
                continue
            others = [o for o in live if o is not s and o.sid in self.sources]
            if not others:
                continue
            moves = []      # (ray, from source or None, to source)
            taken = {o.sid: [r for r in o.rays if r[6] in MIN_GAP] for o in others}

            def clash(o, r):
                gap = MIN_GAP.get(r[6])
                if gap is None:
                    return []
                return [q for q in taken[o.sid] if q[6] == r[6] and abs(q[0] - r[0]) < gap and q is not r]

            for r in s.rays:
                done = False
                fits = sorted(((self._fit(o, r, {}), o) for o in others), key=lambda co: co[0])
                for c, o in fits:
                    if c >= gate:
                        break
                    cl = clash(o, r)
                    if not cl:
                        moves.append((r, None, o))
                        if r[6] in MIN_GAP:
                            taken[o.sid].append(r)
                        done = True
                        break
                    if len(cl) == 1:
                        # one shot in the way: it may belong to a third source (an earlier mix-up); swap it there
                        q = cl[0]
                        for o2 in others:
                            if o2 is o or self._fit(o2, q, {}) >= gate or clash(o2, q):
                                continue
                            moves.append((q, o, o2))
                            taken[o.sid].remove(q)
                            taken[o2.sid].append(q)
                            moves.append((r, None, o))
                            taken[o.sid].append(r)
                            done = True
                            break
                        if done:
                            break
                if not done:
                    moves = None
                    break
            if not moves:
                continue
            touched = set()
            for r, src, o in moves:
                if src is not None and r in src.rays:
                    src.rays.remove(r)
                    touched.add(src.sid)
                r[7] = o.sid
                o.rays.append(r)
                touched.add(o.sid)
            for o in others:
                if o.sid in touched:
                    o.rays.sort(key=lambda r: r[0])
                    o.first_t = min(o.first_t, s.first_t)
                    self._refresh(o, self.w.t - self.p["ray_keep_s"])
                    self._solve(o)
            del self.sources[s.sid]
            live = [x for x in live if x is not s]

    def _nearest_by_bearing(self, b, lo, hi, fire_only=False):
        w = self.w
        best, bc = None, 3.0
        for s in self.sources.values():
            if s.dead_t is not None or (fire_only and s.fire_t < w.t - 8.0):
                continue
            dx, dy = s.x - w.x, s.y - w.y
            d = math.hypot(dx, dy)
            sp, sa = self._spread(s, b, w.t)
            sig = math.hypot(self.sigma, sp / max(d, 1.0))
            c = abs(ang_diff(math.atan2(dy, dx), b)) / sig
            if d < lo - 30 - 1.5 * sa or d > hi + 30 + 1.5 * sa:
                c += 1.5
            if c < bc:
                best, bc = s, c
        return best

    def _open(self, s):
        """Can still take new sounds: alive, or its death is under 2 s old and not yet confirmed by silence."""
        return s.dead_t is None or self.w.t - s.dead_t < 2.0

    def _death(self, ts, b, lo, hi):
        """A death sound comes from the victim. Mark the best-fitting shooter dead; keep the runners-up in case it
        fires again (then the death was someone else's)."""
        w = self.w
        cands = []
        for s in self.sources.values():
            if s.dead_t is not None or s.fire_t < w.t - 8.0:
                continue
            dx, dy = s.x - w.x, s.y - w.y
            d = math.hypot(dx, dy)
            sp, sa = self._spread(s, b, w.t)
            c = abs(ang_diff(math.atan2(dy, dx), b)) / math.hypot(self.sigma, sp / max(d, 1.0))
            if d < lo - 30 - 1.5 * sa or d > hi + 30 + 1.5 * sa:
                c += 1.5
            if c < 4.0:
                cands.append((c, s.sid))
        if not cands:
            return
        cands.sort()
        s = self.sources[cands[0][1]]
        s.dead_t = ts
        s.dead_alt = cands[1:]

    def _check_deaths(self):
        """A 'dead' source that fires after its death sound was not the one that died: pass the death on to the
        runner-up that has been silent since."""
        for s in list(self.sources.values()):
            if s.dead_t is None or s.fire_t <= s.dead_t + 0.15:
                continue
            if sum(1 for r in s.rays if r[0] > s.dead_t + 0.15 and r[6] in FIRE) < 2:
                continue                # one stray ray is bearing noise; a live mech keeps firing
            ts, alts = s.dead_t, s.dead_alt
            s.dead_t, s.dead_alt = None, []
            for c, sid in alts:
                o = self.sources.get(sid)
                if o is not None and o.dead_t is None and o.fire_t <= ts + 0.15:
                    o.dead_t = ts
                    o.dead_alt = [(c2, s2) for c2, s2 in alts if s2 != sid]
                    break

    # ---------------------------------------------------------------- tracks
    def _link_tracks(self):
        trk = self.trk
        w = self.w
        if trk is None:
            return
        tracks = [tr for k, tr in trk.tracks.items() if not isinstance(k, str)]
        for k, tr in trk.tracks.items():
            if isinstance(k, str):
                continue
            h = self._hp.setdefault(k, [])
            if w.t - tr.get("seen_t", -99) < 0.3 and tr.get("hp") is not None and (not h or h[-1][1] != tr["hp"]):
                h.append((w.t, tr["hp"]))
                if len(h) > 8:
                    del h[0]
        for s in self.sources.values():
            if not s.solved:
                continue
            best, bd = None, None
            for tr in tracks:
                ex, ey, unc, age = trk.est(tr)
                if age > 6.0:
                    continue
                d = math.hypot(ex - s.x, ey - s.y)
                lim = self.p["track_link_m"] + min(s.unc, 80.0) + min(unc, 40.0)
                if d < lim and (bd is None or d < bd):
                    best, bd = tr, d
            if best is None:
                s.track_id = None
                s.facing = None
                continue
            s.track_id = best["id"]
            ex, ey, unc, age = trk.est(best)
            if age < 1.0:                       # seen just now: the track beats the sound solve
                s.x, s.y, s.unc = ex, ey, max(3.0, unc)
                s.facing = best.get("aim")
            elif age < 3.0:
                s.facing = best.get("aim")
            else:
                s.facing = None
            mh = best.get("max_hp") or 1400
            if best.get("hp") is not None:
                s.hp_frac = best["hp"] / float(mh)

    def hp_drop(self, track_id, within=2.0):
        """HP a tracked mech lost in the last `within` seconds (0 if unknown)."""
        h = self._hp.get(track_id)
        if not h:
            return 0.0
        t = self.w.t
        old = [hp for (ts, hp) in h if t - ts <= within]
        if not old:
            return 0.0
        return max(0.0, max(old) - h[-1][1])

    # ---------------------------------------------------------------- fights
    def _build_fights(self):
        w, p = self.w, self.p
        t = w.t
        act = p["fight_active_s"]
        shooters = [s for s in self.sources.values()
                    if (t - s.fire_t < act or (s.dead_t is not None and t - s.dead_t < 10.0 and s.shots))
                    and t - s.at_us_t > 3.0]
        # connected groups of shooters within fight range of each other
        groups, seen = [], set()
        for s in shooters:
            if s.sid in seen:
                continue
            g, stack = [], [s]
            seen.add(s.sid)
            while stack:
                a = stack.pop()
                g.append(a)
                for b in shooters:
                    if b.sid not in seen and math.hypot(a.x - b.x, a.y - b.y) <= p["fight_link_m"] + 0.5 * (a.unc + b.unc):
                        seen.add(b.sid)
                        stack.append(b)
            groups.append(g)
        fights = []
        live_keys = set()
        for g in groups:
            g.sort(key=lambda s: -len(s.shots))
            parts = []
            for s in g:
                recent = [x for x in s.shots if t - x <= 2.0]
                silent = t - s.fire_t
                parts.append({
                    "sid": s.sid, "x": s.x, "y": s.y, "unc": s.unc,
                    "kinds": sorted(k for k in s.kinds if k in FIRE),
                    "shots": len(s.shots), "rate": len(recent) / 2.0,
                    "power": sum(HEAVY.get(k, 0.5) * n for k, n in s.kinds.items() if k in FIRE) /
                             max(1.0, (t - s.first_t)),
                    "last_t": s.fire_t, "silent_s": silent,
                    "dead": s.dead_t is not None, "track_id": s.track_id, "facing": s.facing,
                    "hp_frac": s.hp_frac, "hp_drop": self.hp_drop(s.track_id) if s.track_id is not None else 0.0,
                    "hits": s.hits,
                })
            key = min(s.sid for s in g)
            live_keys.add(key)
            started = self._fight_start.setdefault(key, min(s.first_t for s in g))
            wsum = sum(1.0 / (1.0 + q["unc"]) for q in parts)
            cx = sum(q["x"] / (1.0 + q["unc"]) for q in parts) / wsum
            cy = sum(q["y"] / (1.0 + q["unc"]) for q in parts) / wsum
            last = max(q["last_t"] for q in parts)
            span = max(1.0, min(4.0, t - started))
            shots_recent = sum(1 for s in g for x in s.shots if t - x <= span)
            # cracked: a death sound on a participant, or one side burst then went quiet while the other kept firing
            loser = None
            for q in parts:
                if q["dead"]:
                    loser = q["sid"]
                    break
            if loser is None and len(parts) >= 2:
                for q in parts:
                    others_firing = any(o is not q and o["silent_s"] < 1.5 for o in parts)
                    if q["shots"] >= p["min_burst"] and q["silent_s"] > p["silence_s"] and others_firing:
                        loser = q["sid"]
                        break
            winner = None
            if loser is not None:
                alive = [q for q in parts if q["sid"] != loser and not q["dead"]]
                if alive:
                    winner = max(alive, key=lambda q: q["shots"])["sid"]
            fights.append({
                "id": key, "participants": parts, "center": (cx, cy), "started_t": started, "last_t": last,
                "intensity": shots_recent / span, "kinds": sorted({k for q in parts for k in q["kinds"]}),
                "likely_cracked": loser is not None, "loser": loser, "winner": winner,
                "dist": math.hypot(cx - w.x, cy - w.y),
            })
        for k in [k for k in self._fight_start if k not in live_keys]:
            del self._fight_start[k]
        fights.sort(key=lambda f: f["dist"])
        self.fights = fights

    def view(self):
        return [{"id": f["id"], "c": [round(f["center"][0]), round(f["center"][1])], "n": len(f["participants"]),
                 "i": round(f["intensity"], 1), "cr": f["likely_cracked"]} for f in self.fights[:3]]


def _overlap(a, b, win=0.12):
    """Share of the shorter shot list with a shot from the other list within `win` s. One gun cannot fire twice in
    0.17 s, so a high share means two shooters firing at the same time."""
    if len(a) > len(b):
        a, b = b, a
    if not a:
        return 0.0
    j, hit = 0, 0
    for x in a:
        while j < len(b) and b[j] < x - win:
            j += 1
        if j < len(b) and abs(b[j] - x) <= win:
            hit += 1
    return hit / len(a)
