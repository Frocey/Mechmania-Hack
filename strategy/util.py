"""Small float helpers and the `Track`: the plan works in plain floats and builds a Vec2 only when it
has to call the engine."""

import bisect
import math


def dist(ax, ay, bx, by):
    return math.hypot(ax - bx, ay - by)


def clip(x, y, limit):
    n = math.hypot(x, y)
    if n > limit and n > 0.0:
        return x * limit / n, y * limit / n
    return x, y


def angle_deg(fx, fy, tx, ty):
    return math.degrees(math.atan2(ty - fy, tx - fx))


def hex_offsets(spacing, count):
    """The `count` points of a hexagonal lattice nearest (0, 0), nearest first: the shape of a compact
    blob of bots `spacing` apart."""
    r = int(math.sqrt(count) * 1.2) + 3
    h = spacing * math.sqrt(3.0) / 2.0
    pts = []
    for j in range(-r, r + 1):
        for i in range(-r, r + 1):
            pts.append((spacing * (i + 0.5 * (j % 2)), h * j))
    pts.sort(key=lambda p: (p[0] * p[0] + p[1] * p[1], p[1], p[0]))
    return pts[:count]


class Track:
    """A walkable polyline measured by arc length.

    Arc 0 is the forward end (what the army is heading for) and arcs grow towards home, so
    "further along" is a smaller arc and a retreat is a larger one. The army's shooters stand on
    it in a line, one `spacing` apart, and slide along it together.
    """

    def __init__(self, pts):
        clean = [pts[0]]
        for p in pts[1:]:
            if dist(p[0], p[1], clean[-1][0], clean[-1][1]) > 1e-6:
                clean.append(p)
        if len(clean) == 1:
            clean.append((clean[0][0] + 1e-3, clean[0][1]))
        self.pts = clean
        self.cum = [0.0]
        for i in range(1, len(clean)):
            self.cum.append(
                self.cum[-1] + dist(clean[i][0], clean[i][1], clean[i - 1][0], clean[i - 1][1])
            )
        self.length = self.cum[-1]

    def at(self, a):
        """The point at arc `a` (clamped to the ends)."""
        if a <= 0.0:
            return self.pts[0]
        if a >= self.length:
            return self.pts[-1]
        i = min(bisect.bisect_right(self.cum, a) - 1, len(self.pts) - 2)
        seg = self.cum[i + 1] - self.cum[i]
        t = (a - self.cum[i]) / seg if seg > 0.0 else 0.0
        x0, y0 = self.pts[i]
        x1, y1 = self.pts[i + 1]
        return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)

    def project(self, x, y):
        """The arc of the point on the track nearest to (x, y)."""
        best = None
        for i in range(len(self.pts) - 1):
            ax, ay = self.pts[i]
            bx, by = self.pts[i + 1]
            dx, dy = bx - ax, by - ay
            n2 = dx * dx + dy * dy
            t = 0.0 if n2 <= 0.0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / n2))
            d = dist(x, y, ax + dx * t, ay + dy * t)
            if best is None or d < best[0]:
                best = (d, self.cum[i] + t * math.sqrt(n2))
        return best[1]
