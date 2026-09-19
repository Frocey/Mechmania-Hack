"""What the plan knows about the map: gates, mining spots, tracks, and who can hit whom."""

import math

from . import (
    MAP_SIZE,
    Vec2,
    corridor_clear,
    line_of_sight,
    path_length,
    payload_pos,
    point_free,
    point_seg_dist,
    route_waypoints,
)
from .settings import *
from .util import Track, dist


class Gate:
    """A narrow place the enemy has to come through to reach our half."""

    def __init__(self, cx, cy, width=0.0):
        self.cx, self.cy = cx, cy
        self.width = width
        self.hits = 1
        self.prior = 0.0


class TerrainMixin:
    # ---- standing and walking -------------------------------------------------------

    def _standable(self, x, y):
        if x < 0.4 or y < 0.4 or x > self.map_max or y > self.map_max:
            return False
        return point_free(Vec2(x, y))

    def _walk_ticks(self, frm, to):
        """Ticks to walk from `frm` to `to`, around walls."""
        d = path_length(Vec2(frm[0], frm[1]), Vec2(to[0], to[1]))
        if d is None:
            d = 1.4 * dist(frm[0], frm[1], to[0], to[1])
        return int(d / self.conf.bot.speed) + WALK_SLACK

    def _free(self, x, y):
        """Can a bot stand centred here: clear of walls and not inside the payload or a deposit?"""
        if not self._standable(x, y):
            return False
        clear = self.conf.bot.radius + 0.02
        for c, r in self._solids:
            if dist(x, y, c.x, c.y) < r + clear:
                return False
        return True

    def _around_payload(self, pts):
        """`pts` (a route) with a waypoint added wherever a leg would run through the payload: the
        payload is solid, so the route goes round it, on the side it is already on."""
        c, r = self._solids[2]
        clear = r + self.conf.bot.radius + 0.1
        out = [pts[0]]
        for a, b in zip(pts, pts[1:]):
            length = dist(a[0], a[1], b[0], b[1])
            if (length > 1e-6
                    and point_seg_dist(c, Vec2(a[0], a[1]), Vec2(b[0], b[1])) < clear
                    and dist(c.x, c.y, a[0], a[1]) >= clear
                    and dist(c.x, c.y, b[0], b[1]) >= clear):
                ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
                side = (c.x - a[0]) * (-uy) + (c.y - a[1]) * ux
                s = 1.0 if side >= 0.0 else -1.0
                off = clear + 0.4
                for sgn in (s, -s):
                    w = (c.x + sgn * uy * off, c.y - sgn * ux * off)
                    if self._standable(w[0], w[1]):
                        out.append(w)
                        break
            out.append(b)
        return out

    def _make_track(self, ox, oy, around=True):
        """The walking route from (ox, oy) back to our deposit, as a Track (arc 0 at (ox, oy)).
        With `around`, it goes round the payload rather than through it."""
        wps = route_waypoints(Vec2(ox, oy), Vec2(self.goal[0], self.goal[1]))
        pts = [(ox, oy)]
        if wps:
            pts += [(w.x, w.y) for w in wps]
        pts.append(self.goal)
        if around:
            pts = self._around_payload(pts)

        # A tail on past the deposit, in the direction the route arrives from, so there is room to
        # fall back to when the enemy is right at the deposit.
        gx, gy = self.goal
        px, py = pts[-2]
        n = math.hypot(gx - px, gy - py)
        if n > 0.3:
            ux, uy = (gx - px) / n, (gy - py) / n
            last = (gx, gy)
            for k in range(1, int(TAIL_LEN / 0.5) + 1):
                q = (gx + ux * 0.5 * k, gy + uy * 0.5 * k)
                if not self._standable(q[0], q[1]) or not corridor_clear(
                    Vec2(last[0], last[1]), Vec2(q[0], q[1])
                ):
                    break
                last = q
            if last != (gx, gy):
                pts.append(last)
        return Track(pts)

    # ---- gates ------------------------------------------------------------------------

    def _polyline(self, a, b):
        """The route from a to b as points 0.5 apart, or None."""
        wps = route_waypoints(a, b)
        if wps is None:
            return None
        pts = [(a.x, a.y)] + [(w.x, w.y) for w in wps] + [(b.x, b.y)]
        out = [pts[0]]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            n = max(1, int(math.hypot(x1 - x0, y1 - y0) / 0.5))
            for k in range(1, n + 1):
                t = k / n
                out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
        return out

    def _width(self, route, i):
        """How much room there is across the route at sample i: the free run of standable points
        to either side, at right angles to the direction of travel."""
        px, py = route[i]
        ax, ay = route[max(0, i - 1)]
        bx, by = route[min(len(route) - 1, i + 1)]
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy)
        if n < 1e-9:
            return 99.0
        key = (round(px * 4), round(py * 4), round(math.degrees(math.atan2(dy, dx)) / 45.0))
        cached = self._width_cache.get(key)
        if cached is not None:
            return cached
        nx, ny = -dy / n, dx / n
        total = 0.0
        for sgn in (1.0, -1.0):
            reach = 0.0
            while reach < 6.0 and point_free(
                Vec2(px + sgn * nx * (reach + 0.25), py + sgn * ny * (reach + 0.25))
            ):
                reach += 0.25
            total += reach
        self._width_cache[key] = total
        return total

    def _gate_on_route(self, route):
        """The gate on a route: its narrowest spot around where it enters our half."""
        cross = None
        for i, (_, y) in enumerate(route):
            if y >= Y_LINE:
                cross = i
                break
        if cross is None or cross == 0:
            return None
        lo = max(0, cross - GATE_BEFORE)
        hi = min(len(route) - 1, cross + GATE_AFTER)
        widths = [self._width(route, i) for i in range(lo, hi + 1)]
        wmin = min(widths)
        k = next(j for j, w in enumerate(widths) if w <= wmin + GATE_TIE)
        ci = lo + k
        return Gate(route[ci][0], route[ci][1], wmin)

    def _find_gates(self):
        goal = Vec2(self.goal[0], self.goal[1])
        gates = []
        for oy in range(2, int(Y_LINE) - 2, GATE_ORIGIN_STEP):
            for ox in range(2, MAP_SIZE - 1, GATE_ORIGIN_STEP):
                if not self._standable(float(ox), float(oy)):
                    continue
                route = self._polyline(Vec2(float(ox), float(oy)), goal)
                if not route:
                    continue
                gate = self._gate_on_route(route)
                if gate is None:
                    continue
                for g in gates:
                    if dist(g.cx, g.cy, gate.cx, gate.cy) <= GATE_MERGE_DIST:
                        g.hits += 1
                        break
                else:
                    gates.append(gate)
        gates.sort(key=lambda g: -g.hits)
        gates = gates[:MAX_GATES]
        if not gates:
            print("[plan] WARNING: no gates found from the map, using GATE_SEEDS")
            gates = [Gate(x, y) for x, y in GATE_SEEDS if self._standable(x, y)]
        if not gates:
            gates = [Gate(16.0, 26.0)]
        return gates

    def _set_priors(self):
        """Before any enemy is seen, favour the gate on the enemy's shortest route to us."""
        gates = self.gates
        route = self._polyline(
            Vec2(self.enemy_spawn[0], self.enemy_spawn[1]), Vec2(self.goal[0], self.goal[1])
        )
        primary = 0
        if route:
            gaps = [min(dist(g.cx, g.cy, x, y) for x, y in route) for g in gates]
            primary = gaps.index(min(gaps))
        for i, g in enumerate(gates):
            if len(gates) == 1:
                g.prior = 1.0
            else:
                g.prior = PRIOR_PRIMARY if i == primary else (1.0 - PRIOR_PRIMARY) / (len(gates) - 1)
        self.primary = primary

    def _default_gate(self):
        """The gate the army holds while it has no enemy to face.

        The primary gate, unless the payload is on our side of centre: then the first gate its
        path to our base passes, the next one it has to come through. Switches at most every 60
        ticks (at once in an emergency).
        """
        tick = self._tick_no
        if tick - self._gate_checked >= 30:
            self._gate_checked = tick
            want = self.primary
            if self._capture <= -PAYLOAD_THREAT_C:
                t = self._capture
                while t >= -1.0:
                    p = payload_pos(t)
                    hit = next(
                        (i for i, g in enumerate(self.gates)
                         if dist(p.x, p.y, g.cx, g.cy) <= PAYLOAD_GATE_R),
                        None,
                    )
                    if hit is not None:
                        want = hit
                        break
                    t -= 0.02
            if want != self.gate_i and (tick - self.gate_since >= 60 or self.emergency):
                self.gate_i = want
                self.gate_since = tick
        return self.gates[self.gate_i]

    # ---- mining spots ---------------------------------------------------------------

    def _mining_spots(self):
        """Spots that can mine our deposit, safest first: the ones furthest from every gate."""
        conf = self.conf
        dvec = Vec2(self.dep_x, self.dep_y)
        r_min = conf.deposit.radius + conf.bot.radius + 0.35
        found = []
        y = self.dep_y - MINER_MAX_DIST
        while y <= self.dep_y + MINER_MAX_DIST:
            x = self.dep_x - MINER_MAX_DIST
            while x <= self.dep_x + MINER_MAX_DIST:
                d = dist(x, y, self.dep_x, self.dep_y)
                if (r_min <= d <= MINER_MAX_DIST and y >= self.y_min and self._standable(x, y)
                        and line_of_sight(Vec2(x, y), dvec)
                        and path_length(Vec2(self.spawn[0], self.spawn[1]), Vec2(x, y)) is not None):
                    safety = min((dist(x, y, g.cx, g.cy) for g in self.gates), default=d)
                    found.append((-safety, d, (x, y)))
                x += MINER_SPACING
            y += MINER_SPACING
        found.sort()
        return [p for _, _, p in found]

    # ---- who can hit whom ---------------------------------------------------------------

    def _blocked(self, ax, ay, bx, by):
        """Would the payload or a deposit swallow a shot from a to b?"""
        a, b = Vec2(ax, ay), Vec2(bx, by)
        for c, r in self._solids:
            if point_seg_dist(c, a, b) < r:
                return True
        return False

    def _sees(self, x, y, tx, ty):
        """Could a shot from (x, y) reach (tx, ty): no wall, deposit or payload in the way."""
        return line_of_sight(Vec2(x, y), Vec2(tx, ty)) and not self._blocked(x, y, tx, ty)

    def _exposed(self, x, y, ex, ey):
        """Could an enemy at (ex, ey) hit a bot centred at (x, y)?"""
        if dist(x, y, ex, ey) > self.conf.bot.blaster_range + 0.3:
            return False
        return self._sees(x, y, ex, ey)

    def _hull_hidden(self, x, y, ex, ey):
        """Is a whole bot at (x, y) out of the enemy's line, not just its centre? A shot hits the
        hull, so the centre and a point HULL to each side must all be hidden."""
        dx, dy = ex - x, ey - y
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return False
        nx, ny = -dy / d, dx / d
        for s in (0.0, HULL, -HULL):
            if self._exposed(x + nx * s, y + ny * s, ex, ey):
                return False
        return True
