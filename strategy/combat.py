"""The army: one blob of shooters that peeks in and out together, healers well behind, and a
payload hugger.

The blob is a compact hexagonal lattice of shooters around a centre. The centre stands on a `Track`,
a walkable route from home to whatever the army is facing, and slides along it:

    calm    nothing near: the blob stands at its post, drifting a little so it is never still
    hide    an enemy shooter near: the blob sits at the furthest forward place on the track that the
            enemy cannot see (behind a wall, a corner, the payload, or just out of range), cooldowns
            ticking. The healers wait HUB_BEHIND further back; wounded shooters fall back to them.
    out     when most of the blob will be ready by the time it arrives, it slides forward together to
            the place from which most of it can hit the enemy, fires, and slides back

The payload is an object like any other: it is solid, so shots stop on it, routes go round it and
nobody stands in it. Being solid it is cover: the hide/out search counts it, so the blob hides behind
it and peeks round it, and goes round it to get at an enemy that hides behind it.

One shooter, the hugger, does not belong to the blob: it stands right against the payload on the side
the enemy cannot see.
"""

import math

from . import (
    BotClass,
    SpecialAction,
    Vec2,
    diff_degrees,
    line_of_sight,
    turn_to_angle,
)
from .settings import *
from .util import angle_deg, dist


class CombatMixin:
    # ---- what the army is facing --------------------------------------------------------

    def _objective(self, state):
        """((x, y), kind): what the army heads for. kind is "payload", "raid" (an enemy shooter near
        our deposit) or "gate" (nothing to face: hold the default gate)."""
        payload = state.payload_pos()
        if self.phase != "defend" or self.emergency:
            return (payload.x, payload.y), "payload"

        best = None
        for eid, (x, y, shooter, _w, _h, _i) in self._einfo.items():
            if not shooter:
                continue
            d = dist(x, y, self.dep_x, self.dep_y)
            if d <= TRACK_R and (best is None or d < best[0]):
                best = (d, eid, x, y)
        cur = self._einfo.get(self.target_id)
        if best is not None and cur is not None and cur[2] and best[1] != self.target_id:
            dc = dist(cur[0], cur[1], self.dep_x, self.dep_y)
            if dc <= TRACK_R and best[0] > dc - TARGET_KEEP:
                best = (dc, self.target_id, cur[0], cur[1])         # keep the current target
        if best is not None:
            self.target_id = best[1]
            return (best[2], best[3]), "raid"
        self.target_id = None
        g = self._default_gate()
        return (g.cx, g.cy), "gate"

    def _ensure_track(self, obj, kind, tick, me, fighters):
        """Keep `self.track` (from `obj` back to our deposit) up to date; the blob keeps its place on it."""
        tr = self.track
        pay = self._solids[2][0]
        stale = (
            tr is None
            or kind != self.track_kind
            or dist(obj[0], obj[1], self.track_obj[0], self.track_obj[1]) > TRACK_REBUILD_MOVE
            or tick - self.track_built >= TRACK_MAX_AGE
            or (kind != "payload" and dist(pay.x, pay.y, self.track_pay[0], self.track_pay[1]) > 1.0)
        )
        if not stale:
            return
        new = self._make_track(obj[0], obj[1], around=(kind != "payload"))
        if tr is not None:
            px, py = tr.at(self.a)
        else:
            pts = [(me[b].pos.x, me[b].pos.y) for b in fighters]
            px = sum(p[0] for p in pts) / len(pts)
            py = sum(p[1] for p in pts) / len(pts)
        self.a = new.project(px, py)
        # the arc from which the track is on our side of the line (before the march)
        a = 0.0
        while a < new.length and new.at(a)[1] < self.y_min:
            a += 0.5
        self.track_a_y = a
        self.track, self.track_kind, self.track_obj, self.track_built = new, kind, obj, tick
        self.track_pay = (pay.x, pay.y)
        self.geo = None

    def _arc_limit(self, kind):
        """The smallest arc the blob's centre may reach: no closer to what it faces than this, and
        not north of our half before the march."""
        base = {"payload": 1.5, "gate": 1.0, "raid": 3.5}.get(kind, 1.0)
        if self.phase == "defend" and not self.emergency:
            base = max(base, self.track_a_y)
        return min(base, self.track.length)

    def _calm_arc(self, kind, tick, n):
        if kind == "payload":
            if self.phase == "hold" and not self.emergency:
                # the whole blob just outside the payload's capture radius, so it does not push it on
                return self.conf.payload.capture_radius + 0.29 * math.sqrt(max(1, n)) + 0.8
            return PUSH_ARC
        if kind == "raid":
            return RAID_ARC
        return POST_ARC + SWAY_R * math.sin(2.0 * math.pi * tick / SWAY_PERIOD)

    # ---- the payload hugger -----------------------------------------------------------------

    def _hugger(self, me, shooters, payload, tick):
        """Keep one shooter against the payload; return {its id: (x, y)}.

        It stands at the point of a ring round the payload (its hull just clear of the payload's)
        that the fewest enemy shooters can see: the payload itself is what hides it. When it dies the
        shooter nearest the payload takes over.
        """
        conf = self.conf
        h = self.hugger
        if h is None or h not in me or self.role.get(h) != ROLE_FIGHTER or me[h].class_ != BotClass.Battle:
            self.hugger = None
            pool = [b for b in shooters if b not in self.recovering] or shooters
            if pool:
                self.hugger = min(
                    pool, key=lambda b: dist(me[b].pos.x, me[b].pos.y, payload.x, payload.y)
                )
                self.hug_ang = None
                print(f"[plan] tick {tick}: bot {self.hugger} goes to hug the payload")
        if self.hugger is None:
            return {}

        r = conf.payload.radius + conf.bot.radius + HUG_GAP
        if self.hug_ang is None or tick - self.hug_t >= HUG_TICKS:
            self.hug_t = tick
            threats = [
                (x, y) for eid, (x, y, sh, _w, _h, _i) in self._einfo.items()
                if sh and dist(x, y, payload.x, payload.y) <= HUG_THREAT_R
            ]
            if not threats:
                threats = [self.enemy_spawn]            # they will come from over there
            tx = sum(t[0] for t in threats) / len(threats)
            ty = sum(t[1] for t in threats) / len(threats)
            away = math.atan2(payload.y - ty, payload.x - tx)   # straight behind the payload

            def cost(ang):
                px = payload.x + r * math.cos(ang)
                py = payload.y + r * math.sin(ang)
                c = 0.0
                for ex, ey in threats:
                    if not self._hull_hidden(px, py, ex, ey):
                        c += 1.0 + (HUG_THREAT_R - min(HUG_THREAT_R, dist(px, py, ex, ey))) / HUG_THREAT_R
                return c + 0.3 * (1.0 - math.cos(ang - away)) / 2.0

            best = min((cost(math.radians(20.0 * k)), math.radians(20.0 * k)) for k in range(18))
            if self.hug_ang is None or cost(self.hug_ang) > best[0] + HUG_STAY:
                self.hug_ang = best[1]
        return {self.hugger: (payload.x + r * math.cos(self.hug_ang),
                              payload.y + r * math.sin(self.hug_ang))}

    # ---- the plan for the tick ------------------------------------------------------------

    def _plan_army(self, me, state, tick):
        """Where every fighter stands this tick: {bot id: (x, y)}."""
        conf = self.conf
        rng = conf.bot.blaster_range
        full = conf.bot.health
        payload = state.payload_pos()
        fighters = [b for b, r in self.role.items() if r == ROLE_FIGHTER and b in me]
        if not fighters:
            return {}
        all_shooters = sorted(b for b in fighters if me[b].class_ == BotClass.Battle)
        healers = sorted(b for b in fighters if me[b].class_ == BotClass.Healer)

        hug = self._hugger(me, all_shooters, payload, tick)
        shooters = [b for b in all_shooters if b != self.hugger]

        # who is wounded, and who is in the blob (in a stable order)
        for b in shooters:
            hp = me[b].health
            if b in self.recovering:
                if hp >= RECOVER_HP * full:
                    self.recovering.discard(b)
            elif hp < RETREAT_HP * full:
                self.recovering.add(b)
        self.recovering &= set(shooters)
        self.order = [b for b in self.order if b in shooters]
        known = set(self.order)
        self.order += [b for b in shooters if b not in known]
        blob = [b for b in self.order if b not in self.recovering]

        obj, kind = self._objective(state)
        self._ensure_track(obj, kind, tick, me, fighters)
        tr = self.track
        a_lim = self._arc_limit(kind)

        # the enemy shooters, nearest to the blob first
        cx, cy = tr.at(self.a)
        foes = sorted(
            (dist(cx, cy, x, y), eid)
            for eid, (x, y, shooter, _w, _h, _i) in self._einfo.items() if shooter
        )
        reach = rng + ENGAGE_MARGIN + (ENGAGE_HYST if self.engaged else 0.0)
        engaged = bool(blob) and bool(foes) and foes[0][0] <= reach
        self.engaged = engaged
        if foes:
            self._face = (self._einfo[foes[0][1]][0], self._einfo[foes[0][1]][1])
        else:
            self._face = tr.at(max(0.0, self.a - 4.0))

        offs = self._blob_offsets("blob", cx, cy, len(blob), tick)

        # where the hide and out places are (worked out now and then: it costs sight-line checks)
        geo = None
        if engaged:
            tid = foes[0][1]
            g = self.geo
            if g is not None and g["tid"] != tid:
                # keep the current target unless another has come clearly nearer
                cur = next((d for d, e in foes if e == g["tid"]), None)
                if cur is not None and cur <= foes[0][0] + 2.0:
                    tid = g["tid"]
            if g is None or g["tid"] != tid or tick - g["t"] >= self.geo_every:
                g = self._geometry(tr, foes, len(blob), offs, a_lim, tick, tid)
                self.geo = g
            geo = g
        else:
            self.geo = None
            self.cycle, self.cycle_since = "hide", tick

        if geo is None:
            a_goal = self._calm_arc(kind, tick, len(blob))
            hub_arc = self.a + HUB_BEHIND
        else:
            a_goal = geo["a_hide"]
            hub_arc = geo["a_hide"] + HUB_BEHIND
            if geo["a_out"] is None:
                self.cycle = "hide"
                if self._chase(blob, tick):
                    a_goal = self.a - 2.0                # cannot hit it from here: close in
            else:
                self._cycle(me, blob, geo, tick)
                if self.cycle == "out":
                    a_goal = geo["a_out"]

        # slide the blob: back at any time, forward only when the bots that have reached it are together
        goal = max(a_lim, min(tr.length, a_goal))
        if goal > self.a:
            self.a = min(goal, self.a + ARC_SPEED)
        elif goal < self.a and self._cohesive(me, cx, cy, blob, offs):
            self.a = max(goal, self.a - ARC_SPEED)

        cx, cy = tr.at(self.a)
        slot = {}
        for i, b in enumerate(blob):
            dx, dy = offs[i % len(offs)]
            slot[b] = (cx + dx, cy + dy)
        hub = healers + sorted(b for b in shooters if b in self.recovering)
        if hub:
            hx, hy = tr.at(max(a_lim, hub_arc))
            hoffs = self._blob_offsets("hub", hx, hy, len(hub), tick)
            for i, b in enumerate(hub):
                dx, dy = hoffs[i % len(hoffs)]
                slot[b] = (hx + dx, hy + dy)
        slot.update(hug)
        return slot

    def _blob_offsets(self, key, cx, cy, need, tick):
        """The first `need` spots of the lattice round (cx, cy) that a bot can stand on and see the
        centre from, as (dx, dy) offsets (nearest first). Worked out every OFFSET_TICKS ticks."""
        need = max(1, need)
        ent = self._off_cache.get(key)
        if (ent is not None and tick - ent[0] < OFFSET_TICKS and ent[4] >= need
                and dist(cx, cy, ent[1], ent[2]) < 0.4):
            return ent[3]
        here = Vec2(cx, cy)
        out = []
        for dx, dy in self.blob_offs:
            if len(out) >= need:
                break
            x, y = cx + dx, cy + dy
            if (dx == 0.0 and dy == 0.0) or (self._free(x, y) and line_of_sight(here, Vec2(x, y))):
                out.append((dx, dy))
        if not out:
            out = [(0.0, 0.0)]
        self._off_cache[key] = (tick, cx, cy, out, need)
        return out

    def _cohesive(self, me, cx, cy, blob, offs):
        """Have the bots that have reached the blob caught up with it? (Bots still on their way to
        join it do not count: they just come.)"""
        near = arrived = 0
        for i, b in enumerate(blob):
            dx, dy = offs[i % len(offs)]
            d = dist(me[b].pos.x, me[b].pos.y, cx + dx, cy + dy)
            if d <= JOIN_R:
                near += 1
                if d <= ARRIVE_R:
                    arrived += 1
        return near > 0 and arrived >= COHESION * near

    def _chase(self, blob, tick):
        """May the blob close in on an enemy it cannot hit from where it is? Yes if it has the numbers,
        or if nobody has fired for a long time."""
        if tick - self._last_shot >= STALL_TICKS:
            return True
        near = sum(1 for _, (x, y, sh, *_r) in self._einfo.items()
                   if sh and dist(x, y, self._face[0], self._face[1]) <= 12.0)
        return len(blob) >= CHASE_ODDS * max(1, near)

    # ---- the peek cycle ---------------------------------------------------------------------

    def _cycle(self, me, blob, geo, tick):
        """Decide hide or out for the whole blob."""
        travel = max(0.0, geo["a_hide"] - geo["a_out"]) / ARC_SPEED
        lead = travel + PEEK_LEAD
        since = tick - self.cycle_since
        if self.cycle == "hide":
            ready = sum(1 for b in blob if me[b].next_fire_tick - tick <= lead)
            if since >= MIN_HIDE and (ready >= READY_FRAC * len(blob) or since >= MAX_WAIT):
                self.cycle, self.cycle_since = "out", tick
                self.out_nft = {b: me[b].next_fire_tick for b in blob}
        else:
            fired = sum(1 for b in blob if me[b].next_fire_tick > self.out_nft.get(b, 10 ** 9))
            if since >= MAX_OUT or fired >= FIRED_FRAC * len(blob):
                self.cycle, self.cycle_since = "hide", tick

    def _geometry(self, tr, foes, n, offs, a_lim, tick, tid):
        """The two places on the track that matter against the nearest enemy shooters.

        hide: a place the whole blob is out of sight of the few nearest enemy shooters (hull and all;
              walls, corners and the payload all count).
        out:  a place, at most OUT_REACH forward of it, from which at least three quarters of the blob can
              hit the target.
        Of the pairs there are, the one whose hiding place is nearest where the blob is now. If there
        is none, the blob hides where the hidden stretch from the safe end begins, and its firing
        place is wherever the most of it can hit (a partial volley), or none.
        """
        rng = self.conf.bot.blaster_range
        n = max(1, n)
        tx, ty = self._einfo[tid][0], self._einfo[tid][1]
        gpts = [(self._einfo[e][0], self._einfo[e][1]) for _, e in foes[:GUARDS]]
        lo = max(a_lim, self.a - WIN)
        hi = max(lo, min(tr.length, self.a + WIN))
        arcs = []
        a = lo
        while a <= hi + 1e-9:
            arcs.append(a)
            a += SAMPLE
        if arcs[-1] < hi - 1e-9:
            arcs.append(hi)

        rb = 0.29 * math.sqrt(n) + 0.3           # radius of the blob
        ring = ((rb, 0.0), (-rb, 0.0), (0.0, rb), (0.0, -rb))
        hid_cache, hit_cache = {}, {}

        def hidden(arc):
            key = round(arc * 4)
            v = hid_cache.get(key)
            if v is None:
                x, y = tr.at(arc)
                v = all(self._hull_hidden(x, y, gx, gy) for gx, gy in gpts) and not any(
                    self._exposed(x + ox, y + oy, gx, gy) for ox, oy in ring for gx, gy in gpts
                )
                hid_cache[key] = v
            return v

        def hits(arc):
            key = round(arc * 4)
            v = hit_cache.get(key)
            if v is None:
                v = self._count_hits(tr, arc, offs, tx, ty, rng)
                hit_cache[key] = v
            return v

        need = max(1, math.ceil(0.75 * n))
        choice = None
        for h in arcs:
            if not hidden(h):
                continue
            arc = h - SAMPLE
            floor = max(lo, h - OUT_REACH)
            while arc >= floor - 1e-9:
                if hits(arc) >= need:
                    if choice is None or abs(h - self.a) < abs(choice[0] - self.a):
                        choice = (h, arc)
                    break
                arc -= SAMPLE
        if choice is not None:
            a_hide, a_out = choice
        else:
            a_hide = None
            for arc in reversed(arcs):
                if not hidden(arc):
                    break
                a_hide = arc
            if a_hide is None:
                a_hide = hi                         # nothing hidden: fall back as far as the window allows
            a_out, best = None, None
            arc = a_hide - SAMPLE
            while arc >= lo - 1e-9:
                c = hits(arc)
                if c > 0 and (best is None or c > best):
                    best, a_out = c, arc
                arc -= SAMPLE
        a_hide = min(tr.length, a_hide + HIDE_MARGIN)
        return {"t": tick, "tid": tid, "a_hide": a_hide, "a_out": a_out}

    def _count_hits(self, tr, arc, offs, tx, ty, rng):
        """How many of the blob's spots, with its centre at `arc`, can hit (tx, ty): in range and a
        clear shot (no wall, deposit or payload in the way)."""
        lim = rng - 0.3
        cx, cy = tr.at(arc)
        if dist(cx, cy, tx, ty) > lim + 1.5:
            return 0
        cnt = 0
        for dx, dy in offs:
            x, y = cx + dx, cy + dy
            if dist(x, y, tx, ty) <= lim and self._sees(x, y, tx, ty):
                cnt += 1
        return cnt

    # ---- shooting and healing ---------------------------------------------------------------

    def _shadowed(self, nx, ny, ex, ey, d, eid, hull):
        """Is another enemy on the ray from (nx, ny) to enemy `eid`, so that a shot would hit it instead?"""
        ux, uy = (ex - nx) / d, (ey - ny) / d
        for oid, (ox, oy, *_rest) in self._einfo.items():
            if oid == eid:
                continue
            t = (ox - nx) * ux + (oy - ny) * uy
            if 0.0 < t < d and abs((ox - nx) * uy - (oy - ny) * ux) <= hull:
                return True
        return False

    def _shoot(self, bid, bot, nx, ny, tick, claimed, ba):
        """Aim at the enemy the shot will actually hit (the first one on the ray) and fire when the
        blaster is ready, the target is vulnerable and no one else is firing at it this tick."""
        conf = self.conf
        rng = conf.bot.blaster_range
        hull = conf.bot.radius
        turn_speed = conf.bot.turn_speed
        me_pos = Vec2(nx, ny)

        cands = []
        for eid, (ex, ey, _sh, _w, hp, inv) in self._einfo.items():
            d = math.hypot(ex - nx, ey - ny)
            if d > rng + 2.0 or d < 1e-6:
                continue
            if self._shadowed(nx, ny, ex, ey, d, eid, hull):
                continue
            if not line_of_sight(me_pos, Vec2(ex, ey)):
                continue
            ang = angle_deg(nx, ny, ex, ey)
            score = hp + abs(diff_degrees(ang, bot.angle)) / 20.0
            if self.aim.get(bid) == eid:
                score -= 1.0
            vulnerable = inv <= tick
            if not vulnerable:
                score += 100.0
            blocked = self._blocked(nx, ny, ex, ey)
            if blocked:
                score += 50.0
            cands.append((score, eid, d, ang, vulnerable, blocked))

        if not cands:
            # Nobody in reach: pre-aim at where the enemy is (or the way the blob faces).
            self.aim.pop(bid, None)
            fx, fy = self._face
            if dist(nx, ny, fx, fy) > 0.3:
                ba.turn_action = turn_to_angle(angle_deg(nx, ny, fx, fy))
            ba.special_action = SpecialAction.Battle(fire=False)
            return

        pool = [c for c in cands if c[1] not in claimed] or cands
        _score, eid, d, ang, vulnerable, blocked = min(pool)
        self.aim[bid] = eid
        ba.turn_action = turn_to_angle(ang)

        # facing after this tick's turn, capped at the turn speed
        turn = max(-turn_speed, min(turn_speed, diff_degrees(ang, bot.angle)))
        err = abs(diff_degrees(ang, bot.angle + turn))
        err_max = math.degrees(math.asin(min(1.0, 0.22 / max(d, 0.3))))
        can_fire = (
            tick >= bot.next_fire_tick and vulnerable and eid not in claimed
            and d <= rng and err <= err_max * 0.85 and not blocked
        )
        if can_fire:
            claimed.add(eid)
        ba.special_action = SpecialAction.Battle(fire=can_fire)

    def _heal(self, bid, bot, nx, ny, me, npos, ba):
        conf = self.conf
        reach = conf.bot.base_heal_range - 0.15
        half_arc = conf.bot.base_heal_arc_deg / 2.0 * 0.8
        turn_speed = conf.bot.turn_speed
        me_pos = Vec2(nx, ny)

        best = None
        nearest = None
        for aid in me:
            if aid == bid:
                continue
            ax, ay = npos[aid]
            d = math.hypot(ax - nx, ay - ny)
            if nearest is None or d < nearest[0]:
                nearest = (d, ax, ay)
            if me[aid].health >= conf.bot.health - 1e-3 or d > reach:
                continue
            if not line_of_sight(me_pos, Vec2(ax, ay)):
                continue
            ang = angle_deg(nx, ny, ax, ay)
            score = me[aid].health + abs(diff_degrees(ang, bot.angle)) / 30.0
            if best is None or score < best[0]:
                best = (score, aid, ang)

        if best is None:
            # nobody hurt: face the nearest ally so the healing arc already covers it
            if nearest is not None:
                ba.turn_action = turn_to_angle(angle_deg(nx, ny, nearest[1], nearest[2]))
            ba.special_action = SpecialAction.Healer(fire=False, target=bid)
            return
        _, aid, ang = best
        ba.turn_action = turn_to_angle(ang)
        turn = max(-turn_speed, min(turn_speed, diff_degrees(ang, bot.angle)))
        in_arc = abs(diff_degrees(ang, bot.angle + turn)) <= half_arc
        ba.special_action = SpecialAction.Healer(fire=in_arc, target=aid)
