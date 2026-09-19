import math
import traceback

from . import *

# =====================================================================================
# The plan
#
#   Phase 1 (tick < SWITCH_TICK)
#     * Opening: the free tick-0 bot + 16 rush orders (the 800 starting tokens) = 17 bots:
#       8 extractors, 8 shooters, 1 healer. All 17 walk to the ENEMY deposit; the extractors
#       mine it (8 slots) and the shooters/healer guard them and never leave.
#     * Every later free (timer) build is a shooter and joins that guard.
#     * Every rush order bought with stolen tokens is a shooter/healer at a 3:1 ratio that
#       stays at our base.
#   Phase 2 (tick >= SWITCH_TICK)
#     * Everything waiting at the base marches to the payload and holds it.
#     * Every new bot (free or rush) is a shooter that goes to the payload.
#     * The extraction team stays where it is and is not replaced.
#
#   Always: no two of our bots may sit inside one blaster splash of each other.
# =====================================================================================

SWITCH_TICK = 2000

N_MINERS = 8
N_OPENING_SHOOTERS = 8
N_OPENING_HEALERS = 1
OPENING = (
    [BotClass.Extractor] * N_MINERS
    + [BotClass.Battle] * N_OPENING_SHOOTERS
    + [BotClass.Healer] * N_OPENING_HEALERS
)

# Base reinforcements: this many shooters per healer.
BASE_SHOOTERS_PER_HEALER = 3

# Where the reinforcements wait (our half, between our spawn corner and our goal).
BASE_POINT = (6.0, 28.0)

# --- spacing -------------------------------------------------------------------------
# A shot detonates on the first enemy it meets and hurts every bot whose centre is within
# `base_blaster_splash_radius + bot.radius` of that impact point. Working through the geometry
# (impact lies on the hull, 0.25 from the target's centre), two bots stay safe from a single
# blast as long as their centres are at least ~0.8 apart. `hard` adds a margin on top.
#
# Spawning is the one place this cannot hold: the engine puts every new bot on the exact
# same spot, and one bot arrives per tick. Spacing is therefore enforced whenever an enemy is
# within `blaster_range + THREAT_MARGIN` of either bot; further away nothing can shoot us, so
# a freshly-spawned convoy is allowed to walk out of the corner instead of stretching into a
# 17-bot conga line. Set ALWAYS_SPREAD = True to enforce it unconditionally.
THREAT_MARGIN = 6.0
ALWAYS_SPREAD = False

# Mining spots are searched within this distance of the deposit centre (the engine's own
# extract range is 5 from the bot centre to the deposit's hull; stay comfortably inside it).
MINER_MAX_DIST = 4.2

ROLE_MINER = "miner"      # extractor at the enemy deposit
ROLE_ESCORT = "escort"    # shooter / healer guarding the miners
ROLE_BASE = "base"        # reinforcement waiting at home (phase 1)
ROLE_ATTACK = "attack"    # shooter / healer on the payload (phase 2)


def get_strategy(team: int) -> Strategy:
    """Both sides run the same plan: the engine mirrors the world for the top-right team."""
    return Plan()


# -------------------------------------------------------------------------------------
# small float helpers (the plan works in plain floats and builds a Vec2 only at the end)
# -------------------------------------------------------------------------------------

def _dist(ax, ay, bx, by):
    return math.hypot(ax - bx, ay - by)


def _clip(x, y, limit):
    n = math.hypot(x, y)
    if n > limit and n > 0.0:
        return x * limit / n, y * limit / n
    return x, y


def _rotate(x, y, rad):
    c, s = math.cos(rad), math.sin(rad)
    return x * c - y * s, x * s + y * c


def _angle_deg(fx, fy, tx, ty):
    return math.degrees(math.atan2(ty - fy, tx - fx))


class SlotBook:
    """Which bot owns which numbered standing spot. Assignments stick until the bot dies."""

    def __init__(self):
        self.of = {}

    def drop(self, bot_id):
        self.of.pop(bot_id, None)

    def assign(self, bot_id, order):
        """The bot's spot, picking the first free index in `order` if it has none yet."""
        if bot_id in self.of:
            return self.of[bot_id]
        if not order:
            return None
        used = set(self.of.values())
        for idx in order:
            if idx not in used:
                self.of[bot_id] = idx
                return idx
        # Every spot is taken (more bots than spots): double up rather than wander.
        self.of[bot_id] = order[0]
        return order[0]


class Plan:
    def __init__(self):
        self.ready = False
        self.role = {}      # bot id -> role
        self.cls = {}       # bot id -> BotClass it was born as (catches id reuse)
        self.pending = None  # (role, class) of the bot we ordered last tick
        self.built = 0
        self.base_built = 0
        self.aim = {}       # bot id -> enemy id it was last aiming at
        self.dep_book = SlotBook()
        self.base_book = SlotBook()
        self.ring_of = {}
        self._ring_tick = -1
        self._ring_cache = {}
        self.phase2_announced = False

    # ---------------------------------------------------------------------------------
    # entry point
    # ---------------------------------------------------------------------------------

    def __call__(self, state: GameState) -> FleetAction:
        try:
            return self._tick(state)
        except Exception:
            # A crash would take the whole bot down; report it in the log and sit tight.
            traceback.print_exc()
            return FleetAction.new()

    # ---------------------------------------------------------------------------------
    # one-time setup
    # ---------------------------------------------------------------------------------

    def _setup(self, state, conf):
        self.conf = conf
        b = conf.bot
        self.hard = 2.0 * b.radius + b.base_blaster_splash_radius + 0.1   # ~0.9
        self.rep_start = self.hard + 0.1                                  # ~1.0
        self.spacing = self.hard + 0.15                                   # ~1.05: rest spacing
        self.threat_range = b.blaster_range + THREAT_MARGIN
        self.map_max = float(MAP_SIZE) - 0.4

        self.spawn = (b.radius + 0.002, float(MAP_SIZE) - b.radius - 0.002)

        d = state.deposit_other.pos
        self.dep_x, self.dep_y = d.x, d.y
        miner_spots, escort_spots = self._deposit_spots()
        self.dep_spots = miner_spots + escort_spots
        self.miner_order = list(range(len(miner_spots)))
        self.escort_order = list(range(len(miner_spots), len(self.dep_spots)))
        # If a class runs out of its own spots it may fall back on the other's.
        self.miner_order += self.escort_order
        self.escort_order += list(range(len(miner_spots)))

        self.base_spots = self._base_spots()
        self.base_order = list(range(len(self.base_spots)))

        self.ring_offsets = self._ring_offsets()

        print(
            f"[plan] enemy deposit at ({self.dep_x:.1f}, {self.dep_y:.1f}); "
            f"{len(miner_spots)} mining spots, {len(escort_spots)} guard spots, "
            f"{len(self.base_spots)} base spots"
        )
        if len(miner_spots) < N_MINERS:
            print(f"[plan] WARNING: only {len(miner_spots)} spots with a sightline to the deposit")
        self.ready = True

    def _standable(self, x, y):
        if x < 0.4 or y < 0.4 or x > self.map_max or y > self.map_max:
            return False
        return point_free(Vec2(x, y))

    def _reachable(self, x, y):
        return path_length(Vec2(self.spawn[0], self.spawn[1]), Vec2(x, y)) is not None

    def _lattice(self, cx, cy, half):
        n = int(half / self.spacing)
        for i in range(-n, n + 1):
            for j in range(-n, n + 1):
                yield cx + i * self.spacing, cy + j * self.spacing

    def _deposit_spots(self):
        conf = self.conf
        r_min = conf.deposit.radius + conf.bot.radius + 0.35
        dvec = Vec2(self.dep_x, self.dep_y)
        found = []
        for x, y in self._lattice(self.dep_x, self.dep_y, 9.0):
            d = _dist(x, y, self.dep_x, self.dep_y)
            if d < r_min or not self._standable(x, y) or not self._reachable(x, y):
                continue
            found.append((d, x, y))
        found.sort()
        miners, escorts = [], []
        for d, x, y in found:
            if (len(miners) < N_MINERS and d <= MINER_MAX_DIST
                    and line_of_sight(Vec2(x, y), dvec)):
                miners.append((x, y))
            else:
                escorts.append((x, y))
        return miners, escorts

    def _base_spots(self):
        bx, by = BASE_POINT
        found = []
        for x, y in self._lattice(bx, by, 8.0):
            if self._standable(x, y) and self._reachable(x, y):
                found.append((_dist(x, y, bx, by), x, y))
        found.sort()
        return [(x, y) for _, x, y in found]

    def _ring_offsets(self):
        """Standing offsets around the payload, in rings, at least `spacing` apart.

        The first two rings sit inside the payload's capture radius (so they count towards
        pushing); the outer two are a reserve that can still shoot.
        """
        offsets = []
        for k, r in enumerate((1.2, 2.3, 3.4, 4.5)):
            n = int(math.pi / math.asin(min(1.0, self.spacing / (2.0 * r))))
            twist = (k % 2) * math.pi / n
            for m in range(n):
                a = twist + 2.0 * math.pi * m / n
                offsets.append((r * math.cos(a), r * math.sin(a), k))
        return offsets

    # ---------------------------------------------------------------------------------
    # the tick
    # ---------------------------------------------------------------------------------

    def _tick(self, state):
        conf = get_config()
        if not self.ready:
            self._setup(state, conf)

        tick = state.tick
        me = {b.id: b for b in state.fleet_me}
        enemies = [e for e in state.fleet_other]

        self._sync_roles(me, tick)
        if tick >= SWITCH_TICK and not self.phase2_announced:
            self.phase2_announced = True
            print(f"[plan] tick {tick}: phase 2 -- base bots go to the payload")

        action = FleetAction.new()
        self._decide_build(state, conf, action)

        # 1. where does each bot want to go
        targets = {}
        for bid, bot in me.items():
            targets[bid] = self._target(bid, bot, state)

        des = {}
        for bid, bot in me.items():
            tgt = targets[bid]
            dx = dy = 0.0
            if tgt is not None:
                if _dist(bot.pos.x, bot.pos.y, tgt[0], tgt[1]) > 1e-3:
                    v = navigate_to(bot.pos, Vec2(tgt[0], tgt[1]))
                    dx, dy = _clip(v.x, v.y, 1.0)
            des[bid] = (dx, dy)

        # 2. keep out of each other's splash
        final = self._spread(me, enemies, des, conf)

        speed = conf.bot.speed
        npos = {
            bid: (bot.pos.x + final[bid][0] * speed, bot.pos.y + final[bid][1] * speed)
            for bid, bot in me.items()
        }

        # 3. turn / shoot / heal / mine, using where each bot will actually be this tick
        claimed = set()
        payload = state.payload_pos()
        for bid, bot in me.items():
            ba = action.bots[bid]
            ba.move_action = move_bot(Vec2(final[bid][0], final[bid][1]))
            nx, ny = npos[bid]

            if bot.class_ == BotClass.Extractor:
                ba.turn_action = turn_towards(state.deposit_other.pos)
                ba.special_action = SpecialAction.Extractor(mine=True)
            elif bot.class_ == BotClass.Battle:
                self._shoot(bid, bot, nx, ny, enemies, state, conf, payload, claimed, ba)
            else:
                self._heal(bid, bot, nx, ny, me, npos, conf, ba)

        return action

    # ---------------------------------------------------------------------------------
    # bookkeeping: who is who
    # ---------------------------------------------------------------------------------

    def _forget(self, bid):
        self.role.pop(bid, None)
        self.cls.pop(bid, None)
        self.aim.pop(bid, None)
        self.dep_book.drop(bid)
        self.base_book.drop(bid)
        self.ring_of.pop(bid, None)

    def _sync_roles(self, me, tick):
        for bid in list(self.role):
            if bid not in me or me[bid].class_ != self.cls[bid]:
                self._forget(bid)

        for bid, bot in me.items():
            if bid in self.role:
                continue
            role = None
            if self.pending is not None and self.pending[1] == bot.class_:
                role = self.pending[0]
            if role is None:
                if bot.class_ == BotClass.Extractor:
                    role = ROLE_MINER
                else:
                    role = ROLE_BASE if tick < SWITCH_TICK else ROLE_ATTACK
            self.role[bid] = role
            self.cls[bid] = bot.class_
        self.pending = None

        if tick >= SWITCH_TICK:
            for bid, role in self.role.items():
                if role == ROLE_BASE:
                    self.role[bid] = ROLE_ATTACK
                    self.base_book.drop(bid)

    # ---------------------------------------------------------------------------------
    # the fabricator
    # ---------------------------------------------------------------------------------

    def _decide_build(self, state, conf, action):
        tick = state.tick
        fab = state.fabricator_me
        action.fabricator_next = int(BotClass.Battle)
        action.rush_order = False

        # Nothing is built in the endgame, and a full fleet ignores builds.
        if tick >= conf.max_ticks - conf.endgame_ticks or state.fleet_me.is_full():
            return

        natural_due = fab.next_bot_creation <= tick
        can_rush = fab.tokens >= conf.fabricator.rush_cost
        if not natural_due and not can_rush:
            return

        # A tick where the free build is due never also rushes, so the class we set is
        # unambiguously the free bot's; otherwise the free build would just slide a tick.
        if self.built < len(OPENING):
            cls = OPENING[self.built]
            role = ROLE_MINER if self.built < N_MINERS else ROLE_ESCORT
            self.built += 1
        elif natural_due:
            cls = BotClass.Battle
            role = ROLE_ESCORT if tick < SWITCH_TICK else ROLE_ATTACK
        elif tick < SWITCH_TICK:
            is_healer = self.base_built % (BASE_SHOOTERS_PER_HEALER + 1) == BASE_SHOOTERS_PER_HEALER
            cls = BotClass.Healer if is_healer else BotClass.Battle
            role = ROLE_BASE
            self.base_built += 1
        else:
            cls = BotClass.Battle
            role = ROLE_ATTACK

        action.fabricator_next = int(cls)
        action.rush_order = not natural_due
        self.pending = (role, cls)

    # ---------------------------------------------------------------------------------
    # where each role stands
    # ---------------------------------------------------------------------------------

    def _target(self, bid, bot, state):
        role = self.role[bid]
        if role == ROLE_MINER or role == ROLE_ESCORT:
            order = self.miner_order if role == ROLE_MINER else self.escort_order
            idx = self.dep_book.assign(bid, order)
            return self.dep_spots[idx] if idx is not None else None
        if role == ROLE_BASE:
            idx = self.base_book.assign(bid, self.base_order)
            return self.base_spots[idx] if idx is not None else BASE_POINT
        return self._payload_target(bid, bot, state)

    def _valid_ring(self, state):
        if self._ring_tick == state.tick:
            return self._ring_cache
        p = state.payload_pos()
        pvec = Vec2(p.x, p.y)
        cache = {}
        for idx, (ox, oy, k) in enumerate(self.ring_offsets):
            x, y = p.x + ox, p.y + oy
            if not self._standable(x, y):
                continue
            if not line_of_sight(pvec, Vec2(x, y)):
                continue
            cache[idx] = (x, y, k)
        self._ring_tick = state.tick
        self._ring_cache = cache
        return cache

    def _payload_target(self, bid, bot, state):
        valid = self._valid_ring(state)
        cur = self.ring_of.get(bid)
        if cur in valid:
            x, y, _ = valid[cur]
            return (x, y)

        used = {v for b, v in self.ring_of.items() if b != bid and v in valid}
        best = None
        for idx, (x, y, k) in valid.items():
            if idx in used:
                continue
            key = (0 if k <= 1 else 1, _dist(bot.pos.x, bot.pos.y, x, y))
            if best is None or key < best[0]:
                best = (key, idx)
        if best is None:
            p = state.payload_pos()
            return (p.x, p.y)
        self.ring_of[bid] = best[1]
        x, y, _ = valid[best[1]]
        return (x, y)

    # ---------------------------------------------------------------------------------
    # spacing
    # ---------------------------------------------------------------------------------

    def _spread(self, me, enemies, des, conf):
        speed = conf.bot.speed
        ids = list(me)

        threatened = {}
        for bid in ids:
            p = me[bid].pos
            threatened[bid] = any(
                _dist(p.x, p.y, e.pos.x, e.pos.y) <= self.threat_range for e in enemies
            )

        q = {
            bid: (me[bid].pos.x + des[bid][0] * speed, me[bid].pos.y + des[bid][1] * speed)
            for bid in ids
        }

        final = {}
        for i in ids:
            rx = ry = 0.0
            for j in ids:
                if i == j:
                    continue
                if not (ALWAYS_SPREAD or threatened[i] or threatened[j]):
                    continue
                dx, dy = q[i][0] - q[j][0], q[i][1] - q[j][1]
                d = math.hypot(dx, dy)
                if d >= self.rep_start:
                    continue
                sign = 1.0 if i > j else -1.0
                if d < 1e-4:
                    # Exactly on top of each other: pick a direction both bots agree on.
                    a = math.radians(((min(i, j) * 53 + max(i, j) * 29) * 137.508) % 360.0)
                    ux, uy = sign * math.cos(a), sign * math.sin(a)
                else:
                    ux, uy = dx / d, dy / d
                # Sidestep a little as well as backing off, so a column of bots fans out
                # instead of the rear ones simply stalling behind the front.
                ux, uy = _rotate(ux, uy, sign * math.radians(40.0))
                s = min(1.0, (self.rep_start - d) / (self.rep_start - self.hard)) * 1.5
                rx += ux * s
                ry += uy * s
            final[i] = _clip(des[i][0] + rx, des[i][1] + ry, 1.0)
        return final

    # ---------------------------------------------------------------------------------
    # battle bots
    # ---------------------------------------------------------------------------------

    def _blocked(self, state, conf, ax, ay, bx, by):
        """Would the payload or a deposit swallow a shot from a to b?"""
        a, b = Vec2(ax, ay), Vec2(bx, by)
        solids = (
            (state.deposit_me.pos, conf.deposit.radius),
            (state.deposit_other.pos, conf.deposit.radius),
            (state.payload_pos(), conf.payload.radius),
        )
        for c, r in solids:
            if point_seg_dist(c, a, b) < r:
                return True
        return False

    def _shoot(self, bid, bot, nx, ny, enemies, state, conf, payload, claimed, ba):
        rng = conf.bot.blaster_range
        turn_speed = conf.bot.turn_speed
        me_pos = Vec2(nx, ny)

        cands = []
        for e in enemies:
            # The engine moves everyone before anyone shoots, so aim where they will be.
            ex, ey = e.pos.x + e.vel.x, e.pos.y + e.vel.y
            d = math.hypot(ex - nx, ey - ny)
            if d > rng + 2.0:
                continue
            if not line_of_sight(me_pos, Vec2(ex, ey)):
                continue
            ang = _angle_deg(nx, ny, ex, ey)
            turn_needed = abs(diff_degrees(ang, bot.angle))
            vulnerable = e.invulnerable_until_tick <= state.tick
            score = e.health + turn_needed / 20.0
            if self.aim.get(bid) == e.id:
                score -= 1.0
            if not vulnerable:
                score += 100.0
            cands.append((score, e.id, d, ang, vulnerable))

        if not cands:
            # Nobody to shoot: pre-aim at the nearest enemy, else at the payload.
            self.aim.pop(bid, None)
            ang = None
            if enemies:
                near = min(enemies, key=lambda e: _dist(nx, ny, e.pos.x, e.pos.y))
                ang = _angle_deg(nx, ny, near.pos.x, near.pos.y)
            elif _dist(nx, ny, payload.x, payload.y) > 0.5:
                ang = _angle_deg(nx, ny, payload.x, payload.y)
            if ang is not None:
                ba.turn_action = turn_to_angle(ang)
            ba.special_action = SpecialAction.Battle(fire=False)
            return

        # Don't stack shots on one enemy: a bot hit this tick is immune to the rest.
        pool = [c for c in cands if c[1] not in claimed] or cands
        score, eid, d, ang, vulnerable = min(pool)
        self.aim[bid] = eid
        ba.turn_action = turn_to_angle(ang)

        # What the bot will face after this tick's turn, capped at the turn speed.
        turn = max(-turn_speed, min(turn_speed, diff_degrees(ang, bot.angle)))
        err = abs(diff_degrees(ang, bot.angle + turn))
        err_max = math.degrees(math.asin(min(1.0, 0.22 / max(d, 0.3))))

        # Firing is not free: a shot that misses still burns the cooldown.
        ready = state.tick >= bot.next_fire_tick
        can_fire = (
            ready
            and vulnerable
            and eid not in claimed
            and d <= rng
            and err <= err_max * 0.85
            and not self._blocked(state, conf, nx, ny, nx + math.cos(math.radians(ang)) * d,
                                  ny + math.sin(math.radians(ang)) * d)
        )
        if can_fire:
            claimed.add(eid)
        ba.special_action = SpecialAction.Battle(fire=can_fire)

    # ---------------------------------------------------------------------------------
    # healers
    # ---------------------------------------------------------------------------------

    def _heal(self, bid, bot, nx, ny, me, npos, conf, ba):
        reach = conf.bot.base_heal_range - 0.15
        half_arc = conf.bot.base_heal_arc_deg / 2.0 * 0.8
        turn_speed = conf.bot.turn_speed
        me_pos = Vec2(nx, ny)

        best = None
        nearest = None
        for aid, ally in me.items():
            if aid == bid:
                continue
            ax, ay = npos[aid]
            d = math.hypot(ax - nx, ay - ny)
            if nearest is None or d < nearest[0]:
                nearest = (d, ax, ay)
            if ally.health >= conf.bot.health - 1e-3 or d > reach:
                continue
            if not line_of_sight(me_pos, Vec2(ax, ay)):
                continue
            ang = _angle_deg(nx, ny, ax, ay)
            score = ally.health + abs(diff_degrees(ang, bot.angle)) / 30.0
            if best is None or score < best[0]:
                best = (score, aid, ang)

        if best is None:
            # Nobody hurt: face the nearest ally so the healing arc already covers it.
            if nearest is not None:
                ba.turn_action = turn_to_angle(_angle_deg(nx, ny, nearest[1], nearest[2]))
            ba.special_action = SpecialAction.Healer(fire=False, target=bid)
            return

        _, aid, ang = best
        ba.turn_action = turn_to_angle(ang)
        turn = max(-turn_speed, min(turn_speed, diff_degrees(ang, bot.angle)))
        in_arc = abs(diff_degrees(ang, bot.angle + turn)) <= half_arc
        ba.special_action = SpecialAction.Healer(fire=in_arc, target=aid)
