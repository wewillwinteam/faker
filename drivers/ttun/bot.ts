import { type ActorObs, type Hello, type Observation, type BotAction, draw, log, run } from "./mechfight.ts"
import { WorldMap } from "./map.ts"

const TAU = Math.PI * 2
const wrap = (a: number) => {
  a = (a + Math.PI) % TAU
  if (a < 0) a += TAU
  return a - Math.PI
}
const hyp2 = (ax: number, ay: number, bx: number, by: number) => Math.hypot(bx - ax, by - ay)

type Track = { id: number; pos: number[]; vel: number[]; hp: number; maxHp: number; t: number; weapons: string[]; charging: boolean; aim: number[]; cubes: number }

const WEAPON_PREF: Record<string, number> = { laser: 3.2, lightning: 2.5, mortar: 1, gun: 0 }
const UPGRADE_PREF = [
  "plating", "armor", "nanites", "lightning_efficiency", "laser_damage", "laser_charge", "gun_damage", "gun_rate",
  "lightning_range", "lightning_chain", "gyros", "servos", "gun_mag", "capacitor", "reactor", "jets", "gun_velocity",
  "sensors", "mortar_reload", "mortar_velocity", "mortar_splash", "laser_pierce",
]

class Brain {
  name = "TTUN-mech"
  cfg: any
  map: WorldMap | null = null
  tracks = new Map<number, Track>()
  strafeDir = 1
  strafeUntil = 0
  lastPos: number[] = [0, 0, 0]
  stuckT = 0
  lastDamageT = -99
  lastDamageBearing = 0
  threatBearing: number | null = null
  threatT = -99
  rng: () => number
  lastHp = 0
  scanPhase = 0
  laserHold = false
  attackers = new Map<number, number>() // id -> last time they hit us
  hideUntil = 0
  mechHitT = -99
  targetId = -1
  targetT = -99
  mechHitBearing = 0
  npcMem = new Map<number, { pos: number[]; vel: number[]; t: number; hp: number }>()
  cubeMem = new Map<number, { pos: number[]; t: number }>()
  pickMem = new Map<number, { pos: number[]; kind: string; t: number }>()

  constructor(hello: Hello) {
    this.cfg = hello.config
    let s = (hello.seed >>> 0) || 1
    this.rng = () => {
      s ^= s << 13
      s >>>= 0
      s ^= s >>> 17
      s ^= s << 5
      s >>>= 0
      return s / 4294967296
    }
    try {
      const t0 = Date.now()
      this.map = new WorldMap(hello.map.dir)
      // warm the JIT on the path search
      this.map.field(0, 0)
      this.map.pump(1e9)
      this.map.field(200, -100)
      this.map.pump(1e9)
      log(`map loaded in ${Date.now() - t0} ms`)
    } catch (e) {
      log("map load failed", e)
    }
  }

  act(o: Observation): BotAction {
    const t0 = performance.now()
    const me = o.self
    const [x, y, z] = me.pos
    const t = o.t
    const map = this.map
    const act: BotAction = {}
    const scale = 1 + (this.cfg.cubes?.size_per_cube ?? 0.05) * me.cubes
    const muzzleZ = z + (me.stats.muzzle_height ?? 7)
    const eyeZ = z + (me.stats.eye_height ?? 8)

    // ---------- memory
    for (const m of o.mechs) {
      if (m.kind !== "mech") continue
      this.tracks.set(m.id, {
        id: m.id, pos: m.pos, vel: m.vel, hp: m.hp ?? 1400, maxHp: m.max_hp ?? 1400, t, weapons: m.weapons ?? [],
        charging: !!m.charging, aim: m.aim, cubes: m.cubes ?? 0,
      })
    }
    for (const [id, tr] of this.tracks) if (t - tr.t > 12) this.tracks.delete(id)
    for (const e of o.events) {
      if (e.type === "damaged" && e.by != null && e.weapon !== "npc") {
        this.attackers.set(e.by, t)
        if (e.by < 16 && e.bearing != null) {
          this.mechHitT = t
          this.mechHitBearing = e.bearing
        }
      }
      if (e.type === "damaged" && e.weapon !== "zone" && e.bearing != null) {
        this.lastDamageT = t
        this.lastDamageBearing = e.bearing
        this.threatBearing = e.bearing
        this.threatT = t
      }
      if (e.type === "kill") this.tracks.delete(e.target)
    }
    for (const s of o.sounds) {
      if (["gun", "laser", "laser_charge", "lightning", "mortar", "boost", "step", "jet"].includes(s.kind) && s.dist !== "far") {
        if (t - this.threatT > 1.5) {
          this.threatBearing = s.bearing
          this.threatT = t
        }
      }
    }

    // ---------- upgrades
    if (o.offer) {
      let bi = 0, bs = 1e9
      o.offer.choices.forEach((c, i) => {
        const r = UPGRADE_PREF.indexOf(c.id)
        const s = r < 0 ? 50 : r
        if (s < bs) {
          bs = s
          bi = i
        }
      })
      act.pick = bi
    }

    // ---------- weapons held
    const w0 = me.weapons[0], w1 = me.weapons[1]
    const held = (k: string) => (w0?.kind === k ? 0 : w1?.kind === k ? 1 : -1)
    const gunS = held("gun"), laserS = held("laser"), lightS = held("lightning"), mortarS = held("mortar")

    // ---------- loot: which pickups we want. -1 = goes into an empty slot, 0/1 = slot to swap out, null = not wanted.
    // The gun is never swapped out while held.
    const wantKind = (k: string): number | null => {
      if (held(k) >= 0) return null
      if (!w0 || !w1) return -1
      let worst: number | null = null, wv = 1e9
      ;[w0, w1].forEach((w, i) => {
        if (w.kind === "gun" || (w.kind === "laser" && (w.charge ?? 0) > 0)) return
        const v = WEAPON_PREF[w.kind] ?? 0
        if (v < wv) {
          wv = v
          worst = i
        }
      })
      return worst !== null && (WEAPON_PREF[k] ?? 0) > wv + 0.5 ? worst : null
    }
    const onlyGun = !(w0 && w1)
    for (const p of o.pickups) {
      if (Math.abs(p.pos[2] - this.mapGround(p.pos[0], p.pos[1])) > 2) continue
      this.pickMem.set(p.id, { pos: p.pos, kind: p.kind, t })
    }
    for (const e of o.events) if ((e.type === "took" || e.type === "take_failed") && e.id != null) this.pickMem.delete(e.id)
    for (const [id, p] of this.pickMem) {
      if (p.t === t) continue
      if (t - p.t > 60 || hyp2(x, y, p.pos[0], p.pos[1]) < 18) this.pickMem.delete(id)
    }
    let nearPick: number[] | null = null, nearPickD = 1e9
    for (const p of this.pickMem.values()) {
      if (wantKind(p.kind) === null) continue
      const d = hyp2(x, y, p.pos[0], p.pos[1]) - (onlyGun ? 0 : (WEAPON_PREF[p.kind] ?? 0) * 5)
      if (d < nearPickD) {
        nearPickD = d
        nearPick = p.pos
      }
    }
    const reach = this.cfg.loot.pickup_radius - 0.3
    for (const p of o.pickups) {
      if (hyp2(x, y, p.pos[0], p.pos[1]) > reach || Math.abs(p.pos[2] - z) > 3.5) continue
      const slot = wantKind(p.kind)
      if (slot === null) continue
      act.take = p.id
      if (slot >= 0) act.drop = slot
      break
    }

    // ---------- choose target
    const visible = o.mechs.filter((m) => m.kind === "mech")
    let target: Track | null = null
    let tScore = 1e9
    for (const tr of this.tracks.values()) {
      const d = hyp2(x, y, tr.pos[0], tr.pos[1])
      const age = t - tr.t
      if (age > 4) continue
      const hitUs = t - (this.attackers.get(tr.id) ?? -99) < 3 ? 50 : 0
      const stick = tr.id === this.targetId && t - this.targetT < 2 ? 40 : 0
      const s = d + (tr.hp / 1400) * 90 + age * 30 - hitUs - stick
      if (s < tScore) {
        tScore = s
        target = tr
      }
    }
    if (target) {
      this.targetId = target.id
      this.targetT = t
    }
    const npcs = o.mechs.filter((m) => m.kind === "npc")
    for (const n of npcs) this.npcMem.set(n.id, { pos: n.pos, vel: n.vel, t, hp: n.hp ?? 350 })
    for (const e of o.events) if (e.type === "hit" && e.target >= 100 && e.target < 1000) {
      const nm = this.npcMem.get(e.target)
      if (nm) nm.hp -= e.amount
    }
    for (const [id, nm] of this.npcMem) if (t - nm.t > 25 || nm.hp <= 0) this.npcMem.delete(id)
    for (const c of o.cubes) {
      if (Math.abs(c.pos[2] - this.mapGround(c.pos[0], c.pos[1])) > 2) continue
      this.cubeMem.set(c.id, { pos: c.pos, t })
    }
    for (const [id, c] of this.cubeMem) {
      if (c.t === t) continue
      if (t - c.t > 40 || hyp2(x, y, c.pos[0], c.pos[1]) < 18) this.cubeMem.delete(id)
    }
    let nearCube: number[] | null = null, nearCubeD = 1e9
    for (const c of this.cubeMem.values()) {
      const d = hyp2(x, y, c.pos[0], c.pos[1])
      if (d < nearCubeD) {
        nearCubeD = d
        nearCube = c.pos
      }
    }
    let threats = 0
    for (const tr of this.tracks.values()) if (t - tr.t < 3 && hyp2(x, y, tr.pos[0], tr.pos[1]) < 160) threats++

    // ---------- zone goal
    const zn = o.zone
    const curR = zn.radius
    const shrinking = t >= zn.shrink_start
    const timeToShrink = zn.shrink_start - t
    const dNext = hyp2(x, y, zn.next_center[0], zn.next_center[1])
    const dCur = hyp2(x, y, zn.center[0], zn.center[1])
    const speed = me.stats.walk_speed ?? 11.7
    // where must we be
    let zoneGoal: [number, number] | null = null
    const needNext = dNext > zn.next_radius * 0.8 && (shrinking || timeToShrink < (dNext - zn.next_radius * 0.6) / speed + 12)
    if (!me.in_zone || dCur > curR * 0.9 || needNext) {
      const ang = Math.atan2(y - zn.next_center[1], x - zn.next_center[0])
      const r = zn.next_radius * 0.45
      zoneGoal = [zn.next_center[0] + Math.cos(ang) * r, zn.next_center[1] + Math.sin(ang) * r]
    }

    // ---------- decide
    let moveGoal: [number, number] | null = null
    let aimYaw = me.aim[0], aimPitch = 0
    let wantAim = false
    let mode = "roam"
    const fire: [boolean, boolean] = [false, false]
    const hpFrac = me.hp / me.max_hp
    let boost = false
    let jump = false
    let strafe = 0
    let laserCharging = laserS >= 0 && (me.weapons[laserS]?.charge ?? 0) > 0

    if (target) {
      const tp = target.pos
      const d = hyp2(x, y, tp[0], tp[1])
      const tScale = 1 + 0.05 * target.cubes
      const tcz = tp[2] + 4.5 * tScale
      const seen = t - target.t < 0.11
      const hasLos = map ? map.los(x, y, muzzleZ, tp[0], tp[1], tcz) : true
      mode = "fight"

      // aim solution
      const aimFor = (spd: number): [number, number] => {
        let px = tp[0], py = tp[1], pz = tcz
        const age = t - target!.t
        px += target!.vel[0] * age
        py += target!.vel[1] * age
        for (let it = 0; it < 3; it++) {
          const dd = Math.hypot(px - x, py - y, pz - muzzleZ)
          const tt = dd / spd
          px = tp[0] + target!.vel[0] * (tt + age)
          py = tp[1] + target!.vel[1] * (tt + age)
          pz = tcz + (target!.vel[2] ?? 0) * Math.min(tt, 0.3)
        }
        return [Math.atan2(py - y, px - x), Math.atan2(pz - muzzleZ, Math.hypot(px - x, py - y))]
      }
      const instant = (): [number, number] => [Math.atan2(tp[1] - y, tp[0] - x), Math.atan2(tcz - muzzleZ, d)]

      // pick weapon emphasis
      let a: [number, number]
      const gunSpeed = gunS >= 0 ? me.weapons[gunS]!.stats.speed : 252
      if (lightS >= 0 && d < (me.weapons[lightS]!.stats.range ?? 45) * 1.1) a = instant()
      else if (laserS >= 0 && (laserCharging || (me.weapons[laserS]!.cooldown ?? 0) < 0.2)) a = instant()
      else a = aimFor(gunSpeed)
      aimYaw = a[0]
      aimPitch = a[1]
      wantAim = true
      const yawErr = Math.abs(wrap(me.aim[0] - aimYaw))
      const pitchErr = Math.abs(me.aim[1] - aimPitch)
      const tol = Math.atan2(2.2 * tScale, Math.max(d, 1))

      if (seen && hasLos) {
        // gun
        if (gunS >= 0) {
          const g = me.weapons[gunS]!
          const ga = aimFor(g.stats.speed)
          const gErr = Math.abs(wrap(me.aim[0] - ga[0]))
          if (d < g.stats.range && gErr < tol * 1.2 && pitchErr < tol * 2 + 0.03 && (g.reload ?? 0) === 0) fire[gunS] = true
        }
        // lightning
        if (lightS >= 0) {
          const L = me.weapons[lightS]!
          if (d < L.stats.range - 1 && yawErr < ((L.stats.cone_deg * Math.PI) / 180) * 0.45 && me.energy > L.stats.energy + 2) fire[lightS] = true
        }
      }
      // laser: charge while target is around, release when full and aligned
      if (laserS >= 0) {
        const Lw = me.weapons[laserS]!
        const full = Lw.stats.charge
        const ch = Lw.charge ?? 0
        if (ch > 0) {
          const ready = ch >= full - 0.05
          const aligned = seen && hasLos && yawErr < tol * 0.9 && pitchErr < tol * 1.5 + 0.02
          if (ready && aligned) fire[laserS] = false
          else fire[laserS] = true
        } else if (Lw.cooldown <= 0.05 && seen && d < Lw.stats.range && (hasLos || d < 60)) {
          fire[laserS] = true
        }
      }
      // mortar
      if (mortarS >= 0) {
        const M = me.weapons[mortarS]!
        if (M.cooldown <= 0 && d > 18 && d < 220) {
          const v = M.stats.speed, g = M.stats.gravity
          const h = tp[2] - (z + 9.5 * scale)
          let tof = d / (v * 0.7)
          let px = tp[0] + target.vel[0] * tof, py = tp[1] + target.vel[1] * tof
          const dd = Math.hypot(px - x, py - y)
          const disc = v ** 4 - g * (g * dd * dd + 2 * h * v * v)
          if (disc > 0) {
            const low = Math.atan((v * v - Math.sqrt(disc)) / (g * dd))
            const high = Math.atan((v * v + Math.sqrt(disc)) / (g * dd))
            const myMortarYaw = Math.atan2(py - y, px - x)
            let pitch = hasLos ? low : high
            if (pitch > (me.stats.max_pitch ?? 1.3)) pitch = low
            // only use mortar when not using other weapons' aim this tick, or if it's the best
            if (!fire[0] && !fire[1] && !(laserS >= 0 && laserCharging)) {
              aimYaw = myMortarYaw
              aimPitch = pitch
              if (Math.abs(wrap(me.aim[0] - myMortarYaw)) < 0.03 && Math.abs(me.aim[1] - pitch) < 0.03) fire[mortarS] = true
            }
          }
        }
      }

      // movement in fight
      const myDps = (gunS >= 0 ? 150 : 0) + (lightS >= 0 && d < 45 ? 200 : 0) + (laserS >= 0 ? 120 : 0)
      const theirHp = target.hp
      const theirFrac = theirHp / target.maxHp
      const finishing = theirFrac < 0.2 && hpFrac > 0.2 && threats <= 2
      const losing = !finishing && ((hpFrac < 0.35 && theirFrac > hpFrac + 0.1) || (threats >= 2 && hpFrac < 0.55) || (threats >= 3 && hpFrac < 0.8))
      if (losing) this.hideUntil = t + 6
      const prefer = lightS >= 0 ? 28 : laserS >= 0 ? 110 : 70
      if (losing && map) {
        mode = "retreat"
        const cov = map.coverFrom(x, y, tp[0], tp[1], tp[2] + 8, 50)
        if (cov) moveGoal = cov
        else moveGoal = [x - (tp[0] - x), y - (tp[1] - y)]
        boost = me.energy > 15
      } else {
        const dirx = (tp[0] - x) / Math.max(d, 1), diry = (tp[1] - y) / Math.max(d, 1)
        let radial = 0
        if (d > prefer + 15) {
          radial = 1
          if (lightS >= 0 && d < 90 && me.energy > 70) boost = true
        }
        else if (d < prefer - 15) radial = -0.7
        if (!hasLos && seen === false) radial = 1
        if (t > this.strafeUntil) {
          this.strafeDir = this.rng() < 0.5 ? -1 : 1
          this.strafeUntil = t + 0.35 + this.rng() * 0.7
        }
        strafe = this.strafeDir
        // dodge incoming laser: hard strafe
        if (target.charging) {
          const theirYaw = target.aim[0]
          const toMe = Math.atan2(y - tp[1], x - tp[0])
          if (Math.abs(wrap(theirYaw - toMe)) < 0.25) boost = me.energy > (lightS >= 0 ? 45 : 10)
        }
        const mx = dirx * radial + -diry * strafe * 0.9
        const my = diry * radial + dirx * strafe * 0.9
        if (map && radial > 0 && !hasLos) {
          map.field(tp[0], tp[1])
          const st = map.fieldFor(tp[0], tp[1], 30) ? map.steer(x, y) : null
          if (st) moveGoal = [x + st[0] * 10 + -diry * strafe * 3, y + st[1] * 10 + dirx * strafe * 3]
          else moveGoal = [x + mx * 10, y + my * 10]
        } else moveGoal = [x + mx * 10, y + my * 10]
      }
      if (nearCube && nearCubeD < (losing ? 12 : hpFrac > 0.5 ? 70 : 40)) {
        moveGoal = [nearCube[0], nearCube[1]]
        mode += "+cube"
      } else if (nearPick && !losing && nearPickD < (onlyGun ? 60 : 35)) {
        moveGoal = [nearPick[0], nearPick[1]]
        mode += "+weapon"
      }
      if (zoneGoal && (!me.in_zone || shrinking)) {
        // zone overrides when outside
        if (!me.in_zone) moveGoal = null
      }
      void myDps
    }

    // heal: hold still (silent) out of sight until HP is back
    const healing = !moveGoal && !target && !zoneGoal && nearCubeD > 40 && (hpFrac < 0.55 || (t < this.hideUntil && hpFrac < 0.9))
    let slow = false
    if (healing) {
      mode = "heal"
      // creep (silently) into cover from the most recent threat
      let last: Track | null = null
      for (const tr of this.tracks.values()) if (!last || tr.t > last.t) last = tr
      if (last && map) {
        const exposed = map.los(last.pos[0], last.pos[1], last.pos[2] + 8, x, y, z + 6)
        if (exposed) {
          const cov = map.coverFrom(x, y, last.pos[0], last.pos[1], last.pos[2] + 8, 36)
          if (cov) {
            moveGoal = cov
            slow = true
          }
        }
      }
    }
    if (!moveGoal && !healing) {
      if (zoneGoal) {
        mode = mode === "fight" ? "fight-zone" : "zone"
        moveGoal = zoneGoal
        boost = !me.in_zone && me.energy > 20
      } else if (mode !== "fight") {
        // loot: pickups worth taking, then crates/cubes, then NPCs
        let best: [number, number] | null = null, bs = 1e9
        for (const p of this.pickMem.values()) {
          if (wantKind(p.kind) === null) continue
          const d = hyp2(x, y, p.pos[0], p.pos[1])
          const s = d - (WEAPON_PREF[p.kind] ?? 0) * 20 - (onlyGun ? 50 : 0)
          if (s < bs) {
            bs = s
            best = [p.pos[0], p.pos[1]]
          }
        }
        for (const c of this.cubeMem.values()) {
          const d = hyp2(x, y, c.pos[0], c.pos[1]) - 30
          if (d < bs) {
            bs = d
            best = [c.pos[0], c.pos[1]]
          }
        }
        // supply drops: capture when nobody else is on it
        for (const dr of o.drops ?? []) {
          const d = hyp2(x, y, dr.pos[0], dr.pos[1])
          if (dr.holder != null && dr.holder !== me.id) continue
          if (d > 220 || dr.lands_in > d / speed + 3) continue
          const zr = hyp2(dr.pos[0], dr.pos[1], zn.next_center[0], zn.next_center[1])
          if (zr > zn.next_radius && timeToShrink < 15) continue
          const sc = d - 80
          if (sc < bs) {
            bs = sc
            best = d < dr.radius - 3 ? [x, y] : [dr.pos[0], dr.pos[1]]
          }
        }
        // NPC drones: walk into gun range of the nearest remembered one
        if (hpFrac > 0.5) {
          for (const nm of this.npcMem.values()) {
            const d = hyp2(x, y, nm.pos[0], nm.pos[1])
            if (d > 170) continue
            const sc = d - 20
            if (sc < bs) {
              bs = sc
              best = d > 38 ? [nm.pos[0], nm.pos[1]] : [x, y]
            }
          }
        }
        // crates on the ground: walk into range and break them
        if (hpFrac > 0.45) {
          for (const c of o.crates) {
            if (Math.abs(c.pos[2] - this.mapGround(c.pos[0], c.pos[1])) > 2) continue
            const d = hyp2(x, y, c.pos[0], c.pos[1])
            if (d > 150) continue
            const sc = d + 5
            if (sc < bs) {
              bs = sc
              best = d > 30 ? [c.pos[0], c.pos[1]] : [x, y]
            }
          }
        }
        if (best) {
          mode = "loot"
          moveGoal = best
        } else {
          // wander toward the zone's next center slowly while scanning
          mode = "roam"
          const ang = Math.atan2(y - zn.next_center[1], x - zn.next_center[0])
          const r = Math.min(zn.next_radius * 0.6, Math.max(20, dNext - 30))
          moveGoal = [zn.next_center[0] + Math.cos(ang) * r, zn.next_center[1] + Math.sin(ang) * r]
        }
        // shoot crates / NPCs when nothing better
        const crate = o.crates
          .filter((c) => hyp2(x, y, c.pos[0], c.pos[1]) < 120 && map?.los(x, y, muzzleZ, c.pos[0], c.pos[1], c.pos[2] + 1.2) !== false)
          .sort((a, b) => hyp2(x, y, a.pos[0], a.pos[1]) - hyp2(x, y, b.pos[0], b.pos[1]))[0]
        const npc = npcs
          .filter((n) => hyp2(x, y, n.pos[0], n.pos[1]) < 110 && map?.los(x, y, muzzleZ, n.pos[0], n.pos[1], n.pos[2] + 1.75) !== false)
          .sort((a, b) => hyp2(x, y, a.pos[0], a.pos[1]) - hyp2(x, y, b.pos[0], b.pos[1]))[0]
        const tgt = npc ? { pos: npc.pos, vel: npc.vel, h: 1.75 } : crate ? { pos: crate.pos, vel: [0, 0, 0], h: 1.2 } : null
        const unseenAttacker = t - this.mechHitT < 1.5
        if (tgt && hpFrac > 0.4 && !unseenAttacker) {
          const dd = hyp2(x, y, tgt.pos[0], tgt.pos[1])
          const tt = dd / 252
          const px = tgt.pos[0] + tgt.vel[0] * tt, py = tgt.pos[1] + tgt.vel[1] * tt
          aimYaw = Math.atan2(py - y, px - x)
          aimPitch = Math.atan2(tgt.pos[2] + tgt.h - muzzleZ, dd)
          wantAim = true
          const tol = Math.atan2(1.2, Math.max(dd, 1))
          if (Math.abs(wrap(me.aim[0] - aimYaw)) < tol && Math.abs(me.aim[1] - aimPitch) < tol * 2 + 0.02) {
            if (gunS >= 0) fire[gunS] = true
            if (lightS >= 0 && dd < 40) fire[lightS] = true
          }
          if (!npc && crate && dd < 25) moveGoal = [crate.pos[0], crate.pos[1]]
          mode += "+shoot"
        }
      }
    }

    // ---------- movement execution
    let mv: [number, number] = [0, 0]
    if (moveGoal && map) {
      const gd = hyp2(x, y, moveGoal[0], moveGoal[1])
      const direct = map.los(x, y, z + 2, moveGoal[0], moveGoal[1], map.groundAt(moveGoal[0], moveGoal[1]) + 2)
      if (gd > 3) {
        if (direct && gd < 60) mv = [(moveGoal[0] - x) / gd, (moveGoal[1] - y) / gd]
        else {
          map.field(moveGoal[0], moveGoal[1])
          const st = map.fieldFor(moveGoal[0], moveGoal[1], 25) ? map.steer(x, y) : null
          mv = st ?? [(moveGoal[0] - x) / gd, (moveGoal[1] - y) / gd]
        }
      }
    } else if (moveGoal) {
      const gd = hyp2(x, y, moveGoal[0], moveGoal[1])
      if (gd > 2) mv = [(moveGoal[0] - x) / gd, (moveGoal[1] - y) / gd]
    }
    if (slow) mv = [mv[0] * 0.4, mv[1] * 0.4]
    // dodge incoming bullets and shells
    {
      let dx = 0, dy = 0, urgent = false
      const cz = z + 4.5 * scale
      const mg = this.cfg.weapons.mortar.gravity ?? 22
      for (const p of o.projectiles) {
        const [px, py, pz] = p.pos
        const [vx, vy, vz] = p.vel
        if (p.kind === "mortar") {
          // time until it falls to our height, then landing point
          const a = -0.5 * mg, b = vz, c = pz - z
          const disc = b * b - 4 * a * c
          if (disc < 0) continue
          const tl = (-b - Math.sqrt(disc)) / (2 * a)
          if (tl < 0 || tl > 3) continue
          const lx = px + vx * tl, ly = py + vy * tl
          const dd = hyp2(x + me.vel[0] * tl, y + me.vel[1] * tl, lx, ly)
          if (dd < 11) {
            const l = Math.max(0.5, hyp2(x, y, lx, ly))
            dx += (x - lx) / l
            dy += (y - ly) / l
            urgent = true
          }
          continue
        }
        const rx = x - px, ry = y - py, rz = cz - pz
        const vv = vx * vx + vy * vy + vz * vz
        if (vv < 1) continue
        const tc = (rx * vx + ry * vy + rz * vz) / vv
        if (tc < 0 || tc > 0.9) continue
        const mx = px + vx * tc - x, my = py + vy * tc - y, mz = pz + vz * tc - cz
        const miss = Math.hypot(mx, my, mz * 0.5)
        if (miss > 3.6 * scale) continue
        // move perpendicular to the bullet path, away from its closest point
        const hv = Math.hypot(vx, vy) || 1
        let nx = -vy / hv, ny = vx / hv
        if (nx * -mx + ny * -my < 0) {
          nx = -nx
          ny = -ny
        }
        dx += nx
        dy += ny
        urgent = true
      }
      if (urgent) {
        const l = Math.hypot(dx, dy)
        if (l > 0.01) {
          mv = [mv[0] * 0.3 + (dx / l) * 0.95, mv[1] * 0.3 + (dy / l) * 0.95]
          const ml = Math.hypot(mv[0], mv[1])
          if (ml > 1) mv = [mv[0] / ml, mv[1] / ml]
          if (me.energy > (lightS >= 0 && target ? 55 : 25)) boost = true
          mode += "+dodge"
        }
      }
    }
    // stuck handling: jump + jet
    const moved = hyp2(x, y, this.lastPos[0], this.lastPos[1])
    if ((mv[0] || mv[1]) && moved < 0.25 && me.grounded) this.stuckT += 0.1
    else if (moved > 0.6) this.stuckT = 0
    if (this.stuckT > 0.6) {
      jump = true
      boost = me.energy > 10
      if (this.stuckT > 2.5) {
        // try a perpendicular direction
        mv = [-mv[1] * (this.strafeDir), mv[0] * this.strafeDir]
      }
      if (this.stuckT > 4) this.stuckT = 0
    }
    if (!me.grounded && this.stuckT > 0) boost = me.energy > 5
    this.lastPos = [x, y, z]

    // ---------- aim when idle: look along move or toward threats
    if (!wantAim) {
      if (t < 3) aimYaw = me.aim[0] + 1.6
      else if (t - this.mechHitT < 2) aimYaw = this.mechHitBearing
      else if (this.threatBearing !== null && t - this.threatT < 3) aimYaw = this.threatBearing
      else if (mv[0] || mv[1]) {
        this.scanPhase += 0.1
        aimYaw = Math.atan2(mv[1], mv[0]) + Math.sin(this.scanPhase * 1.3) * 0.9
      } else {
        this.scanPhase += 0.1
        aimYaw = me.aim[0] + 0.6
      }
      aimPitch = 0
    }
    // keep laser charging when target briefly lost
    if (laserS >= 0 && laserCharging && !fire[laserS]) {
      const Lw = me.weapons[laserS]!
      if ((Lw.charge ?? 0) < Lw.stats.charge - 0.05) fire[laserS] = true
    }
    // reload when safe
    if (gunS >= 0 && mode !== "fight") {
      const g = me.weapons[gunS]!
      if ((g.ammo ?? 12) < g.stats.mag * 0.5 && (g.reload ?? 0) === 0 && !fire[gunS]) act.reload = true
    }

    const pmin = me.stats.min_pitch ?? -0.7, pmax = me.stats.max_pitch ?? 1.3
    act.move = [Math.round(mv[0] * 1000) / 1000, Math.round(mv[1] * 1000) / 1000]
    act.aim = [Math.round(wrap(aimYaw) * 10000) / 10000, Math.round(Math.max(pmin, Math.min(pmax, aimPitch)) * 10000) / 10000]
    act.fire = fire
    act.boost = boost
    act.jump = jump
    draw.label(`${mode} hp${me.hp | 0} ${target ? "T" + target.id : ""}`)
    void visible
    this.lastHp = me.hp
    if (map) map.pump(Math.max(0, 20 - (performance.now() - t0)), 70000)
    return act
  }

  mapGround(x: number, y: number) {
    return this.map ? this.map.groundAt(x, y) : 0
  }
}

run((hello) => new Brain(hello))
