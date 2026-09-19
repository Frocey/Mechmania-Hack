"""Getting each bot from where it is to where it should be, and out of trouble when it is wedged."""

import math

from . import Vec2, corridor_clear, navigate_to, path_length, point_seg_dist
from .settings import *
from .util import clip, dist


class MovementMixin:
    def _detour(self, ax, ay, tx, ty):
        """Where to steer this tick: the target, or a waypoint that rounds a solid in the way.

        `navigate_to` knows about walls but not about the payload or the deposits, so a straight line
        through one of them would just grind against it.
        """
        length = math.hypot(tx - ax, ty - ay)
        if length < 1e-6:
            return tx, ty
        a, b = Vec2(ax, ay), Vec2(tx, ty)
        ux, uy = (tx - ax) / length, (ty - ay) / length
        for c, r in self._solids:
            clear_r = r + self.conf.bot.radius + 0.1
            if point_seg_dist(c, a, b) >= clear_r:
                continue
            if math.hypot(c.x - ax, c.y - ay) < clear_r:
                continue  # already touching it: collision will slide us round
            side = (c.x - ax) * (-uy) + (c.y - ay) * ux
            s = 1.0 if side >= 0.0 else -1.0
            off = clear_r + 0.3
            for sgn in (s, -s):
                wx, wy = c.x + sgn * uy * off, c.y - sgn * ux * off
                if self._standable(wx, wy):
                    return wx, wy
        return tx, ty

    def _desired(self, bot, tgt):
        """The move (a vector of length 0..1, as a share of full speed) that takes `bot` towards
        `tgt`, round walls and solids, slowing to stop exactly on it."""
        d = dist(bot.pos.x, bot.pos.y, tgt[0], tgt[1])
        if d < 0.02:
            return (0.0, 0.0)
        wx, wy = self._detour(bot.pos.x, bot.pos.y, tgt[0], tgt[1])
        v = navigate_to(bot.pos, Vec2(wx, wy))
        n = math.hypot(v.x, v.y)
        if n < 1e-9:
            return (0.0, 0.0)
        power = min(1.0, d / self.conf.bot.speed)
        return (v.x / n * power, v.y / n * power)

    # ---- getting unstuck --------------------------------------------------------------------

    def _stuck_check(self, bid, bot, target, des, tick):
        """Watch one bot for lack of progress; return an escape direction while it is stuck.

        A bot is stuck if, over the last STUCK_WINDOW ticks, it wanted to move on nearly every tick
        and yet ended up less than STUCK_MIN_MOVE from where it started, with somewhere still to go.
        It then steers by `_unstick_dir` for up to STUCK_ESCAPE_TICKS (or until it is
        STUCK_CLEAR_DIST clear of where it got wedged).
        """
        hist = self.hist.setdefault(bid, [])
        wants = target is not None and math.hypot(des[0], des[1]) >= 0.5
        hist.append((bot.pos.x, bot.pos.y, wants))
        if len(hist) > STUCK_WINDOW + 1:
            hist.pop(0)

        esc = self.escape.get(bid)
        if esc is not None:
            done = (
                tick >= esc[2]
                or target is None
                or dist(bot.pos.x, bot.pos.y, esc[0], esc[1]) >= STUCK_CLEAR_DIST
            )
            if done:
                del self.escape[bid]
                hist.clear()
                return None
            return self._unstick_dir(bot, target)

        if target is None or len(hist) < STUCK_WINDOW + 1:
            return None
        moved = dist(bot.pos.x, bot.pos.y, hist[0][0], hist[0][1])
        tried = sum(1 for _, _, w in hist if w)
        if (tried >= 0.8 * len(hist) and moved < STUCK_MIN_MOVE
                and dist(bot.pos.x, bot.pos.y, target[0], target[1]) > 1.5):
            self.escape[bid] = (bot.pos.x, bot.pos.y, tick + STUCK_ESCAPE_TICKS)
            print(
                f"[plan] tick {tick}: bot {bid} stuck at ({bot.pos.x:.1f}, {bot.pos.y:.1f}), "
                f"steering out"
            )
            return self._unstick_dir(bot, target)
        return None

    def _unstick_dir(self, bot, target):
        """A unit direction that gets a wedged bot moving: of the ways it can really walk, the one
        that leaves the shortest route to where it is going."""
        here = Vec2(bot.pos.x, bot.pos.y)
        goal = Vec2(target[0], target[1])
        best = None
        for step in (0.6, 0.3):
            for k in range(16):
                a = math.radians(22.5 * k)
                ux, uy = math.cos(a), math.sin(a)
                q = Vec2(bot.pos.x + ux * step, bot.pos.y + uy * step)
                if not corridor_clear(here, q):
                    continue
                length = path_length(q, goal)
                if length is None:
                    continue
                if best is None or length < best[0]:
                    best = (length, ux, uy)
            if best is not None:
                break
        if best is None:
            v = navigate_to(here, goal)   # nothing better to try: what the engine suggests
            return clip(v.x, v.y, 1.0)
        return (best[1], best[2])
