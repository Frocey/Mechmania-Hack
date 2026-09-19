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
#     * Every later free (timer) build is a shooter. It joins the guard only while the guard
#       has fewer than ESCORT_SHOOTER_CAP shooters (so dead guards get replaced, and the
#       guard never grows past the cap); otherwise it waits at our base.
#     * Every rush order bought with stolen tokens is a shooter/healer at a 3:1 ratio that
#       stays at our base.
#   Phase 2 (tick >= SWITCH_TICK)
#     * Everything waiting at the base marches to the payload and holds it.
#     * Every new bot (free or rush) is a shooter that goes to the payload.
#     * The extraction team stays where it is and is not replaced.
#
#     * Before the endgame (nothing is built after it), a full fleet swaps its extractors for
#       fighters early enough for them to walk to the payload: see SWAP_EXTRACTORS below.
#
#   Always: no two of our bots may sit inside one blaster splash of each other.
# =====================================================================================

SWITCH_TICK = 2000

N_MINERS = 8
N_OPENING_SHOOTERS = 8
N_OPENING_HEALERS = 1
# Built (and so walking out) front to back: shooters, then the healer, then the extractors.
OPENING = (
    [BotClass.Battle] * N_OPENING_SHOOTERS
    + [BotClass.Healer] * N_OPENING_HEALERS
    + [BotClass.Extractor] * N_MINERS
)

# The extraction guard never holds more than this many shooters; fewer than this and the
# next free build is sent to refill it (phase 1 only).
ESCORT_SHOOTER_CAP = N_OPENING_SHOOTERS

# If True, rush orders also refill a short-handed guard instead of always going to the base.
REPLACE_WITH_RUSH = False

# --- pre-endgame extractor swap --------------------------------------------------------
# Nothing is built once the endgame starts, so a full fleet is stuck with whatever it has. If
# the fleet is full, the extractors are turned into fighters BEFORE the endgame: they
# self-destruct together (they still mine on that tick), which opens their slots, and the
# banked tokens rush shooters/healers into them, one per tick.
#
# New bots spawn in our corner and have to walk to the payload, so the swap starts early
# enough that the last replacement has arrived by the time the endgame begins:
#     start tick = endgame_start - walk_ticks(spawn -> payload) - swaps - SWAP_MARGIN
# How many extractors go is limited by the tokens banked (50 per replacement, plus the free
# build if one is due). A swap that is short of tokens is topped up on later ticks as more
# tokens arrive, until there are no ticks left to build in.
SWAP_EXTRACTORS = True
SWAP_MARGIN = 6            # ticks of slack for a skipped tick (compute budget)
SWAP_ARRIVAL_SLACK = 40    # extra walking time allowed for turning, detours and spacing

# Lost extractors are replaced (up to N_MINERS) so the income keeps flowing, ahead of every other
# build, until a replacement could no longer arrive and mine for EXTRACTOR_MIN_MINE_TICKS before
# the swap below (or the endgame). It also waits while an enemy shooter is at the deposit.
REPLACE_EXTRACTORS = True
EXTRACTOR_MIN_MINE_TICKS = 300

# The attack pool (base bots, then payload bots) is kept at this many shooters per healer, for
# every build that joins it: free builds, rush orders and the post-swap refills alike.
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

# How many of the spots nearest the deposit make up each pool of the guard's formation.
GUARD_POOL = 24

# --- extraction guard: calm formation and alert response ---------------------------------
# Calm (no enemy near the deposit): this share of the guard shooters stand in front of the
# healer and extractors, the rest behind them.
GUARD_FRONT_SHARE = 0.5
# Alert: an enemy within blaster_range + GUARD_ALERT_MARGIN of the deposit. Every guard shooter
# is then re-planned onto the spots nearest that enemy, each spot taken by whichever shooter is
# closest to it, so the ones that were at the back run round to help. The plan is redone if the
# nearest enemy has moved GUARD_REPLAN_DIST, or every GUARD_REPLAN_TICKS. After GUARD_CALM_TICKS
# without an enemy the shooters go back to their calm posts.
# Collapse: if no enemy comes near the deposit for COLLAPSE_QUIET_TICKS in a row (and the game
# is past COLLAPSE_AFTER_TICK), the guard's shooters and healer walk to the payload and join the
# attack; the extractors stay and keep mining. If an enemy turns up at the deposit while they
# are still within RECALL_RANGE of it, they go back to guarding. Keep COLLAPSE_AFTER_TICK at or
# after SWITCH_TICK: before it, free builds refill the guard, which would fight the collapse.
COLLAPSE_ENABLED = True
COLLAPSE_AFTER_TICK = SWITCH_TICK
COLLAPSE_QUIET_TICKS = 300
RECALL_RANGE = 14.0

GUARD_ALERT_MARGIN = 4.0

# --- stuck detection -----------------------------------------------------------------------
# Expected travel is 0.05 a tick, i.e. 2.0 over STUCK_WINDOW ticks; under STUCK_MIN_MOVE means
# wedged. A stuck bot ignores spacing and steers out for at most STUCK_ESCAPE_TICKS.
STUCK_WINDOW = 40
STUCK_MIN_MOVE = 0.5
STUCK_ESCAPE_TICKS = 60
STUCK_CLEAR_DIST = 2.0
GUARD_REPLAN_DIST = 4.0
GUARD_REPLAN_TICKS = 90
GUARD_CALM_TICKS = 90

# --- shooters in front ---------------------------------------------------------------
# Healers and extractors never stand closer to an enemy than our nearest shooter does. When an
# enemy is within blaster_range + COVER_TRIGGER of one and a shooter is within COVER_RANGE, the
# support bot steps to COVER_BEHIND behind that shooter (on the side away from the enemy) if its
# own spot would be less than COVER_MARGIN further back than the shooter is.
COVER_TRIGGER = 3.0
COVER_RANGE = 10.0
COVER_MARGIN = 0.5
COVER_BEHIND = 1.3

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
        self.aim = {}       # bot id -> enemy id it was last aiming at
        self.dep_book = SlotBook()
        self.base_book = SlotBook()
        self.ring_of = {}
        self._ring_tick = -1
        self._ring_cache = {}
        self.phase2_announced = False
        self.swap_done = False
        self.post = {}          # guard shooter id -> "front" | "rear" (its calm-state post)
        self.hist = {}          # bot id -> recent (x, y, wanted_to_move) for the stuck check
        self.escape = {}        # bot id -> (x, y, until_tick) while it steers out of a jam
        self.quiet = 0          # consecutive ticks with no enemy near the deposit
        self.collapsed = set()  # guard bots currently sent to the payload (may be recalled)
        self.alert = False
        self.alert_at = (0.0, 0.0)
        self.alert_tick = 0
        self.calm = 0

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
        miner_spots, front_spots, flank_spots, rear_spots = self._deposit_spots()
        self.dep_spots = miner_spots + front_spots + flank_spots + rear_spots
        n_m, n_f, n_s = len(miner_spots), len(front_spots), len(flank_spots)
        miner_idx = list(range(n_m))
        front_idx = list(range(n_m, n_m + n_f))                  # frontmost first
        flank_idx = list(range(n_m + n_f, n_m + n_f + n_s))      # beside the extractors
        rear_idx = list(range(n_m + n_f + n_s, len(self.dep_spots)))  # behind them
        # The extractors sit deepest; the healer just in front of them; the shooters are split
        # between the front (ahead of the healer and extractors) and the rear (behind them).
        # Each class falls back on the other pools only if its own runs out.
        self.miner_order = miner_idx + front_idx[::-1] + flank_idx + rear_idx
        self.healer_order = front_idx[::-1] + flank_idx + rear_idx + miner_idx
        self.front_order = front_idx + flank_idx + rear_idx + miner_idx
        self.rear_order = rear_idx + flank_idx + front_idx[::-1] + miner_idx
        self.guard_idx = front_idx + flank_idx + rear_idx   # every spot a guard shooter may hold
        self.escort_only = list(self.guard_idx)             # for flanking round a deposit

        self.enemy_spawn = (float(MAP_SIZE) - self.spawn[0], float(MAP_SIZE) - self.spawn[1])

        # How long a new extractor takes to walk from our spawn to a mining spot.
        mine_at = self.dep_spots[0] if miner_spots else (self.dep_x, self.dep_y)
        walk = path_length(Vec2(self.spawn[0], self.spawn[1]), Vec2(mine_at[0], mine_at[1]))
        if walk is None:
            walk = 1.4 * _dist(self.spawn[0], self.spawn[1], mine_at[0], mine_at[1])
        self.dep_walk_ticks = int(walk / b.speed) + SWAP_ARRIVAL_SLACK

        self.base_spots = self._base_spots()
        self.base_order = list(range(len(self.base_spots)))
        # Healers wait at the back of the base group: of the spots nearest the rally point,
        # the ones furthest from the enemy's side of the map.
        head = self.base_order[:16]
        self.base_healer_order = (
            sorted(head, key=lambda i: -_dist(self.base_spots[i][0], self.base_spots[i][1],
                                              self.enemy_spawn[0], self.enemy_spawn[1]))
            + self.base_order[16:]
        )

        self.ring_offsets = self._ring_offsets()

        print(
            f"[plan] enemy deposit at ({self.dep_x:.1f}, {self.dep_y:.1f}); "
            f"{len(miner_spots)} mining spots, guard spots: {len(front_spots)} front / "
            f"{len(flank_spots)} side / {len(rear_spots)} rear, {len(self.base_spots)} base spots"
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
        # "Front" is towards the enemy. Depth is the walking distance from the enemy's spawn:
        # the smaller it is, the more exposed the spot.
        enemy_spawn = Vec2(float(MAP_SIZE) - self.spawn[0], float(MAP_SIZE) - self.spawn[1])
        found = []
        for x, y in self._lattice(self.dep_x, self.dep_y, 9.0):
            d = _dist(x, y, self.dep_x, self.dep_y)
            if d < r_min or not self._standable(x, y) or not self._reachable(x, y):
                continue
            depth = path_length(enemy_spawn, Vec2(x, y))
            if depth is None:
                depth = _dist(x, y, enemy_spawn.x, enemy_spawn.y)
            found.append((d, x, y, depth))
        found.sort()   # nearest the deposit first

        # Extractors: of the spots that can mine, the ones deepest behind the guard.
        eligible = [
            s for s in found
            if s[0] <= MINER_MAX_DIST and line_of_sight(Vec2(s[1], s[2]), dvec)
        ]
        eligible.sort(key=lambda s: (-s[3], s[0]))
        miners = eligible[:N_MINERS]
        taken = {(s[1], s[2]) for s in miners}
        shallowest = min((s[3] for s in miners), default=0.0)

        # Guard spots, in three pools relative to the extractors' depth: in front of them,
        # beside them, or behind them (each the nearest GUARD_POOL to the deposit). Frontmost
        # first, so the front shooters fill the very front and the healer (which takes the
        # list backwards) sits just ahead of the extractors.
        deepest = max((s[3] for s in miners), default=0.0)
        rest = [s for s in found if (s[1], s[2]) not in taken]   # nearest the deposit first
        front = [s for s in rest if s[3] <= shallowest][:GUARD_POOL]
        flank = [s for s in rest if shallowest < s[3] <= deepest][:GUARD_POOL]
        rear = [s for s in rest if s[3] > deepest][:GUARD_POOL]
        front.sort(key=lambda s: (s[3], s[0]))   # frontmost first

        xy = lambda spots: [(s[1], s[2]) for s in spots]
        return xy(miners), xy(front), xy(flank), xy(rear)

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

        # Per-tick facts shared by everything below. The payload and both deposits are solid:
        # bots cannot walk through them and a shot stops dead on them.
        payload = state.payload_pos()
        self._solids = [
            (state.deposit_me.pos, conf.deposit.radius),
            (state.deposit_other.pos, conf.deposit.radius),
            (payload, conf.payload.radius),
        ]
        self._enemy_pts = [(e.id, e.pos.x, e.pos.y) for e in enemies]
        # Only enemy shooters are a danger to stand near: extractors and healers cannot hurt us,
        # and an enemy extractor mining its own deposit is not an attack on ours.
        self._threat_pts = [
            (e.id, e.pos.x, e.pos.y) for e in enemies if e.class_ == BotClass.Battle
        ]
        self._clear_cache = {}

        self._sync_roles(me, tick)
        if tick >= SWITCH_TICK and not self.phase2_announced:
            self.phase2_announced = True
            print(f"[plan] tick {tick}: phase 2 -- base bots go to the payload")

        action = FleetAction.new()
        self._decide_build(state, conf, action)

        # 1. where does each bot want to go
        self._update_guard(me, tick)
        targets = {}
        for bid, bot in me.items():
            targets[bid] = self._target(bid, bot, state)
        # ...except that healers and extractors never lead: near an enemy they tuck in
        # behind our shooters.
        for bid, bot in me.items():
            if bot.class_ != BotClass.Battle:
                targets[bid] = self._cover_target(bot, targets[bid], me)

        des = {}
        for bid, bot in me.items():
            tgt = targets[bid]
            dx = dy = 0.0
            if tgt is not None:
                if _dist(bot.pos.x, bot.pos.y, tgt[0], tgt[1]) > 1e-3:
                    # navigate_to routes around walls only, so go round the payload/deposits
                    # ourselves instead of grinding into them.
                    wx, wy = self._detour(bot.pos.x, bot.pos.y, tgt[0], tgt[1])
                    v = navigate_to(bot.pos, Vec2(wx, wy))
                    dx, dy = _clip(v.x, v.y, 1.0)
            des[bid] = (dx, dy)

        # 2. keep out of each other's splash...
        final = self._spread(me, des, conf)
        # ...unless a bot has stopped making progress, in which case getting it moving again
        # comes first: it steps off along whichever way shortens its route.
        for bid, bot in me.items():
            unstick = self._stuck_check(bid, bot, targets[bid], des[bid], tick)
            if unstick is not None:
                final[bid] = unstick

        speed = conf.bot.speed
        npos = {
            bid: (bot.pos.x + final[bid][0] * speed, bot.pos.y + final[bid][1] * speed)
            for bid, bot in me.items()
        }

        # 3. turn / shoot / heal / mine, using where each bot will actually be this tick
        claimed = set()
        swap = self._plan_swap(state, conf, me)
        for bid, bot in me.items():
            ba = action.bots[bid]
            ba.move_action = move_bot(Vec2(final[bid][0], final[bid][1]))
            nx, ny = npos[bid]

            if bot.class_ == BotClass.Extractor:
                ba.turn_action = turn_towards(state.deposit_other.pos)
                ba.special_action = SpecialAction.Extractor(mine=True)
                if bid in swap:
                    ba.self_destruct = True
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
        self.post.pop(bid, None)
        self.hist.pop(bid, None)
        self.escape.pop(bid, None)
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

    def _plan_swap(self, state, conf, me):
        """Ids of extractors to self-destruct this tick so their slots can be refilled."""
        if not SWAP_EXTRACTORS:
            return set()
        tick = state.tick
        endgame_start = conf.max_ticks - conf.endgame_ticks
        last_build = endgame_start - 1   # last tick a rush is honoured
        if tick >= last_build:
            return set()
        miners = [bid for bid, role in self.role.items() if role == ROLE_MINER]
        if not miners:
            return set()

        fab = state.fabricator_me
        free_slots = BOTS_MAX - len(me)
        # Replacements we can pay for: 50 tokens each, plus the free build if it will fire
        # (a full fleet holds a due free build, and it lands the tick a slot opens).
        builds = int(fab.tokens // conf.fabricator.rush_cost)
        if fab.next_bot_creation <= last_build:
            builds += 1
        # Free slots are filled first; only the rest need an extractor to make room. One bot
        # is built per tick, so no more swaps than there are ticks left to build in.
        k = min(len(miners), builds - free_slots, last_build - tick)
        if k <= 0:
            return set()
        # Not yet: the replacements would still be walking when the endgame starts.
        if tick < endgame_start - self._walk_ticks(state, conf) - k - SWAP_MARGIN:
            return set()

        miners.sort(key=lambda bid: (me[bid].health, bid))
        chosen = set(miners[:k])
        self.swap_done = True
        print(
            f"[plan] tick {tick}: self-destructing {k} of {len(miners)} extractors "
            f"({fab.tokens:.0f} tokens banked) to refill the slots with fighters"
        )
        return chosen

    def _walk_ticks(self, state, conf):
        """Ticks a bot built at our spawn needs to reach the payload."""
        p = state.payload_pos()
        d = path_length(Vec2(self.spawn[0], self.spawn[1]), Vec2(p.x, p.y))
        if d is None:
            d = 1.4 * _dist(self.spawn[0], self.spawn[1], p.x, p.y)
        return int(d / conf.bot.speed) + SWAP_ARRIVAL_SLACK

    def _pool_class(self):
        """Class for a bot headed for the attack pool (base bots now, payload bots later).

        Keeps the pool at BASE_SHOOTERS_PER_HEALER shooters per healer by looking at who is
        actually alive, not at a running count, so it stays right after deaths and no matter
        which kind of build (free, rush or post-swap refill) produced the bots so far. A healer
        is due once there are enough shooters to have earned another one.
        """
        shooters = healers = 0
        for bid, role in self.role.items():
            if role != ROLE_BASE and role != ROLE_ATTACK:
                continue
            if self.cls[bid] == BotClass.Battle:
                shooters += 1
            elif self.cls[bid] == BotClass.Healer:
                healers += 1
        if shooters >= BASE_SHOOTERS_PER_HEALER * (healers + 1):
            return BotClass.Healer
        return BotClass.Battle

    def _want_extractor(self, state, conf):
        """Should the next build replace a lost extractor?

        Yes while there are fewer than N_MINERS, unless: an enemy shooter is at the deposit
        (a new extractor would only walk into the fight), or it could not arrive and mine for
        EXTRACTOR_MIN_MINE_TICKS before the extractors are swapped out for fighters (see
        SWAP_EXTRACTORS) or the endgame starts. A bot built too late to earn anything is just
        50 tokens not spent on a fighter.
        """
        if not REPLACE_EXTRACTORS:
            return False
        miners = sum(1 for role in self.role.values() if role == ROLE_MINER)
        if miners >= N_MINERS or self.quiet == 0:
            return False
        cutoff = conf.max_ticks - conf.endgame_ticks
        if SWAP_EXTRACTORS:
            cutoff -= self._walk_ticks(state, conf) + N_MINERS + SWAP_MARGIN
        return state.tick + self.dep_walk_ticks + EXTRACTOR_MIN_MINE_TICKS <= cutoff

    def _escort_shooters(self):
        """How many shooters are currently guarding the extractors."""
        return sum(
            1 for bid, role in self.role.items()
            if role == ROLE_ESCORT and self.cls[bid] == BotClass.Battle
        )

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
            role = ROLE_MINER if cls == BotClass.Extractor else ROLE_ESCORT
            self.built += 1
        elif self._want_extractor(state, conf):
            # Income comes first: with no extractors nothing is ever earned to build with, so a
            # lost extractor takes the next build, free or paid.
            cls = BotClass.Extractor
            role = ROLE_MINER
        elif natural_due:
            if tick < SWITCH_TICK and self._escort_shooters() < ESCORT_SHOOTER_CAP:
                cls = BotClass.Battle       # refills the extraction guard
                role = ROLE_ESCORT
            else:
                cls = self._pool_class()
                role = ROLE_ATTACK if tick >= SWITCH_TICK else ROLE_BASE
        elif tick < SWITCH_TICK and REPLACE_WITH_RUSH and self._escort_shooters() < ESCORT_SHOOTER_CAP:
            cls = BotClass.Battle
            role = ROLE_ESCORT
        else:
            cls = self._pool_class()
            role = ROLE_ATTACK if tick >= SWITCH_TICK else ROLE_BASE

        action.fabricator_next = int(cls)
        action.rush_order = not natural_due
        self.pending = (role, cls)

    # ---------------------------------------------------------------------------------
    # where each role stands
    # ---------------------------------------------------------------------------------

    def _target(self, bid, bot, state):
        role = self.role[bid]
        if role == ROLE_MINER or role == ROLE_ESCORT:
            if role == ROLE_MINER:
                order = self.miner_order
            elif bot.class_ == BotClass.Healer:
                order = self.healer_order
            else:
                order = self.front_order if self._post_of(bid) == "front" else self.rear_order
            idx = self.dep_book.assign(bid, order)
            if idx is None:
                return None
            if role == ROLE_ESCORT and bot.class_ == BotClass.Battle:
                idx = self._escort_flank(bid, bot, idx)
            return self.dep_spots[idx]
        if role == ROLE_BASE:
            order = self.base_healer_order if bot.class_ == BotClass.Healer else self.base_order
            idx = self.base_book.assign(bid, order)
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
            # Settled on its spot but every enemy it can see is behind the payload (or a
            # wall corner): shift round the ring to a spot with a clear shot.
            if bot.class_ == BotClass.Battle and _dist(bot.pos.x, bot.pos.y, x, y) <= 0.35:
                seen = self._flank_wanted(bot)
                if seen:
                    used = {v for b, v in self.ring_of.items() if b != bid and v in valid}
                    cands = [(i, vx, vy, 0 if k <= 1 else 1) for i, (vx, vy, k) in valid.items()]
                    new = self._pick_flank(bot, cands, used, seen)
                    if new is not None:
                        self.ring_of[bid] = new
                        x, y, _ = valid[new]
            return (x, y)

        used = {v for b, v in self.ring_of.items() if b != bid and v in valid}
        best = None
        for idx, (x, y, k) in valid.items():
            if idx in used:
                continue
            # Shooters take the exposed spots (nearest the enemy), healers the sheltered ones;
            # a small pull towards the bot's own position breaks ties.
            expo = self._exposure(x, y)
            lead = expo if bot.class_ == BotClass.Battle else -expo
            key = (0 if k <= 1 else 1, lead + 0.02 * _dist(bot.pos.x, bot.pos.y, x, y))
            if best is None or key < best[0]:
                best = (key, idx)
        if best is None:
            p = state.payload_pos()
            return (p.x, p.y)
        self.ring_of[bid] = best[1]
        x, y, _ = valid[best[1]]
        return (x, y)

    # ---------------------------------------------------------------------------------
    # solids: the payload and the deposits block shots and bodies
    # ---------------------------------------------------------------------------------

    def _blocked(self, ax, ay, bx, by):
        """Would the payload or a deposit swallow a shot from a to b?"""
        a, b = Vec2(ax, ay), Vec2(bx, by)
        for c, r in self._solids:
            if point_seg_dist(c, a, b) < r:
                return True
        return False

    def _clear(self, x, y, eid, ex, ey):
        """Could a shooter standing at (x, y) actually hit enemy `eid` at (ex, ey)?"""
        key = (round(x, 2), round(y, 2), eid)
        v = self._clear_cache.get(key)
        if v is None:
            d = math.hypot(ex - x, ey - y)
            v = (
                d <= self.conf.bot.blaster_range - 0.3
                and line_of_sight(Vec2(x, y), Vec2(ex, ey))
                and not self._blocked(x, y, ex, ey)
            )
            self._clear_cache[key] = v
        return v

    def _flank_wanted(self, bot):
        """The enemies this shooter can see but not hit from where it stands.

        Returns None when there is nothing to fix: no enemy in reach, or at least one that
        it can already hit.
        """
        # Only enemies genuinely within reach count: one that is merely a bit too far away
        # is not an obstacle problem, and chasing it is not what a guard or a holder does.
        rng = self.conf.bot.blaster_range - 0.3
        here = Vec2(bot.pos.x, bot.pos.y)
        seen = []
        for eid, ex, ey in self._enemy_pts:
            if math.hypot(ex - bot.pos.x, ey - bot.pos.y) > rng:
                continue
            if not line_of_sight(here, Vec2(ex, ey)):
                continue
            seen.append((eid, ex, ey))
        if not seen:
            return None
        for eid, ex, ey in seen:
            if self._clear(bot.pos.x, bot.pos.y, eid, ex, ey):
                return None
        return seen

    def _pick_flank(self, bot, cands, used, seen):
        """The nearest free standing spot with a clear shot at any of `seen`, or None."""
        best = None
        for idx, x, y, pref in cands:
            if idx in used:
                continue
            if not any(self._clear(x, y, eid, ex, ey) for eid, ex, ey in seen):
                continue
            key = (pref, _dist(bot.pos.x, bot.pos.y, x, y))
            if best is None or key < best[0]:
                best = (key, idx)
        return best[1] if best is not None else None

    def _escort_flank(self, bid, bot, idx):
        """Same idea for a guard: shift to another guard spot if a deposit blocks its shot."""
        x, y = self.dep_spots[idx]
        if _dist(bot.pos.x, bot.pos.y, x, y) > 0.35:
            return idx
        seen = self._flank_wanted(bot)
        if not seen:
            return idx
        used = set(self.dep_book.of.values())
        cands = [
            (i, self.dep_spots[i][0], self.dep_spots[i][1], 0)
            for i in self.escort_only
            if _dist(self.dep_spots[i][0], self.dep_spots[i][1], self.dep_x, self.dep_y) <= 6.0
        ]
        new = self._pick_flank(bot, cands, used, seen)
        if new is None:
            return idx
        self.dep_book.of[bid] = new
        return new

    # ---------------------------------------------------------------------------------
    # the extraction guard: calm posts, and running to where the enemy is
    # ---------------------------------------------------------------------------------

    def _post_of(self, bid):
        """A guard shooter's calm-state post, keeping the front/rear split even."""
        if bid not in self.post:
            n_front = int(math.ceil(GUARD_FRONT_SHARE * ESCORT_SHOOTER_CAP))
            fronts = sum(1 for p in self.post.values() if p == "front")
            self.post[bid] = "front" if fronts < n_front else "rear"
        return self.post[bid]

    def _update_guard(self, me, tick):
        """Alert the guard when an enemy comes near the deposit, stand it down afterwards."""
        reach = self.conf.bot.blaster_range + GUARD_ALERT_MARGIN
        near = None
        for _, ex, ey in self._threat_pts:
            d = math.hypot(ex - self.dep_x, ey - self.dep_y)
            if d <= reach and (near is None or d < near[0]):
                near = (d, ex, ey)
        self.quiet = self.quiet + 1 if near is None else 0

        self._collapse_check(me, tick, near is not None)

        guards = sorted(
            bid for bid, role in self.role.items()
            if role == ROLE_ESCORT and me[bid].class_ == BotClass.Battle
        )

        if near is None or not guards:
            if self.alert:
                self.calm += 1
                if self.calm >= GUARD_CALM_TICKS or not guards:
                    self._stand_down(guards)
            return

        self.calm = 0
        _, ex, ey = near
        moved = _dist(ex, ey, self.alert_at[0], self.alert_at[1]) > GUARD_REPLAN_DIST
        if not self.alert or moved or tick - self.alert_tick >= GUARD_REPLAN_TICKS:
            self._plan_defense(guards, me, ex, ey)
            self.alert = True
            self.alert_at = (ex, ey)
            self.alert_tick = tick

    def _collapse_check(self, me, tick, attacked):
        """Send the guard to the payload if the enemy leaves the extraction site alone.

        Only the guard's shooters and healer go; the extractors stay and keep mining. It
        happens once the deposit has had no enemy near it for COLLAPSE_QUIET_TICKS, from
        COLLAPSE_AFTER_TICK onwards. If an enemy does turn up at the deposit while the
        collapsed bots are still within RECALL_RANGE of it, they go back to guarding;
        further away than that the trip back would take too long to matter, so they carry on.
        """
        if not COLLAPSE_ENABLED:
            return

        if attacked:
            for bid in list(self.collapsed):
                bot = me.get(bid)
                if bot is None or self.role.get(bid) != ROLE_ATTACK:
                    self.collapsed.discard(bid)
                    continue
                if _dist(bot.pos.x, bot.pos.y, self.dep_x, self.dep_y) <= RECALL_RANGE:
                    self.role[bid] = ROLE_ESCORT
                    self.ring_of.pop(bid, None)
                    self.collapsed.discard(bid)
                    print(f"[plan] tick {tick}: enemy at the deposit, bot {bid} returns to guard")
                else:
                    self.collapsed.discard(bid)   # too far to matter: it stays on the payload
            return

        if tick < COLLAPSE_AFTER_TICK or self.quiet < COLLAPSE_QUIET_TICKS:
            return
        moved = 0
        for bid, role in list(self.role.items()):
            if role != ROLE_ESCORT:
                continue
            self.role[bid] = ROLE_ATTACK
            self.dep_book.drop(bid)
            self.post.pop(bid, None)
            self.collapsed.add(bid)
            moved += 1
        if moved:
            self.alert = False
            self.calm = 0
            print(
                f"[plan] tick {tick}: deposit quiet for {self.quiet} ticks -- "
                f"{moved} guard bots collapse on the payload"
            )

    def _plan_defense(self, guards, me, ex, ey):
        """Send the guard shooters to the spots nearest the enemy, quickest arrival first.

        Spots are taken in order of how near they are to the enemy, and each goes to whichever
        shooter is closest to it, so the shooters that were at the back run round to help
        rather than everyone shuffling one place along.
        """
        for bid in guards:
            self.dep_book.of.pop(bid, None)
        held = set(self.dep_book.of.values())    # the healer's and extractors' spots stay theirs
        spots = [i for i in self.guard_idx if i not in held]
        spots.sort(key=lambda i: _dist(self.dep_spots[i][0], self.dep_spots[i][1], ex, ey))

        remaining = set(guards)
        for i in spots:
            if not remaining:
                break
            sx, sy = self.dep_spots[i]
            bid = min(remaining, key=lambda b: _dist(me[b].pos.x, me[b].pos.y, sx, sy))
            remaining.discard(bid)
            self.dep_book.of[bid] = i
        # Anyone left over gets a free spot the normal way, in `_target`.

    def _stand_down(self, guards):
        """Threat gone: everyone goes back to the calm formation."""
        for bid in guards:
            self.dep_book.of.pop(bid, None)
        self.alert = False
        self.calm = 0

    def _exposure(self, x, y):
        """How near the front a spot is: distance to the nearest enemy (smaller = more
        exposed), or to the enemy's spawn when none is on the field."""
        best = None
        for _, ex, ey in self._threat_pts:
            d = math.hypot(ex - x, ey - y)
            if best is None or d < best:
                best = d
        if best is None:
            best = _dist(x, y, self.enemy_spawn[0], self.enemy_spawn[1])
        return best

    def _cover_target(self, bot, target, me):
        """Keep a healer or extractor behind our shooters.

        Returns `target` unchanged unless an enemy is close and that target would put the bot
        closer to it than our frontmost nearby shooter, in which case it returns a spot just
        behind that shooter. It is a pure function of where everyone stands, so a bot that has
        tucked in stays tucked in instead of oscillating back to its old spot.
        """
        px, py = bot.pos.x, bot.pos.y
        reach = self.conf.bot.blaster_range + COVER_TRIGGER
        nearest = None
        for _, ex, ey in self._threat_pts:
            d = math.hypot(ex - px, ey - py)
            if d <= reach and (nearest is None or d < nearest[0]):
                nearest = (d, ex, ey)
        if nearest is None:
            return target
        _, ex, ey = nearest

        shooters = [
            b for b in me.values()
            if b.class_ == BotClass.Battle and _dist(b.pos.x, b.pos.y, px, py) <= COVER_RANGE
        ]
        if not shooters:
            return target
        front = min(shooters, key=lambda b: _dist(b.pos.x, b.pos.y, ex, ey))
        d_front = _dist(front.pos.x, front.pos.y, ex, ey)
        if d_front < 1e-6:
            return target

        tx, ty = target if target is not None else (px, py)
        if d_front + COVER_MARGIN <= _dist(tx, ty, ex, ey):
            return target   # already behind a shooter

        ux, uy = (front.pos.x - ex) / d_front, (front.pos.y - ey) / d_front
        cx, cy = front.pos.x + ux * COVER_BEHIND, front.pos.y + uy * COVER_BEHIND
        if not self._standable(cx, cy):
            return target
        return (cx, cy)

    def _detour(self, ax, ay, tx, ty):
        """Where to steer this tick: the target, or a waypoint that rounds a solid in the way.

        `navigate_to` knows about walls but not about the payload or the deposits, so a
        straight line through one of them would just grind against it.
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
            # Which side of our line is it on? Pass on the other one.
            side = (c.x - ax) * (-uy) + (c.y - ay) * ux
            s = 1.0 if side >= 0.0 else -1.0
            off = clear_r + 0.3
            for sgn in (s, -s):
                wx, wy = c.x + sgn * uy * off, c.y - sgn * ux * off
                if self._standable(wx, wy):
                    return wx, wy
        return tx, ty

    # ---------------------------------------------------------------------------------
    # spacing
    # ---------------------------------------------------------------------------------

    def _spread(self, me, des, conf):
        speed = conf.bot.speed
        ids = list(me)

        threatened = {}
        for bid in ids:
            p = me[bid].pos
            threatened[bid] = any(
                _dist(p.x, p.y, ex, ey) <= self.threat_range for _, ex, ey in self._threat_pts
            )

        q = {
            bid: (me[bid].pos.x + des[bid][0] * speed, me[bid].pos.y + des[bid][1] * speed)
            for bid in ids
        }

        # In a narrow spot (a 2-wide gap, a corner) sideways pushing just jams bots against the
        # walls, so there the rule is a queue instead: a bot only slows for whoever is ahead of
        # it, and the one in front is never held up.
        tight = {
            bid: not disc_free(Vec2(me[bid].pos.x, me[bid].pos.y), self.conf.bot.radius + 0.55)
            for bid in ids
        }

        final = {}
        for i in ids:
            rx = ry = 0.0
            slow = 1.0
            for j in ids:
                if i == j:
                    continue
                if not (ALWAYS_SPREAD or threatened[i] or threatened[j]):
                    continue
                dx, dy = q[i][0] - q[j][0], q[i][1] - q[j][1]
                d = math.hypot(dx, dy)
                if d >= self.rep_start:
                    continue
                if tight[i]:
                    ahead = des[i][0] * (q[j][0] - q[i][0]) + des[i][1] * (q[j][1] - q[i][1])
                    if ahead > 0.0:
                        slow = min(slow, max(0.0, (d - self.hard) / (self.rep_start - self.hard)))
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
            if tight[i]:
                final[i] = (des[i][0] * slow, des[i][1] * slow)
            else:
                final[i] = _clip(des[i][0] + rx, des[i][1] + ry, 1.0)
        return final

    # ---------------------------------------------------------------------------------
    # getting unstuck
    # ---------------------------------------------------------------------------------

    def _stuck_check(self, bid, bot, target, des, tick):
        """Watch one bot for lack of progress; return an escape direction while it is stuck.

        A bot is stuck if, over the last STUCK_WINDOW ticks, it wanted to move on nearly every
        tick and yet ended up less than STUCK_MIN_MOVE from where it started, with somewhere
        still to go. It then ignores spacing for up to STUCK_ESCAPE_TICKS (or until it is
        STUCK_CLEAR_DIST clear of where it got wedged) and steers by `_unstick_dir`.
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
                or _dist(bot.pos.x, bot.pos.y, esc[0], esc[1]) >= STUCK_CLEAR_DIST
            )
            if done:
                del self.escape[bid]
                hist.clear()
                return None
            return self._unstick_dir(bot, target)

        if target is None or len(hist) < STUCK_WINDOW + 1:
            return None
        moved = _dist(bot.pos.x, bot.pos.y, hist[0][0], hist[0][1])
        tried = sum(1 for _, _, w in hist if w)
        if (tried >= 0.8 * len(hist) and moved < STUCK_MIN_MOVE
                and _dist(bot.pos.x, bot.pos.y, target[0], target[1]) > 1.5):
            self.escape[bid] = (bot.pos.x, bot.pos.y, tick + STUCK_ESCAPE_TICKS)
            print(
                f"[plan] tick {tick}: bot {bid} stuck at ({bot.pos.x:.1f}, {bot.pos.y:.1f}), "
                f"steering out"
            )
            return self._unstick_dir(bot, target)
        return None

    def _unstick_dir(self, bot, target):
        """A unit direction that gets a wedged bot moving: of the ways it can really walk, the
        one that leaves the shortest route to where it is going.

        Path length is the engine's own walking distance around walls, so a step that
        reduces it is real progress even when the straight line to the target is blocked.
        """
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
            return _clip(v.x, v.y, 1.0)
        return (best[1], best[2])

    # ---------------------------------------------------------------------------------
    # battle bots
    # ---------------------------------------------------------------------------------

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
            # A shot into the payload or a deposit detonates on it and wastes the cooldown,
            # so an enemy hiding behind one is the last choice (the flanking logic in
            # `_target` is what moves us to a spot where it becomes a first choice).
            blocked = self._blocked(nx, ny, ex, ey)
            score = e.health + turn_needed / 20.0
            if self.aim.get(bid) == e.id:
                score -= 1.0
            if not vulnerable:
                score += 100.0
            if blocked:
                score += 50.0
            cands.append((score, e.id, d, ang, vulnerable, blocked))

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
        score, eid, d, ang, vulnerable, blocked = min(pool)
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
            and not blocked
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
