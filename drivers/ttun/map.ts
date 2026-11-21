import { readFileSync } from "node:fs"
import { join } from "node:path"

// 1 m raster of the static world: ground height, solid column (base..top) of prisms.
export class WorldMap {
  minX = 0
  minY = 0
  nx = 0
  ny = 0
  ground!: Float32Array
  top!: Float32Array // max prism top over the cell (-inf if none)
  base!: Float32Array // min prism base over the cell
  // navigation grid (2 m cells)
  nnx = 0
  nny = 0
  walk!: Uint8Array
  dist!: Float32Array
  heap!: Int32Array
  goalKey = ""

  constructor(dir: string) {
    const world = JSON.parse(readFileSync(join(dir, "v2/world.json"), "utf8"))
    const t = world.terrain
    this.minX = t.minX
    this.minY = t.minY
    this.nx = t.nx
    this.ny = t.ny
    const raw = readFileSync(join(dir, "v2/" + (t.file ?? "terrain.i16")))
    const i16 = new Int16Array(raw.buffer, raw.byteOffset, raw.byteLength >> 1)
    const n = this.nx * this.ny
    this.ground = new Float32Array(n)
    const sc = t.scale ?? 0.01
    for (let i = 0; i < n; i++) this.ground[i] = i16[i] * sc
    this.top = new Float32Array(n).fill(-1e9)
    this.base = new Float32Array(n).fill(1e9)
    for (const p of world.prisms) this.raster(p)
    this.buildNav()
  }

  private raster(p: any) {
    const ring: number[] = p.ring
    const holes: number[][] = (p.holes ?? []).map((h: any) => (Array.isArray(h[0]) ? h.flat() : h))
    let y0 = 1e9, y1 = -1e9
    for (let i = 1; i < ring.length; i += 2) {
      y0 = Math.min(y0, ring[i])
      y1 = Math.max(y1, ring[i])
    }
    const j0 = Math.max(0, Math.floor(y0 - this.minY)), j1 = Math.min(this.ny - 1, Math.ceil(y1 - this.minY))
    for (let j = j0; j <= j1; j++) {
      const y = this.minY + j + 0.0001
      const xs: number[] = []
      const polys = [ring, ...holes]
      for (const poly of polys) {
        const m = poly.length >> 1
        for (let a = 0; a < m; a++) {
          const b = (a + 1) % m
          const ax = poly[2 * a], ay = poly[2 * a + 1], bx = poly[2 * b], by = poly[2 * b + 1]
          if ((ay <= y && by > y) || (by <= y && ay > y)) xs.push(ax + ((y - ay) / (by - ay)) * (bx - ax))
        }
      }
      xs.sort((a, b) => a - b)
      for (let k = 0; k + 1 < xs.length; k += 2) {
        const i0 = Math.max(0, Math.ceil(xs[k] - this.minX)), i1 = Math.min(this.nx - 1, Math.floor(xs[k + 1] - this.minX))
        for (let i = i0; i <= i1; i++) {
          const idx = j * this.nx + i
          if (p.top > this.top[idx]) this.top[idx] = p.top
          if (p.base < this.base[idx]) this.base[idx] = p.base
        }
      }
    }
  }

  idx(x: number, y: number) {
    let i = Math.round(x - this.minX), j = Math.round(y - this.minY)
    if (i < 0) i = 0
    else if (i >= this.nx) i = this.nx - 1
    if (j < 0) j = 0
    else if (j >= this.ny) j = this.ny - 1
    return j * this.nx + i
  }

  groundAt(x: number, y: number) {
    const fx = x - this.minX, fy = y - this.minY
    let i = Math.floor(fx), j = Math.floor(fy)
    if (i < 0) i = 0
    if (j < 0) j = 0
    if (i > this.nx - 2) i = this.nx - 2
    if (j > this.ny - 2) j = this.ny - 2
    const tx = Math.min(1, Math.max(0, fx - i)), ty = Math.min(1, Math.max(0, fy - j))
    const g = this.ground, n = this.nx, k = j * n + i
    return (g[k] * (1 - tx) + g[k + 1] * tx) * (1 - ty) + (g[k + n] * (1 - tx) + g[k + n + 1] * tx) * ty
  }

  // Surface a mech would stand on at (x,y) given it is around height z.
  surfaceAt(x: number, y: number) {
    const k = this.idx(x, y)
    const g = this.groundAt(x, y)
    if (this.top[k] > g + 0.5 && this.base[k] < g + 1) return this.top[k]
    return g
  }

  // true if the segment a->b clears all prisms and terrain (sampled ~every 1 m).
  los(ax: number, ay: number, az: number, bx: number, by: number, bz: number) {
    const dx = bx - ax, dy = by - ay, dz = bz - az
    const len = Math.hypot(dx, dy)
    const steps = Math.max(1, Math.ceil(len))
    for (let s = 1; s < steps; s++) {
      const f = s / steps
      const x = ax + dx * f, y = ay + dy * f, z = az + dz * f
      const k = this.idx(x, y)
      if (z < this.top[k] && z > this.base[k]) return false
      if (z < this.ground[k] - 0.3) return false
    }
    return true
  }

  // ---------------- navigation (2 m cells, ground-level only)
  private buildNav() {
    this.nnx = Math.ceil(this.nx / 2)
    this.nny = Math.ceil(this.ny / 2)
    const N = this.nnx * this.nny
    this.walk = new Uint8Array(N)
    for (let J = 0; J < this.nny; J++)
      for (let I = 0; I < this.nnx; I++) {
        let ok = 1
        // the mech is r=2.5: require clearance in a 5x5 m window
        for (let dj = -2; dj <= 3 && ok; dj++)
          for (let di = -2; di <= 3 && ok; di++) {
            const i = I * 2 + di, j = J * 2 + dj
            if (i < 0 || j < 0 || i >= this.nx || j >= this.ny) {
              ok = 0
              break
            }
            const k = j * this.nx + i
            const g = this.ground[k]
            if (this.top[k] > g + 1.2 && this.base[k] < g + 9.5) ok = 0
          }
        if (ok) {
          // slope check against neighbours
          const i = Math.min(this.nx - 3, I * 2), j = Math.min(this.ny - 3, J * 2)
          const g = this.ground[j * this.nx + i]
          if (Math.abs(this.ground[j * this.nx + i + 2] - g) > 2.2 || Math.abs(this.ground[(j + 2) * this.nx + i] - g) > 2.2) ok = 0
        }
        this.walk[J * this.nnx + I] = ok
      }
    this.dist = new Float32Array(N).fill(1e9)
    this.heap = new Int32Array(N * 4)
  }

  cellOf(x: number, y: number) {
    let I = Math.round((x - this.minX) / 2), J = Math.round((y - this.minY) / 2)
    I = Math.max(0, Math.min(this.nnx - 1, I))
    J = Math.max(0, Math.min(this.nny - 1, J))
    return J * this.nnx + I
  }

  // Time-sliced Dijkstra (8-conn) distance field from a goal. field() requests a goal,
  // pump() spends a bounded amount of time on it; steer() reads the last finished field.
  work!: Float32Array
  hk!: Float32Array
  hn = 0
  pendingKey = ""
  pendingGoal: [number, number] = [0, 0]
  readyGoal: [number, number] | null = null

  field(gx: number, gy: number) {
    const key = `${Math.round(gx / 5)},${Math.round(gy / 5)}`
    if (key === this.pendingKey) return
    if (!this.pendingKey && key === this.goalKey) return
    // don't thrash: if a computation is running for a nearby goal, let it finish
    if (this.pendingKey && Math.hypot(gx - this.pendingGoal[0], gy - this.pendingGoal[1]) < 25) return
    const W = this.nnx, H = this.nny, walk = this.walk
    if (!this.work) {
      this.work = new Float32Array(this.dist.length)
      this.hk = new Float32Array(this.heap.length)
    }
    this.work.fill(1e9)
    this.hn = 0
    let g = this.cellOf(gx, gy)
    if (!walk[g]) {
      let best = -1, bd = 1e9
      const gi = g % W, gj = (g / W) | 0
      for (let r = 1; r < 30 && best < 0; r++)
        for (let dj = -r; dj <= r; dj++)
          for (let di = -r; di <= r; di++) {
            const i = gi + di, j = gj + dj
            if (i < 0 || j < 0 || i >= W || j >= H) continue
            const k = j * W + i
            if (walk[k] && di * di + dj * dj < bd) {
              bd = di * di + dj * dj
              best = k
            }
          }
      if (best >= 0) g = best
    }
    this.work[g] = 0
    this.push(g, 0)
    this.pendingKey = key
    this.pendingGoal = [gx, gy]
  }

  private push(k: number, v: number) {
    const hk = this.hk, hv = this.heap
    let i = this.hn++
    while (i > 0) {
      const p = (i - 1) >> 1
      if (hk[p] <= v) break
      hk[i] = hk[p]
      hv[i] = hv[p]
      i = p
    }
    hk[i] = v
    hv[i] = k
  }

  private pop() {
    const hk = this.hk, hv = this.heap
    const top = hv[0]
    const n = --this.hn
    const lv = hk[n], lk = hv[n]
    let i = 0
    while (true) {
      let c = 2 * i + 1
      if (c >= n) break
      if (c + 1 < n && hk[c + 1] < hk[c]) c++
      if (hk[c] >= lv) break
      hk[i] = hk[c]
      hv[i] = hv[c]
      i = c
    }
    hk[i] = lv
    hv[i] = lk
    return top
  }

  // maxIter bounds the work per call (deterministic); budgetMs is a wall-clock safety cap.
  pump(budgetMs: number, maxIter = Infinity) {
    if (!this.pendingKey) return
    const W = this.nnx, H = this.nny, d = this.work, walk = this.walk, hk = this.hk
    const D = Math.SQRT2
    const t0 = performance.now()
    let iter = 0
    while (this.hn > 0) {
      if (++iter > maxIter) return
      if ((iter & 1023) === 0 && performance.now() - t0 > budgetMs) return
      if (this.hn > this.heap.length - 16) break
      const v0 = hk[0]
      const k = this.pop()
      if (v0 > d[k]) continue
      const i = k % W, j = (k / W) | 0
      for (let dj = -1; dj <= 1; dj++)
        for (let di = -1; di <= 1; di++) {
          if (!di && !dj) continue
          const ii = i + di, jj = j + dj
          if (ii < 0 || jj < 0 || ii >= W || jj >= H) continue
          const kk = jj * W + ii
          if (!walk[kk]) continue
          if (di && dj && (!walk[j * W + ii] || !walk[jj * W + i])) continue
          const nv = v0 + (di && dj ? D : 1)
          if (nv < d[kk]) {
            d[kk] = nv
            this.push(kk, nv)
          }
        }
    }
    const tmp = this.dist
    this.dist = this.work
    this.work = tmp
    this.goalKey = this.pendingKey
    this.readyGoal = this.pendingGoal
    this.pendingKey = ""
  }

  // True when the finished field is for a goal near (gx, gy).
  fieldFor(gx: number, gy: number, tol = 20) {
    return !!this.readyGoal && Math.hypot(gx - this.readyGoal[0], gy - this.readyGoal[1]) < tol
  }

  // Direction to follow the current distance field from (x,y); null if no path info.
  steer(x: number, y: number): [number, number] | null {
    const W = this.nnx, H = this.nny, d = this.dist
    const c = this.cellOf(x, y)
    const ci = c % W, cj = (c / W) | 0
    let best = d[c], bi = 0, bj = 0
    // look a few cells out for the lowest reachable value along straight lines
    for (let a = 0; a < 24; a++) {
      const ang = (a / 24) * Math.PI * 2
      const ca = Math.cos(ang), sa = Math.sin(ang)
      for (let r = 1; r <= 5; r++) {
        const i = Math.round(ci + ca * r), j = Math.round(cj + sa * r)
        if (i < 0 || j < 0 || i >= W || j >= H) break
        const k = j * W + i
        if (!this.walk[k]) break
        const v = d[k] + r * 0.02
        if (v < best) {
          best = v
          bi = i - ci
          bj = j - cj
        }
      }
    }
    if (!bi && !bj) return null
    const l = Math.hypot(bi, bj)
    return [bi / l, bj / l]
  }

  // Nearby walkable point that hides from (ex,ey,ez) eye, searching rings around (x,y).
  coverFrom(x: number, y: number, ex: number, ey: number, ez: number, maxR = 40): [number, number] | null {
    let best: [number, number] | null = null, bs = 1e9
    for (let r = 6; r <= maxR; r += 6)
      for (let a = 0; a < 16; a++) {
        const ang = (a / 16) * Math.PI * 2
        const px = x + Math.cos(ang) * r, py = y + Math.sin(ang) * r
        if (!this.walk[this.cellOf(px, py)]) continue
        const pz = this.groundAt(px, py)
        if (this.los(ex, ey, ez, px, py, pz + 6)) continue
        const away = Math.hypot(px - ex, py - ey) - Math.hypot(x - ex, y - ey)
        const s = r - away * 0.3
        if (s < bs) {
          bs = s
          best = [px, py]
        }
      }
    return best
  }
}
