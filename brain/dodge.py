"""Dodge reflex: take less damage (public evidence: -30% damage taken ~ +1.65 points, aim is worth almost nothing).

Every decision, before the action goes out:
  bullets  each visible enemy gun/NPC bullet: if its closest pass to our chest in the next `look_s` is within
           `bullet_r`, step sideways off its path (away from where it will pass)
  lasers   each visible enemy with `charging: true` whose aim line passes within `laser_r` of us: sidestep across
           the line (the beam fires along its aim when it lets go)
  mortars  each visible shell: predict where it lands (gravity); if within `splash_r`, move away from that spot
Returns a move vector (unit-ish) to blend into the chosen move, and why."""
import math

CHEST = 5.0


def dodge(w, p):
    obs = w.obs
    x, y, z = w.x, w.y, w.z + CHEST
    dx = dy = 0.0
    why = []
    look = p.get("look_s", 0.6)
    br = p.get("bullet_r", 4.0)
    for pr in obs.get("projectiles") or []:
        px, py, pz = pr["pos"]
        vx, vy, vz = pr["vel"]
        k = pr.get("kind")
        if k == "mortar":
            g = p.get("shell_g", 22.0)
            # time until it reaches our height on the way down
            a, b, c = -0.5 * g, vz, pz - w.z
            disc = b * b - 4 * a * c
            if disc < 0:
                continue
            t = (-b - math.sqrt(disc)) / (2 * a)
            if t <= 0 or t > 3.0:
                continue
            lx, ly = px + vx * t, py + vy * t
            d = math.hypot(lx - x, ly - y)
            if d < p.get("splash_r", 10.0):
                ux, uy = (x - lx, y - ly)
                n = math.hypot(ux, uy) or 1.0
                dx += ux / n * 1.2
                dy += uy / n * 1.2
                why.append("shell")
            continue
        rx, ry, rz = px - x, py - y, pz - z
        v2 = vx * vx + vy * vy + vz * vz
        if v2 < 1.0:
            continue
        t = -(rx * vx + ry * vy + rz * vz) / v2
        if t <= 0 or t > look:
            continue
        cx, cy, cz = rx + vx * t, ry + vy * t, rz + vz * t
        if math.sqrt(cx * cx + cy * cy + cz * cz) > br:
            continue
        # sideways to the bullet's path, away from where it passes
        sx, sy = -vy, vx
        n = math.hypot(sx, sy) or 1.0
        sx, sy = sx / n, sy / n
        if sx * cx + sy * cy > 0:
            sx, sy = -sx, -sy
        dx += sx
        dy += sy
        why.append("bullet")
    lr = p.get("laser_r", 4.5)
    for e in w.enemies:
        if not e.get("charging") or "laser" not in (e.get("weapons") or []):
            continue
        ex, ey = e["pos"][0], e["pos"][1]
        yaw = (e.get("aim") or [0.0])[0]
        ax, ay = math.cos(yaw), math.sin(yaw)
        rx, ry = x - ex, y - ey
        along = rx * ax + ry * ay
        if along <= 0 or along > 290:
            continue
        perp = rx * ay - ry * ax          # signed distance from its aim line
        if abs(perp) > lr:
            continue
        s = 1.0 if perp >= 0 else -1.0    # step further out the side we're already on
        dx += ay * s * 1.3
        dy += -ax * s * 1.3
        why.append("laser")
    if not why:
        return None, ""
    n = math.hypot(dx, dy) or 1.0
    return (dx / n, dy / n), "+".join(sorted(set(why)))
