"""Map model for Mech Royale: terrain height, building columns on a 2 m grid, line of sight, and a
flow field (BFS) toward a goal on a 4 m walk grid. Standard library only; built once at hello."""
import array
import json
import math
import os
from collections import deque

CELL = 2.0     # fine grid for line of sight and wall columns
WCELL = 4.0    # walk grid for the flow field


class Map:
    def __init__(self, mdir):
        w = json.load(open(os.path.join(mdir, "v2", "world.json")))
        t = w["terrain"]
        self.tx0, self.ty0, self.tstep = t["minX"], t["minY"], t["step"]
        self.tnx, self.tny = t["nx"], t["ny"]
        self.tsc = t.get("scale", 0.01)
        h = array.array("h")
        with open(os.path.join(mdir, "v2", t["file"]), "rb") as f:
            h.frombytes(f.read())
        self.th = h
        b = w["bounds"]
        self.x0, self.y0 = b["minX"], b["minY"]
        self.x1, self.y1 = b["maxX"], b["maxY"]
        self.nx = int((self.x1 - self.x0) / CELL) + 1
        self.ny = int((self.y1 - self.y0) / CELL) + 1
        self.cols = {}  # cell index -> list of (base, top)
        for p in w["prisms"]:
            self._fill(p)
        self._walk_grid()
        self._trees(w)
        self._roofs()
        self.flow = None
        self.flow_goal = None
        self._job = None

    # ---------- trees (vision only: crowns fade sight, trunks stop it; shots ignore trees)
    def _trees(self, w):
        tr = w.get("trees") or {}
        f = tr.get("fields") or []
        data = tr.get("data") or []
        rules = w.get("rules") or {}
        self.canopy_density = rules.get("canopyDensity", 0.35)
        self.canopy_block_t = rules.get("canopyBlockT", 0.25)
        self.tgrid = {}
        if not f:
            return
        n = len(f)
        ix = {k: i for i, k in enumerate(f)}
        for i in range(0, len(data) - n + 1, n):
            r = data[i:i + n]
            z = r[ix["z"]]
            t = (r[ix.get("crownX", ix["x"])], r[ix.get("crownY", ix["y"])], r[ix["crownR"]], z + r[ix["crownBase"]],
                 z + r[ix["height"]], r[ix["x"]], r[ix["y"]], r[ix["trunkR"]], z)
            cx, cy = int(t[0] // 20), int(t[1] // 20)
            for gx in (cx - 1, cx, cx + 1):
                for gy in (cy - 1, cy, cy + 1):
                    self.tgrid.setdefault((gx, gy), []).append(t)

    def _roofs(self):
        """Flat roofs 8-28 m above the ground with room to stand back from the edge (hidden from below).
        One spot per 6 m block, kept in a 30 m grid: self.roof_grid[(gx, gy)] -> [(x, y, top)]."""
        self.roof_grid = {}
        nx = self.nx
        tops = {}
        for k, cs in self.cols.items():
            j, i = divmod(k, nx)
            x, y = self.x0 + (i + 0.5) * CELL, self.y0 + (j + 0.5) * CELL
            g = self.ground(x, y)
            t = max((tp for b, tp in cs if b <= g + 1.5), default=None)
            if t is not None and 8.0 <= t - g <= 28.0:
                tops[k] = t
        seen = set()
        for k, t in tops.items():
            j, i = divmod(k, nx)
            blk = (i // 3, j // 3)
            if blk in seen:
                continue
            ok = True
            for dj in (-2, -1, 0, 1, 2):
                for di in (-2, -1, 0, 1, 2):
                    t2 = tops.get((j + dj) * nx + (i + di))
                    if t2 is None or abs(t2 - t) > 0.6:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                continue
            seen.add(blk)
            x, y = self.x0 + (i + 0.5) * CELL, self.y0 + (j + 0.5) * CELL
            self.roof_grid.setdefault((int(x // 30), int(y // 30)), []).append((x, y, t))

    def roofs_near(self, x, y, r):
        out = []
        gx, gy = int(x // 30), int(y // 30)
        k = int(r // 30) + 1
        for a in range(gx - k, gx + k + 1):
            for b in range(gy - k, gy + k + 1):
                for s in self.roof_grid.get((a, b), ()):
                    if (s[0] - x) ** 2 + (s[1] - y) ** 2 <= r * r:
                        out.append(s)
        return out

    def trees_near(self, x, y):
        return self.tgrid.get((int(x // 20), int(y // 20)), ())

    def under_canopy(self, x, y):
        for t in self.trees_near(x, y):
            if (x - t[0]) ** 2 + (y - t[1]) ** 2 < t[2] * t[2]:
                return True
        return False

    def tree_sight(self, a, b):
        """True if trees let a see b: crown path length L gives T = exp(-density * L), blocked under canopyBlockT;
        a trunk on the line blocks. An estimate of the game's rule (its exact formula is not published)."""
        if not self.tgrid:
            return True
        ax, ay, az = a
        bx, by, bz = b
        dx, dy, dz = bx - ax, by - ay, bz - az
        L2 = dx * dx + dy * dy
        if L2 < 1e-6:
            return True
        L = math.sqrt(L2)
        seen = set()
        path = 0.0
        steps = int(L // 15) + 1
        for k in range(steps + 1):
            px, py = ax + dx * k / steps, ay + dy * k / steps
            for t in self.trees_near(px, py):
                if id(t) in seen:
                    continue
                seen.add(id(t))
                # trunk: closest approach to the trunk axis within the trunk's height
                u = ((t[5] - ax) * dx + (t[6] - ay) * dy) / L2
                if 0.0 < u < 1.0:
                    qx, qy, qz = ax + dx * u, ay + dy * u, az + dz * u
                    if (qx - t[5]) ** 2 + (qy - t[6]) ** 2 < t[7] * t[7] and t[8] <= qz <= t[3]:
                        return False
                # crown: chord through the crown circle, kept where the line is between crown base and top
                u = ((t[0] - ax) * dx + (t[1] - ay) * dy) / L2
                qx, qy = ax + dx * u, ay + dy * u
                h2 = t[2] * t[2] - ((qx - t[0]) ** 2 + (qy - t[1]) ** 2)
                if h2 <= 0:
                    continue
                half = math.sqrt(h2) / L
                u0, u1 = max(0.0, u - half), min(1.0, u + half)
                if u1 <= u0:
                    continue
                z0, z1 = az + dz * u0, az + dz * u1
                lo, hi = max(min(z0, z1), t[3]), min(max(z0, z1), t[4])
                if hi <= lo and not (t[3] <= z0 <= t[4]):
                    continue
                frac = 1.0 if abs(z1 - z0) < 1e-6 else max(0.0, min(1.0, (hi - lo) / abs(z1 - z0)))
                path += (u1 - u0) * L * frac
        return math.exp(-self.canopy_density * path) >= self.canopy_block_t

    # ---------- terrain
    def ground(self, x, y):
        fx = (x - self.tx0) / self.tstep
        fy = (y - self.ty0) / self.tstep
        i = min(max(int(fx), 0), self.tnx - 2)
        j = min(max(int(fy), 0), self.tny - 2)
        ax, ay = min(max(fx - i, 0.0), 1.0), min(max(fy - j, 0.0), 1.0)
        n = self.tnx
        h = self.th
        k = j * n + i
        a = h[k] * (1 - ax) + h[k + 1] * ax
        c = h[k + n] * (1 - ax) + h[k + n + 1] * ax
        return (a * (1 - ay) + c * ay) * self.tsc

    # ---------- prisms -> columns (scanline, even-odd so holes cut out)
    def _fill(self, p):
        rings = [p["ring"]] + list(p.get("holes") or [])
        edges = []
        ymin, ymax = 1e9, -1e9
        for r in rings:
            n = len(r) // 2
            for k in range(n):
                ax, ay = r[2 * k], r[2 * k + 1]
                bx, by = r[2 * ((k + 1) % n)], r[2 * ((k + 1) % n) + 1]
                if ay != by:
                    edges.append((ax, ay, bx, by))
                ymin, ymax = min(ymin, ay), max(ymax, ay)
        if not edges:
            return
        base, top = p["base"], p["top"]
        j0 = max(0, int((ymin - self.y0) / CELL))
        j1 = min(self.ny - 1, int((ymax - self.y0) / CELL) + 1)
        for j in range(j0, j1 + 1):
            yc = self.y0 + (j + 0.5) * CELL
            xs = []
            for ax, ay, bx, by in edges:
                if (ay <= yc < by) or (by <= yc < ay):
                    xs.append(ax + (yc - ay) * (bx - ax) / (by - ay))
            xs.sort()
            for k in range(0, len(xs) - 1, 2):
                i0 = max(0, int(math.ceil((xs[k] - self.x0) / CELL - 0.5)))
                i1 = min(self.nx - 1, int(math.floor((xs[k + 1] - self.x0) / CELL - 0.5)))
                for i in range(i0, i1 + 1):
                    self.cols.setdefault(j * self.nx + i, []).append((base, top))

    def solid(self, x, y, z):
        i = int((x - self.x0) / CELL)
        j = int((y - self.y0) / CELL)
        if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
            return True
        c = self.cols.get(j * self.nx + i)
        if c:
            for b, t in c:
                if b <= z <= t:
                    return True
        return False

    def los(self, a, b, step=CELL):
        """True when the segment a->b (3D points) passes no building column and stays above ground."""
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        d = math.hypot(dx, dy)
        n = int(d / step)
        if n < 1:
            return True
        cols = self.cols
        x0, y0, nx = self.x0, self.y0, self.nx
        for k in range(1, n):
            f = k / n
            x, y, z = a[0] + dx * f, a[1] + dy * f, a[2] + dz * f
            c = cols.get(int((y - y0) / CELL) * nx + int((x - x0) / CELL))
            if c:
                for bb, tt in c:
                    if bb <= z <= tt:
                        return False
            if (k & 3) == 0 and z < self.ground(x, y) - 0.3:
                return False
        return True

    # ---------- walk grid and flow field
    def _walk_grid(self):
        self.wnx = int((self.x1 - self.x0) / WCELL) + 1
        self.wny = int((self.y1 - self.y0) / WCELL) + 1
        blk = bytearray(self.wnx * self.wny)
        r = int(WCELL / CELL)
        for j in range(self.wny):
            for i in range(self.wnx):
                cx, cy = self.x0 + (i + 0.5) * WCELL, self.y0 + (j + 0.5) * WCELL
                g = self.ground(cx, cy)
                bad = False
                for dj in range(r):
                    for di in range(r):
                        c = self.cols.get((j * r + dj) * self.nx + i * r + di)
                        if c:
                            for b, t in c:
                                if b < g + 9.0 and t > g + 1.2:
                                    bad = True
                if bad:
                    blk[j * self.wnx + i] = 1
        # grow by one cell so a 2.5 m radius body clears corners
        grown = bytearray(blk)
        W = self.wnx
        for j in range(1, self.wny - 1):
            for i in range(1, W - 1):
                if not blk[j * W + i] and (blk[j * W + i - 1] or blk[j * W + i + 1] or blk[(j - 1) * W + i] or blk[(j + 1) * W + i]):
                    grown[j * W + i] = 2
        self.blk = grown

    def wcell(self, x, y):
        i = min(max(int((x - self.x0) / WCELL), 0), self.wnx - 1)
        j = min(max(int((y - self.y0) / WCELL), 0), self.wny - 1)
        return j * self.wnx + i

    def start_flow(self, gx, gy):
        """Begin a BFS from the goal; advance it with step_flow() a slice per tick."""
        n = self.wnx * self.wny
        dist = array.array("i", [-1]) * n
        g = self.wcell(gx, gy)
        dist[g] = 0
        self._job = (dist, deque([g]), (gx, gy))

    def step_flow(self, budget=6000):
        if not self._job:
            return
        dist, q, goal = self._job
        W, H, blk = self.wnx, self.wny, self.blk
        k = 0
        while q and k < budget:
            c = q.popleft()
            k += 1
            dc = dist[c] + 1
            i = c % W
            for nb in (c - 1 if i > 0 else -1, c + 1 if i < W - 1 else -1, c - W, c + W):
                if 0 <= nb < W * H and dist[nb] < 0:
                    if blk[nb] == 1:
                        dist[nb] = -2
                        continue
                    dist[nb] = dc
                    q.append(nb)
        if not q:
            self.flow, self.flow_goal, self._job = dist, goal, None

    def flow_dir(self, x, y):
        """Unit direction to step toward the flow goal, or None if no field / unreachable."""
        if self.flow is None:
            return None
        W = self.wnx
        c = self.wcell(x, y)
        d0 = self.flow[c]
        best, bc = d0 if d0 >= 0 else 1 << 30, None
        i = c % W
        for di, dj in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, -1), (-1, 1), (1, 1)):
            if not (0 <= i + di < W):
                continue
            nb = c + di + dj * W
            if 0 <= nb < len(self.flow):
                v = self.flow[nb]
                if 0 <= v < best:
                    best, bc = v, nb
        if bc is None:
            return None
        tx = self.x0 + (bc % W + 0.5) * WCELL
        ty = self.y0 + (bc // W + 0.5) * WCELL
        dx, dy = tx - x, ty - y
        n = math.hypot(dx, dy) or 1.0
        return dx / n, dy / n
