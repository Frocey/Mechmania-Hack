import math
import traceback

from . import *

# =====================================================================================
# The plan: hold our own extractor behind choke points, then push the payload
#
#   Until the endgame starts
#     * Opening: the free tick-0 bot + 16 rush orders (the 800 starting tokens) = 17 bots:
#       8 extractors, 6 shooters, 3 healers. The extractors mine OUR deposit.
#     * Every build after that (free or rush) is a shooter or healer for the defence (2 shooters
#       per healer) until there are FIGHTER_TARGET of them; only then are lost extractors replaced,
#       so there are always 8 mining.
#     * The defence holds the "gates": the narrow places the enemy has to come through to
#       reach our half. At each gate the shooters stand where they can see the spot the
#       enemy steps out of but the approach behind it cannot see them, so the enemy is
#       fed through one at a time into everyone's fire. Healers stand behind the shooters.
#       Which gate gets the men follows where the enemy actually is.
#     * Nobody goes north of the line y = Y_LINE (the far side of the wall that closes off
#       our half).
#   Around the time a full fleet turns its extractors into fighters (see SWAP_EXTRACTORS; nothing
#   can be built once the endgame starts), the whole team leaves home for the payload:
#     * It escorts the payload forward, shooters in front, but only as far as the two doorways
#       the enemy has to come through to reach it (HOLD_KILL). Then it stops pushing and holds
#       ONE formation that follows HOLD_LINE, an L along the wall that can see both doorways:
#       shooters on the line, healers behind them. Nobody leaves the formation to chase.
#     * That position is held to the end of the match: the payload sits on the enemy's side of
#       centre, so it wins on timeout, or the enemy dies feeding into the doorways. If the
#       payload is ever pushed back, the team goes back to pushing it.
#
#   Always: no two of our bots may sit inside one blaster splash of each other.
# =====================================================================================

N_MINERS = 8
N_OPENING_SHOOTERS = 6
N_OPENING_HEALERS = 3
OPENING = (
    [BotClass.Extractor] * N_MINERS
    + [BotClass.Battle] * N_OPENING_SHOOTERS
    + [BotClass.Healer] * N_OPENING_HEALERS
)

# The defence (and later the payload team) is kept at this many shooters per healer, for every
# build that joins it: 2 shooters per healer, i.e. one healer for every two shooters. (The 17-bot
# opening is 6 shooters and 3 healers, exactly that.)
BASE_SHOOTERS_PER_HEALER = 2

# Our half is y >= Y_LINE (in our own frame; the engine mirrors the world for the other side).
# Bots stay Y_MARGIN below it until the endgame.
Y_LINE = 24.0
Y_MARGIN = 0.35

# --- spacing -------------------------------------------------------------------------
# A shot detonates on the first enemy it meets and hurts every bot whose centre is within
# `base_blaster_splash_radius + bot.radius` of that impact point. Two bots are safe from a single
# blast if their centres are at least ~0.8 apart; `hard` adds a margin, and all standing spots
# are on a lattice `spacing` (~1.05) apart, so a bot at rest is never inside anyone's splash.
#
# Spawning is the one place this cannot hold: the engine puts every new bot on the same spot,
# one per tick. Spacing is therefore enforced whenever an enemy shooter is within
# `blaster_range + THREAT_MARGIN` of either bot. Set ALWAYS_SPREAD = True to enforce it always.
THREAT_MARGIN = 6.0
ALWAYS_SPREAD = False

# --- extractors ----------------------------------------------------------------------
# Mining spots are searched within this distance of the deposit centre (the engine's own
# extract range is 5 from the bot centre to the deposit's hull, i.e. 5.5 from its centre; stay
# a little inside it). Every such spot with a clear line to the deposit can mine.
MINER_MAX_DIST = 5.0
# Extractors keep as far from enemy shooters as they can while still mining. Every
# MINER_REPLAN_TICKS ticks, starting with the most endangered, each extractor moves to the free
# mining spot furthest from the nearest enemy shooter, but only if that spot is at least
# MINER_MOVE_GAIN further away than where it stands (so it does not shuffle for nothing).
# With no enemy shooter on the field they stay where they are.
MINER_REPLAN_TICKS = 20
MINER_MOVE_GAIN = 1.5
MINER_SAFE_CAP = 40.0

# Lost extractors are replaced (up to N_MINERS), but only once the defence has FIGHTER_TARGET
# shooters and healers: until then every build goes to fighters. The exception is
# EXTRACTOR_FLOOR: with fewer extractors than that nothing would ever be earned to build fighters
# with, so one is replaced regardless. Replacements stop once one could no longer arrive and mine
# for EXTRACTOR_MIN_MINE_TICKS before the swap below.
REPLACE_EXTRACTORS = True
FIGHTER_TARGET = 16
EXTRACTOR_FLOOR = 1
EXTRACTOR_MIN_MINE_TICKS = 300

# --- gates (choke points) --------------------------------------------------------------
# Found from the map: routes are traced from a grid of points outside our half to our deposit;
# where each route crosses Y_LINE we look for its narrowest spot (the gate).
GATE_ORIGIN_STEP = 4        # spacing of the grid the routes start from
GATE_MERGE_DIST = 3.0       # gates closer than this are the same gate
GATE_BEFORE = 3             # samples (0.5 apart) before the crossing that may hold the gate
GATE_AFTER = 30             # ... and after it
GATE_TIE = 0.3              # the first spot within this of the narrowest width is the gate
GATE_LANE = 16              # samples of approach behind the gate that must not see the defenders
GATE_EXIT = 6               # samples of route from the gate on: the kill zone
GATE_SPOT_MIN = 2.5         # defenders stand this far from the gate ...
GATE_SPOT_MAX = 9.0         # ... and no further than this
LANE_WEIGHT = 2.0           # how much being seen from the approach costs a spot
MAX_GATES = 5
# Used only if no gate can be found from the map: (x, y) of each gate, in our frame.
GATE_SEEDS = [(30.5, 28.5), (22.5, 23.5), (1.5, 23.5)]

# Which gate gets the defenders. Every enemy shooter counts towards the gate it would reach
# first (nearer = heavier); the counts are smoothed and added to a prior that favours the gate
# on the enemy's shortest route to our deposit. Defenders are only moved between gates when a
# gate is REBALANCE_SLACK men short, and at most once per REBALANCE_TICKS.
PRESSURE_TICKS = 15
PRESSURE_RANGE = 90.0
PRIOR_PRIMARY = 0.5
PRIOR_WEIGHT = 1.0
REBALANCE_TICKS = 45
REBALANCE_SLACK = 2
FLANK_POOL = 40             # how many of a gate's best spots a defender may shift between

# --- rotating wounded shooters out of the line ---------------------------------------------
# A shooter below RETREAT_FRAC of full health falls back to a spot within heal range of a healer
# (further back than it stands, out of the enemy's sight where the map allows) and stays there
# until it has healed to RECOVER_FRAC. The spot it left is taken by the rear-most healthy shooter
# (any healthy shooter steps up into a free spot at least ROTATE_GAP places better than its own),
# so the line stays full and the wounded and the healed keep trading places. A bot moves at most
# once per ROTATE_COOLDOWN ticks, so it cannot flap back and forth.
RETREAT_FRAC = 0.5
RECOVER_FRAC = 0.8
ROTATE_COOLDOWN = 40
ROTATE_GAP = 3

# --- shooters in front ---------------------------------------------------------------
# Healers and extractors never stand closer to an enemy than our nearest shooter does. When an
# enemy shooter is within blaster_range + COVER_TRIGGER of one and one of ours is within
# COVER_RANGE, the support bot steps to COVER_BEHIND behind that shooter (on the side away from
# the enemy) if its own spot would be less than COVER_MARGIN further back than the shooter is.
COVER_TRIGGER = 3.0
COVER_RANGE = 10.0
COVER_MARGIN = 0.5
COVER_BEHIND = 1.3

# --- stuck detection -----------------------------------------------------------------------
# Expected travel is 0.05 a tick, i.e. 2.0 over STUCK_WINDOW ticks; under STUCK_MIN_MOVE means
# wedged. A stuck bot ignores spacing and steers out for at most STUCK_ESCAPE_TICKS.
STUCK_WINDOW = 40
STUCK_MIN_MOVE = 0.5
STUCK_ESCAPE_TICKS = 60
STUCK_CLEAR_DIST = 2.0

# --- pre-endgame extractor swap --------------------------------------------------------
# Nothing is built once the endgame starts, so a full fleet is stuck with whatever it has. If
# the fleet is full, the extractors are turned into fighters just before that: they
# self-destruct together (they still mine on that tick), which opens their slots, and the banked
# tokens rush shooters/healers into them, one per tick. How many go is limited by the tokens
# banked (50 per replacement, plus the free build if one is due), and a swap that is short of
# tokens is topped up on later ticks. It starts
#     endgame_start - SWAP_LEAD_TICKS - swaps - SWAP_MARGIN
# so the last replacement is built SWAP_LEAD_TICKS before the endgame and has time to get
# into position for the march.
SWAP_EXTRACTORS = True
SWAP_MARGIN = 6            # ticks of slack for a skipped tick (compute budget)
SWAP_LEAD_TICKS = 150

# How many extra ticks to allow on top of a straight walk (turning, detours, spacing).
WALK_SLACK = 40

# --- the payload push and hold --------------------------------------------------------
# All in OUR frame (the engine mirrors the world for the other side, so these are right
# whichever side we spawn on).
#
# HOLD_KILL: the two doorways to hold, each as a line the enemy has to cross: the mouth of the
# doorway north of the payload's corner (y = 13.8, x from 21.6 to 24.5) and the doorway east of
# it (x = 25.6, y from 15 to 16.8). The firing formation is picked to see both.
HOLD_KILL = [((21.6, 13.8), (24.5, 13.8)), ((25.6, 15.0), (25.6, 16.8))]
# HOLD_LINE: where the team stands, an L: down the west side then along the south wall. Shooters
# fill the line (and, if there are more of them, the rows just in front of it that are still
# clear of the payload); healers stand behind them.
HOLD_LINE = [(18.4, 14.5), (18.4, 17.5), (24.5, 17.5)]
HOLD_BAND = 3.0             # standing spots are searched this close to HOLD_LINE
# The formation is packed tighter than the rest of the plan so that more bots fit on the line:
# spots are HOLD_SPACING apart (a little inside one blaster splash of each other, on purpose),
# and once the main spots are full a second lattice halfway between them takes the overflow.
# While the team is holding, moving bots only keep HOLD_HARD apart (HOLD_REP is where they start
# to back off), so they can settle at that spacing without shoving each other out of place.
HOLD_SPACING = 0.8
HOLD_HARD = 0.55
HOLD_REP = 0.7
# The payload is pushed until it is HOLD_CLEAR from every spot on HOLD_LINE (just over its
# 2.5 capture radius), at which point nobody in the formation can push it any further. The team
# starts standing off when it is PUSH_LEAD_ARC short of that (it takes a moment to walk out of
# the capture radius and the payload keeps rolling meanwhile), and goes back to pushing if it is
# knocked back by REPUSH_ARC or more.
PUSH_LEAD_ARC = 0.8
REPUSH_ARC = 2.0
HOLD_CLEAR = 2.65
# Start the march this many ticks before the swap below would begin (0 = together with it).
PUSH_EARLY_TICKS = 0

ROLE_MINER = "miner"        # extractor at our deposit
ROLE_DEFENDER = "defender"  # shooter / healer: holds the gates, later escorts/holds the payload


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


class Gate:
    """A narrow place the enemy has to come through to reach our half."""

    def __init__(self, cx, cy, lane, exit_pts):
        self.cx, self.cy = cx, cy
        self.lane = lane          # route points on the enemy's side, before the gate
        self.exit = exit_pts      # route points from the gate on: where the enemy steps out
        self.width = 0.0
        self.hits = 1
        self.prior = 0.0
        self.order = []           # standing spots (grid indices), best first
        self.rank = {}            # grid index -> place in `order` (0 = the best, front-most spot)
        self.info = {}            # grid index -> (exit points seen, lane points seen)


class Plan:
    def __init__(self):
        self.ready = False
        self.role = {}          # bot id -> role
        self.cls = {}           # bot id -> BotClass it was born as (catches id reuse)
        self.pending = None     # (role, class) of the bot we ordered last tick
        self.built = 0
        self.aim = {}           # bot id -> enemy id it was last aiming at
        self.miner_book = SlotBook()
        self.gate_of = {}       # defender id -> index of the gate it holds
        self.spot_of = {}       # defender id -> grid index of its standing spot
        self.taken = {}         # grid index -> defender id
        self.pressure = []
        self.weights = []
        self.last_pressure = -10 ** 9
        self.last_rebalance = -10 ** 9
        self.ring_of = {}
        self._ring_tick = -1
        self._ring_cache = {}
        self._width_cache = {}
        self.mode = "home"      # "home" (holding the gates) -> "push" <-> "hold" (at the chokes)
        self.gates = []         # the gates of the front in use (home or payload chokes)
        self.fgrid = []         # ... and the standing spots they refer to
        self.swap_done = False
        self.recovering = set()  # shooters that have fallen back to be healed
        self.moved_at = {}       # shooter id -> tick of its last rotation move
        self.last_miner_plan = -10 ** 9
        self.hist = {}          # bot id -> recent (x, y, wanted_to_move) for the stuck check
        self.escape = {}        # bot id -> (x, y, until_tick) while it steers out of a jam

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
        self.endgame_start = conf.max_ticks - conf.endgame_ticks
        self.y_min = Y_LINE + Y_MARGIN

        self.spawn = (b.radius + 0.002, float(MAP_SIZE) - b.radius - 0.002)
        self.enemy_spawn = (float(MAP_SIZE) - self.spawn[0], float(MAP_SIZE) - self.spawn[1])

        d = state.deposit_me.pos
        self.dep_x, self.dep_y = d.x, d.y
        gx, gy = self.dep_x, self.dep_y + conf.deposit.radius + b.radius + 0.05
        if not self._standable(gx, gy):
            gx, gy = self.dep_x - conf.deposit.radius - b.radius - 0.05, self.dep_y
        self.goal = (gx, gy)

        # The march starts about when the extractor swap would (see SWAP_EXTRACTORS).
        self.push_start = (
            self.endgame_start - SWAP_LEAD_TICKS - N_MINERS - SWAP_MARGIN - PUSH_EARLY_TICKS
        )

        # Front 1: the gates of our half, held until the march.
        self.grid = self._build_grid()
        self.home_gates = self._find_gates()
        self._set_priors(self.home_gates)
        self.mine_order = self._mining_spots()
        self.mine_set = set(self.mine_order[:N_MINERS])
        for g in self.home_gates:
            self._score_gate(g, self.grid, self.mine_set, None)

        # Front 2: the payload chokes, held from the march on.
        self._find_hold()
        self.push_grid, self.push_gates = self._build_push_front()

        self._use_front(self.home_gates, self.grid)

        # How long a new extractor takes to walk from our spawn to a mining spot.
        mine_at = self.grid[self.mine_order[0]] if self.mine_order else (self.dep_x, self.dep_y)
        self.dep_walk_ticks = self._walk_ticks(self.spawn, mine_at)

        self.ring_offsets = self._ring_offsets()

        self._describe("home gate", self.home_gates, self.grid)
        self._describe("payload formation", self.push_gates, self.push_grid)
        print(
            f"[plan] our deposit at ({self.dep_x:.1f}, {self.dep_y:.1f}); "
            f"{len(self.mine_order)} mining spots, {len(self.grid)} standing spots in our half; "
            f"march at tick {self.push_start}, payload to be pushed to capture "
            f"{self.hold_capture:.3f} (at {self.hold_pos[0]:.1f}, {self.hold_pos[1]:.1f})"
        )
        if len(self.mine_order) < N_MINERS:
            print(f"[plan] WARNING: only {len(self.mine_order)} spots with a sightline to the deposit")
        self.ready = True

    def _describe(self, label, gates, grid):
        for i, g in enumerate(gates):
            best = g.info[g.order[0]] if g.order else (0, 0)
            print(
                f"[plan] {label} {i} at ({g.cx:.1f}, {g.cy:.1f}): width {g.width:.1f}, "
                f"routes {g.hits}, prior {g.prior:.2f}, {len(g.order)} spots "
                f"(best sees {best[0]} exit / {best[1]} lane points)"
            )

    def _use_front(self, gates, grid):
        """Switch which set of gates (and standing spots) the defenders are held on."""
        self.gates = gates
        self.fgrid = grid
        self.gate_of = {}
        self.spot_of = {}
        self.taken = {}
        self.recovering = set()
        self.pressure = [0.0] * len(gates)
        self.weights = [g.prior for g in gates]
        self.last_pressure = -10 ** 9
        self.last_rebalance = -10 ** 9

    def _walk_ticks(self, frm, to):
        """Ticks to walk from `frm` to `to`, around walls."""
        d = path_length(Vec2(frm[0], frm[1]), Vec2(to[0], to[1]))
        if d is None:
            d = 1.4 * _dist(frm[0], frm[1], to[0], to[1])
        return int(d / self.conf.bot.speed) + WALK_SLACK

    def _standable(self, x, y):
        if x < 0.4 or y < 0.4 or x > self.map_max or y > self.map_max:
            return False
        return point_free(Vec2(x, y))

    def _reachable(self, x, y):
        return path_length(Vec2(self.spawn[0], self.spawn[1]), Vec2(x, y)) is not None

    def _build_grid(self):
        """Every standing spot in our half. One lattice for everything, so two spots are never
        closer than `spacing` however they are used."""
        pts = []
        y = self.y_min
        while y <= self.map_max:
            x = 0.5
            while x <= self.map_max:
                if self._standable(x, y) and self._reachable(x, y):
                    pts.append((x, y))
                x += self.spacing
            y += self.spacing
        return pts

    # ---- gates -----------------------------------------------------------------------

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
        """How much room there is across the route at sample i: the free run of standable
        points to either side, at right angles to the direction of travel."""
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
        gate = Gate(route[ci][0], route[ci][1], route[max(0, ci - GATE_LANE):ci],
                    route[ci:ci + GATE_EXIT])
        gate.width = wmin
        return gate

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
                    if _dist(g.cx, g.cy, gate.cx, gate.cy) <= GATE_MERGE_DIST:
                        g.hits += 1
                        break
                else:
                    gates.append(gate)
        gates.sort(key=lambda g: -g.hits)
        gates = gates[:MAX_GATES]
        if not gates:
            gates = self._seed_gates()
        return gates

    def _seed_gates(self):
        print("[plan] WARNING: no gates found from the map, using GATE_SEEDS")
        gates = []
        for cx, cy in GATE_SEEDS:
            if self._standable(cx, cy):
                g = Gate(cx, cy, [], [(cx, cy)])
                g.width = 0.0
                gates.append(g)
        if not gates:
            gates = [Gate(16.0, 26.0, [], [(16.0, 26.0)])]
        return gates

    def _set_priors(self, gates):
        """Before any enemy is seen, favour the gate on the enemy's shortest route to us."""
        n_g = len(gates)
        route = self._polyline(
            Vec2(self.enemy_spawn[0], self.enemy_spawn[1]), Vec2(self.goal[0], self.goal[1])
        )
        primary = 0
        if route and n_g:
            gaps = [min(_dist(g.cx, g.cy, x, y) for x, y in route) for g in gates]
            primary = gaps.index(min(gaps))
        for i, g in enumerate(gates):
            if n_g == 1:
                g.prior = 1.0
            else:
                g.prior = PRIOR_PRIMARY if i == primary else (1.0 - PRIOR_PRIMARY) / (n_g - 1)

    def _score_gate(self, g, grid, exclude, solid):
        """Rank the standing spots around a gate.

        A good spot can see where the enemy steps out (the gate and the route just past it) but
        cannot be seen from the approach behind it, so the enemy has to come out into our fire
        one at a time before it can shoot back. Score = exit points seen - LANE_WEIGHT * lane
        points seen, ties going to the spot nearer the gate.

        `exclude` is a set of grid indices not to use; `solid` is an optional (centre, radius)
        that blocks shots, like the payload does at its hold point: a line through it is not a
        line of sight.
        """
        rng = self.conf.bot.blaster_range

        def sees(here, hx, hy, tx, ty):
            if not line_of_sight(here, Vec2(tx, ty)):
                return False
            if solid is not None and point_seg_dist(solid[0], here, Vec2(tx, ty)) < solid[1]:
                return False
            return True

        rows = []
        for idx, (x, y) in enumerate(grid):
            if idx in exclude:
                continue
            d = _dist(x, y, g.cx, g.cy)
            if d < GATE_SPOT_MIN or d > GATE_SPOT_MAX:
                continue
            here = Vec2(x, y)
            seen_exit = sum(
                1 for ex, ey in g.exit
                if _dist(x, y, ex, ey) <= rng - 1.0 and sees(here, x, y, ex, ey)
            )
            seen_lane = sum(1 for lx, ly in g.lane if sees(here, x, y, lx, ly))
            rows.append((seen_exit - LANE_WEIGHT * seen_lane, -d, idx, seen_exit, seen_lane))
        rows.sort(reverse=True)
        g.order = [r[2] for r in rows]
        g.rank = {idx: k for k, idx in enumerate(g.order)}
        g.info = {r[2]: (r[3], r[4]) for r in rows}

    # ---- the payload front -----------------------------------------------------------

    @staticmethod
    def _sample_polyline(pts, step):
        """Points along a polyline, `step` apart (the last one included)."""
        out = [pts[0]]
        carry = 0.0
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            length = _dist(x0, y0, x1, y1)
            pos = step - carry
            while pos <= length + 1e-9:
                t = pos / length if length > 0 else 0.0
                out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
                pos += step
            carry = length - (pos - step)
        if _dist(out[-1][0], out[-1][1], pts[-1][0], pts[-1][1]) > 0.3:
            out.append(pts[-1])
        return out

    @staticmethod
    def _poly_dist(x, y, pts):
        """Distance from (x, y) to a polyline."""
        best = 1e9
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            dx, dy = bx - ax, by - ay
            n2 = dx * dx + dy * dy
            t = 0.0 if n2 <= 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / n2))
            best = min(best, _dist(x, y, ax + dx * t, ay + dy * t))
        return best

    def _on_our_side(self, x, y, margin):
        """Is (x, y) on our side of both doorways (the same side as the formation)?"""
        for ax, ay, nx, ny in self.kill_sides:
            if nx * (x - ax) + ny * (y - ay) < margin:
                return False
        return True

    def _find_hold(self):
        """Work out the payload front from the drawn geometry.

        The doorways (HOLD_KILL) become rows of points the enemy must cross; HOLD_LINE becomes a
        row of standing points; and the payload is to be pushed to the first point on its path
        that is HOLD_CLEAR from all of them, so that everyone in the formation is just outside
        its capture radius and it stops rolling by itself.
        """
        self.kill_pts = []
        self.kill_sides = []
        line_pts = self._sample_polyline(HOLD_LINE, self.spacing)
        self.line_fine = self._sample_polyline(HOLD_LINE, 0.4)
        cx = sum(p[0] for p in line_pts) / len(line_pts)
        cy = sum(p[1] for p in line_pts) / len(line_pts)
        for (ax, ay), (bx, by) in HOLD_KILL:
            n = max(1, int(_dist(ax, ay, bx, by) / 0.5))
            for k in range(n + 1):
                t = k / n
                self.kill_pts.append((ax + (bx - ax) * t, ay + (by - ay) * t))
            length = _dist(ax, ay, bx, by)
            nx, ny = -(by - ay) / length, (bx - ax) / length
            if nx * (cx - ax) + ny * (cy - ay) < 0:      # point the normal at the formation
                nx, ny = -nx, -ny
            self.kill_sides.append((ax, ay, nx, ny))

        pts = [self.conf.payload_path[i] for i in range(PAYLOAD_PATH_LEN)]
        self.path_len = sum(
            _dist(pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y) for i in range(len(pts) - 1)
        )

        best = None
        fallback = None
        for step in range(0, 251):
            t = step * 0.002
            p = payload_pos(t)
            clear = min(_dist(p.x, p.y, x, y) for x, y in line_pts)
            if fallback is None or clear > fallback[0]:
                fallback = (clear, t, p.x, p.y)
            if clear >= HOLD_CLEAR:
                best = (clear, t, p.x, p.y)
                break
        _, self.hold_capture, hx, hy = best or fallback
        self.hold_pos = (hx, hy)
        self.hold_solid = (Vec2(hx, hy), self.conf.payload.radius)

    def _build_push_front(self):
        """The formation: one Gate whose spots are ranked for holding both doorways.

        Candidates are the standing spots within HOLD_BAND of HOLD_LINE that are on our side of
        the doorways and outside the payload's capture radius. Each is ranked by how many points
        of the doorways it can hit (with the payload in the way, as it will be) minus twice its
        distance from the line, so the line itself fills first, best-placed spots first.
        """
        hx, hy = self.hold_pos
        grid = []
        overflow = set()         # indices of the interleaved second lattice
        half = HOLD_SPACING / 2.0
        for offset, second in ((0.5, False), (0.5 + half, True)):
            y = offset
            while y <= self.map_max:
                x = offset
                while x <= self.map_max:
                    if (self._poly_dist(x, y, HOLD_LINE) <= HOLD_BAND
                            and _dist(x, y, hx, hy) >= HOLD_CLEAR
                            and self._on_our_side(x, y, 0.3)
                            and self._standable(x, y)
                            and self._on_line_side(x, y)
                            and self._reachable(x, y)):
                        if second:
                            overflow.add(len(grid))
                        grid.append((x, y))
                    x += HOLD_SPACING
                y += HOLD_SPACING

        gx = sum(p[0] for p in self.kill_pts) / len(self.kill_pts)
        gy = sum(p[1] for p in self.kill_pts) / len(self.kill_pts)
        gate = Gate(gx, gy, [], list(self.kill_pts))
        gate.prior = 1.0

        rng = self.conf.bot.blaster_range
        solid_c, solid_r = self.hold_solid
        rows = []
        for idx, (x, y) in enumerate(grid):
            here = Vec2(x, y)
            seen = 0
            for kx, ky in self.kill_pts:
                if _dist(x, y, kx, ky) > rng - 1.0:
                    continue
                target = Vec2(kx, ky)
                if not line_of_sight(here, target):
                    continue
                if point_seg_dist(solid_c, here, target) < solid_r:
                    continue
                seen += 1
            # The overflow lattice sits close to its neighbours, so it only fills once the
            # main spots around it are gone.
            score = seen - 2.0 * self._poly_dist(x, y, HOLD_LINE) - (2.0 if idx in overflow else 0.0)
            rows.append((score, -_dist(x, y, gx, gy), idx, seen))
        rows.sort(reverse=True)
        gate.order = [r[2] for r in rows]
        gate.rank = {idx: k for k, idx in enumerate(gate.order)}
        gate.info = {r[2]: (r[3], 0) for r in rows}
        return grid, [gate]

    def _on_line_side(self, x, y):
        """Is (x, y) in the same space as the formation line: not on the far side of a wall?

        A spot must see the nearest point of HOLD_LINE. This is what keeps the bottom arm's
        neighbours out of the room on the other side of the wall it runs along.
        """
        nearest = min(self.line_fine, key=lambda p: _dist(x, y, p[0], p[1]))
        return line_of_sight(Vec2(x, y), Vec2(nearest[0], nearest[1]))

    # ---- extractor spots -------------------------------------------------------------

    def _mining_spots(self):
        """Spots that can mine our deposit, safest first: the ones furthest from every gate."""
        conf = self.conf
        dvec = Vec2(self.dep_x, self.dep_y)
        r_min = conf.deposit.radius + conf.bot.radius + 0.35
        found = []
        for idx, (x, y) in enumerate(self.grid):
            d = _dist(x, y, self.dep_x, self.dep_y)
            if d < r_min or d > MINER_MAX_DIST:
                continue
            if not line_of_sight(Vec2(x, y), dvec):
                continue
            safety = min((_dist(x, y, g.cx, g.cy) for g in self.home_gates), default=d)
            found.append((-safety, d, idx))
        found.sort()
        return [idx for _, _, idx in found]

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
        # Only enemy shooters are a danger to stand near: extractors and healers cannot hurt us.
        self._threat_pts = [
            (e.id, e.pos.x, e.pos.y) for e in enemies if e.class_ == BotClass.Battle
        ]
        self._clear_cache = {}

        self._sync_roles(me, tick)
        self._update_mode(state, tick)
        holding = self.mode == "home"

        action = FleetAction.new()
        self._decide_build(state, conf, action)
        if self.mode != "push":
            self._plan_defense(me, tick)
        self._plan_miners(me, tick)

        # 1. where does each bot want to go
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
        if holding:
            # Nobody crosses the line until the march begins.
            for bid, bot in me.items():
                fx, fy = final[bid]
                if bot.pos.y + fy * speed < self.y_min:
                    fy = min(1.0, (self.y_min - bot.pos.y) / speed)
                    final[bid] = (fx, fy)

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
                ba.turn_action = turn_towards(state.deposit_me.pos)
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
        self.hist.pop(bid, None)
        self.escape.pop(bid, None)
        self.recovering.discard(bid)
        self.moved_at.pop(bid, None)
        self.miner_book.drop(bid)
        self.ring_of.pop(bid, None)
        self._release(bid)

    def _release(self, bid):
        """Give up a defender's gate and standing spot."""
        self.gate_of.pop(bid, None)
        idx = self.spot_of.pop(bid, None)
        if idx is not None and self.taken.get(idx) == bid:
            del self.taken[idx]

    def _assign_spot(self, bid, idx):
        old = self.spot_of.get(bid)
        if old is not None and self.taken.get(old) == bid:
            del self.taken[old]
        self.spot_of[bid] = idx
        self.taken[idx] = bid

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
                    role = ROLE_DEFENDER
            self.role[bid] = role
            self.cls[bid] = bot.class_
        self.pending = None

    def _update_mode(self, state, tick):
        """home -> push (the march starts) <-> hold (the payload is where we want it).

        Pushing stops a little before the hold point (the team needs a moment to walk out of
        the payload's capture radius, and it keeps rolling meanwhile), and starts again if the
        payload is knocked back.
        """
        if self.mode == "home":
            if tick < self.push_start:
                return
            self.mode = "push"
            self._use_front(self.push_gates, self.push_grid)
            for bid in list(self.role):
                self._release(bid)
            self.ring_of = {}
            print(f"[plan] tick {tick}: the march begins -- escorting the payload to the chokes")

        if not self.push_gates:
            return   # no chokes to hold: keep escorting
        remaining = (self.hold_capture - state.capture) * self.path_len
        if self.mode == "push" and remaining <= PUSH_LEAD_ARC:
            self.mode = "hold"
            print(f"[plan] tick {tick}: payload at the chokes (capture {state.capture:.3f}) -- holding")
        elif self.mode == "hold" and remaining >= REPUSH_ARC:
            self.mode = "push"
            print(f"[plan] tick {tick}: payload knocked back (capture {state.capture:.3f}) -- pushing")

    # ---------------------------------------------------------------------------------
    # the fabricator
    # ---------------------------------------------------------------------------------

    def _pool_class(self):
        """Class for a bot joining the defence (and later the payload team).

        Keeps the pool at BASE_SHOOTERS_PER_HEALER shooters per healer by looking at who is
        actually alive, not at a running count, so it stays right after deaths and no matter
        which kind of build produced the bots so far. A healer is due once there are enough
        shooters to have earned another one.
        """
        shooters = healers = 0
        for bid, role in self.role.items():
            if role != ROLE_DEFENDER:
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

        Yes while there are fewer than N_MINERS, but fighters come first: not until the defence
        has FIGHTER_TARGET shooters and healers (unless there are fewer than EXTRACTOR_FLOOR
        extractors, when there would be no income to build fighters with). And not if a
        replacement could not arrive and mine for EXTRACTOR_MIN_MINE_TICKS before the extractors
        are swapped out for fighters (see SWAP_EXTRACTORS): a bot built too late to earn anything
        is just 50 tokens not spent on a fighter.
        """
        if not REPLACE_EXTRACTORS:
            return False
        miners = sum(1 for role in self.role.values() if role == ROLE_MINER)
        if miners >= N_MINERS:
            return False
        fighters = sum(1 for role in self.role.values() if role == ROLE_DEFENDER)
        if fighters < FIGHTER_TARGET and miners >= EXTRACTOR_FLOOR:
            return False
        cutoff = self.push_start   # after this the extractors are swapped out / left behind
        return state.tick + self.dep_walk_ticks + EXTRACTOR_MIN_MINE_TICKS <= cutoff

    def _decide_build(self, state, conf, action):
        tick = state.tick
        fab = state.fabricator_me
        action.fabricator_next = int(BotClass.Battle)
        action.rush_order = False

        # Nothing is built in the endgame, and a full fleet ignores builds.
        if tick >= self.endgame_start or state.fleet_me.is_full():
            return

        natural_due = fab.next_bot_creation <= tick
        can_rush = fab.tokens >= conf.fabricator.rush_cost
        if not natural_due and not can_rush:
            return

        # A tick where the free build is due never also rushes, so the class we set is
        # unambiguously the free bot's; otherwise the free build would just slide a tick.
        if self.built < len(OPENING):
            cls = OPENING[self.built]
            role = ROLE_MINER if cls == BotClass.Extractor else ROLE_DEFENDER
            self.built += 1
        elif self._want_extractor(state, conf):
            # Income comes first: with no extractors nothing is ever earned to build with, so a
            # lost extractor takes the next build, free or paid.
            cls = BotClass.Extractor
            role = ROLE_MINER
        else:
            cls = self._pool_class()
            role = ROLE_DEFENDER

        action.fabricator_next = int(cls)
        action.rush_order = not natural_due
        self.pending = (role, cls)

    def _plan_swap(self, state, conf, me):
        """Ids of extractors to self-destruct this tick so their slots can be refilled."""
        if not SWAP_EXTRACTORS:
            return set()
        tick = state.tick
        last_build = self.endgame_start - 1   # last tick a rush is honoured
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
        if tick < last_build - SWAP_LEAD_TICKS - k - SWAP_MARGIN:
            return set()

        miners.sort(key=lambda bid: (me[bid].health, bid))
        chosen = set(miners[:k])
        self.swap_done = True
        print(
            f"[plan] tick {tick}: self-destructing {k} of {len(miners)} extractors "
            f"({fab.tokens:.0f} tokens banked) to refill the slots with fighters"
        )
        return chosen

    # ---------------------------------------------------------------------------------
    # holding the gates
    # ---------------------------------------------------------------------------------

    def _apportion(self, n, weights):
        """Split n men between the gates in proportion to their weights."""
        total = sum(weights)
        if n <= 0 or total <= 0.0:
            return [0] * len(weights)
        shares = [n * w / total for w in weights]
        counts = [int(s) for s in shares]
        left = n - sum(counts)
        for i in sorted(range(len(weights)), key=lambda i: shares[i] - counts[i], reverse=True)[:left]:
            counts[i] += 1
        return counts

    def _update_pressure(self):
        """Weigh each gate by the enemy shooters that would reach it first."""
        n_g = len(self.gates)
        raw = [0.0] * n_g
        for _, ex, ey in self._threat_pts:
            best = None
            for gi, g in enumerate(self.gates):
                d = path_length(Vec2(ex, ey), Vec2(g.cx, g.cy))
                if d is None:
                    d = 1.4 * _dist(ex, ey, g.cx, g.cy)
                if best is None or d < best[0]:
                    best = (d, gi)
            if best is not None and best[0] <= PRESSURE_RANGE:
                raw[best[1]] += 1.0 + (PRESSURE_RANGE - best[0]) / PRESSURE_RANGE
        for gi in range(n_g):
            self.pressure[gi] = 0.8 * self.pressure[gi] + 0.2 * raw[gi]
            self.weights[gi] = PRIOR_WEIGHT * self.gates[gi].prior + self.pressure[gi]

    def _busy(self):
        """Standing spots that are not free: defenders' spots, plus (while the defenders are on
        the home grid) the extractors', which use the same grid."""
        busy = set(self.taken)
        if self.fgrid is self.grid:
            busy |= set(self.miner_book.of.values())
        return busy

    def _take_spot(self, bid, gi):
        """Give a defender the best free standing spot at gate gi (or, failing that, anywhere)."""
        busy = self._busy()
        for idx in self.gates[gi].order:
            if idx not in busy:
                self._assign_spot(bid, idx)
                return idx
        for other in sorted(range(len(self.gates)), key=lambda i: -self.weights[i]):
            for idx in self.gates[other].order:
                if idx not in busy:
                    self._assign_spot(bid, idx)
                    return idx
        # Out of free spots. Share one, but the least crowded, so leftovers spread out instead of
        # all piling onto the single best spot (which is what stacked every late healer).
        order = self.gates[gi].order
        if order:
            crowd = {}
            for i in self.spot_of.values():
                crowd[i] = crowd.get(i, 0) + 1
            idx = min(order, key=lambda i: crowd.get(i, 0))
            self._assign_spot(bid, idx)
            return idx
        return None

    def _take_support_spot(self, bid, gi):
        """A healer's spot at gate gi: behind the shooters, hidden from the approach, and
        within heal range of them."""
        g = self.gates[gi]
        mates = [
            self.fgrid[self.spot_of[b]] for b, gj in self.gate_of.items()
            if gj == gi and b != bid and self.cls.get(b) == BotClass.Battle and b in self.spot_of
        ]
        if not mates:
            return self._take_spot(bid, gi)
        mx = sum(p[0] for p in mates) / len(mates)
        my = sum(p[1] for p in mates) / len(mates)
        mean_d = sum(_dist(p[0], p[1], g.cx, g.cy) for p in mates) / len(mates)
        reach = self.conf.bot.base_heal_range - 0.4
        busy = self._busy()
        for level in (0, 1, 2):
            best = None
            for idx in g.order:
                if idx in busy:
                    continue
                x, y = self.fgrid[idx]
                if level == 0 and g.info[idx][1] > 0:
                    continue                                   # seen from the approach
                if level <= 1 and _dist(x, y, g.cx, g.cy) < mean_d:
                    continue                                   # not behind the shooters
                d = _dist(x, y, mx, my)
                if level <= 1 and d > reach:
                    continue
                if best is None or d < best[0]:
                    best = (d, idx)
            if best is not None:
                self._assign_spot(bid, best[1])
                return best[1]
        return self._take_spot(bid, gi)

    def _plan_defense(self, me, tick):
        """Decide which gate each defender holds and where it stands."""
        n_g = len(self.gates)
        defs = sorted(bid for bid, role in self.role.items() if role == ROLE_DEFENDER and bid in me)
        for bid in list(self.gate_of):
            if bid not in defs:
                self._release(bid)
        if not defs or n_g == 0:
            return

        if tick - self.last_pressure >= PRESSURE_TICKS:
            self._update_pressure()
            self.last_pressure = tick

        shooters = [b for b in defs if self.cls[b] == BotClass.Battle]
        healers = [b for b in defs if self.cls[b] == BotClass.Healer]
        want = self._apportion(len(shooters), self.weights)

        cur = [[] for _ in range(n_g)]
        pool = []
        for b in shooters:
            gi = self.gate_of.get(b)
            if gi is None:
                pool.append(b)
            else:
                cur[gi].append(b)
        deficit = [want[i] - len(cur[i]) for i in range(n_g)]

        # Move men between gates only for a real imbalance, and not too often, so the enemy
        # feinting between two gates does not have us running back and forth.
        moved = False
        if tick - self.last_rebalance >= REBALANCE_TICKS and max(deficit) >= REBALANCE_SLACK:
            for gi in sorted(range(n_g), key=lambda i: deficit[i]):
                if deficit[gi] >= 0:
                    break
                target = max(range(n_g), key=lambda i: deficit[i])
                if deficit[target] <= 0:
                    break
                k = min(-deficit[gi], deficit[target])
                tg = self.gates[target]
                nearest = sorted(
                    cur[gi], key=lambda b: _dist(me[b].pos.x, me[b].pos.y, tg.cx, tg.cy)
                )
                for b in nearest[:k]:
                    cur[gi].remove(b)
                    pool.append(b)
                    self._release(b)
                    deficit[gi] += 1
                    deficit[target] -= 1
                    moved = True
            if moved:
                self.last_rebalance = tick
                print(
                    f"[plan] tick {tick}: rebalancing the gates to "
                    f"{want} (weights {[round(w, 2) for w in self.weights]})"
                )

        # Place everyone without a gate: where the shortfall is biggest first, nearest breaking
        # ties, or failing any shortfall at the heaviest gate.
        deficit = [want[i] - len(cur[i]) for i in range(n_g)]
        for b in sorted(pool):
            bx, by = me[b].pos.x, me[b].pos.y
            open_gates = [i for i in range(n_g) if deficit[i] > 0]
            if open_gates:
                gi = min(open_gates, key=lambda i: (
                    -deficit[i], _dist(bx, by, self.gates[i].cx, self.gates[i].cy)))
            else:
                gi = max(range(n_g), key=lambda i: self.weights[i])
            cur[gi].append(b)
            self.gate_of[b] = gi
            deficit[gi] -= 1

        for b in shooters:
            if b not in self.spot_of:
                self._take_spot(b, self.gate_of[b])

        # Healers go where the shooters are, behind them.
        for h in healers:
            gi = self.gate_of.get(h)
            if gi is None or moved or not cur[gi] or h not in self.spot_of:
                gi = max(range(n_g), key=lambda i: (len(cur[i]), self.weights[i]))
                self._release(h)
                self.gate_of[h] = gi
                self._take_support_spot(h, gi)

        self._rotate_wounded(me, tick)

    def _recovery_spot(self, g, bid, healer_spots, busy):
        """Where a wounded shooter falls back to: a free spot within heal range of a healer,
        further back than where it stands, preferring one the approach cannot see."""
        reach = self.conf.bot.base_heal_range - 0.5
        here = g.rank.get(self.spot_of[bid], 0)
        best = None
        for idx in g.order:
            if idx in busy or g.rank.get(idx, 10 ** 9) <= here:
                continue
            x, y = self.fgrid[idx]
            d = min(_dist(x, y, hx, hy) for hx, hy in healer_spots)
            if d > reach:
                continue
            key = (g.info[idx][1], d, g.rank[idx])
            if best is None or key < best[0]:
                best = (key, idx)
        return best[1] if best is not None else None

    def _rotate_wounded(self, me, tick):
        """Trade wounded shooters for healthy ones so the line stays full and everyone lives longer.

        1. A shooter under RETREAT_FRAC of full health drops back next to a healer.
        2. The freed front spot is taken by the rear-most healthy shooter, and a shooter that has
           healed to RECOVER_FRAC steps up into the best free spot again. Both are the same rule:
           a healthy shooter moves into a free spot that is ROTATE_GAP places better than its own.
        """
        full = self.conf.bot.health
        busy = self._busy()
        for gi, g in enumerate(self.gates):
            shooters = [
                b for b, gj in self.gate_of.items()
                if gj == gi and b in me and b in self.spot_of and self.cls.get(b) == BotClass.Battle
            ]
            healer_spots = [
                self.fgrid[self.spot_of[b]] for b, gj in self.gate_of.items()
                if gj == gi and b in self.spot_of and self.cls.get(b) == BotClass.Healer
            ]

            for b in shooters:
                if b in self.recovering and me[b].health >= RECOVER_FRAC * full:
                    self.recovering.discard(b)

            # 1. the wounded fall back (at most one per gate per tick, worst first)
            if healer_spots:
                for b in sorted(shooters, key=lambda b: me[b].health):
                    if me[b].health >= RETREAT_FRAC * full:
                        break
                    if b in self.recovering or tick - self.moved_at.get(b, -10 ** 9) < ROTATE_COOLDOWN:
                        continue
                    spot = self._recovery_spot(g, b, healer_spots, busy)
                    if spot is None:
                        continue
                    busy.discard(self.spot_of[b])
                    busy.add(spot)
                    self._assign_spot(b, spot)
                    self.recovering.add(b)
                    self.moved_at[b] = tick
                    break

            # 2. the healthy step up into the best free spot (one per gate per tick)
            free = next((idx for idx in g.order if idx not in busy), None)
            if free is None:
                continue
            ready = [
                b for b in shooters
                if b not in self.recovering and me[b].health >= RETREAT_FRAC * full
                and tick - self.moved_at.get(b, -10 ** 9) >= ROTATE_COOLDOWN
            ]
            if not ready:
                continue
            rear = max(ready, key=lambda b: g.rank.get(self.spot_of[b], 10 ** 9))
            if g.rank.get(self.spot_of[rear], 10 ** 9) >= g.rank.get(free, 0) + ROTATE_GAP:
                busy.discard(self.spot_of[rear])
                busy.add(free)
                self._assign_spot(rear, free)
                self.moved_at[rear] = tick

    def _plan_miners(self, me, tick):
        """Move extractors away from enemy shooters, as far as mining allows.

        Every mining spot in the pool can mine (a clear line to the deposit, within range), so the
        only question is which is furthest from the nearest enemy shooter. Each extractor,
        most endangered first, takes a free spot that beats its own by MINER_MOVE_GAIN or more.
        """
        if not self._threat_pts or tick - self.last_miner_plan < MINER_REPLAN_TICKS:
            return
        self.last_miner_plan = tick

        miners = [b for b in sorted(self.role) if self.role[b] == ROLE_MINER
                  and b in me and b in self.miner_book.of]
        if not miners:
            return

        safety = {}

        def safe(idx):
            s = safety.get(idx)
            if s is None:
                x, y = self.grid[idx]
                s = min(MINER_SAFE_CAP, min(_dist(x, y, ex, ey) for _, ex, ey in self._threat_pts))
                safety[idx] = s
            return s

        used = set(self.miner_book.of.values())
        if self.fgrid is self.grid:
            used |= set(self.taken)      # defenders are on the home grid too: keep off their spots
        for _, b in sorted((safe(self.miner_book.of[b]), b) for b in miners):
            old = self.miner_book.of[b]
            best = None
            for idx in self.mine_order:
                if idx in used:
                    continue
                s = safe(idx)
                if s >= safe(old) + MINER_MOVE_GAIN and (best is None or s > best[0]):
                    best = (s, idx)
            if best is not None:
                used.discard(old)
                used.add(best[1])
                self.miner_book.of[b] = best[1]

    def _hold_flank(self, bid, bot, idx):
        """A defender settled on its spot that cannot hit anything it can see (a deposit or the
        payload is in the way) shifts to another spot at its gate that can."""
        x, y = self.fgrid[idx]
        if _dist(bot.pos.x, bot.pos.y, x, y) > 0.35:
            return idx
        seen = self._flank_wanted(bot)
        if not seen:
            return idx
        gi = self.gate_of.get(bid)
        if gi is None:
            return idx
        cands = [
            (i, self.fgrid[i][0], self.fgrid[i][1], 0) for i in self.gates[gi].order[:FLANK_POOL]
        ]
        new = self._pick_flank(bot, cands, self._busy(), seen)
        if new is None:
            return idx
        self._assign_spot(bid, new)
        return new

    # ---------------------------------------------------------------------------------
    # where each role stands
    # ---------------------------------------------------------------------------------

    def _target(self, bid, bot, state):
        role = self.role[bid]
        if role == ROLE_MINER:
            idx = self.miner_book.assign(bid, self.mine_order)
            return self.grid[idx] if idx is not None else None
        # Defenders: pushing the payload, or standing at their gate.
        if self.mode == "push":
            return self._payload_target(bid, bot, state)
        idx = self.spot_of.get(bid)
        if idx is None:
            return None
        if bot.class_ == BotClass.Battle and bid not in self.recovering:
            idx = self._hold_flank(bid, bot, idx)     # (a wounded bot stays with its healer)
        return self.fgrid[idx]

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
            # Never escort from the enemy's side of the doorways.
            if self.mode != "home" and not self._on_our_side(x, y, 0.5):
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
        # is not an obstacle problem, and chasing it is not what a defender does.
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

    def _exposure(self, x, y):
        """How near the front a spot is: distance to the nearest enemy shooter (smaller = more
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

        Returns `target` unchanged unless an enemy shooter is close and that target would put
        the bot closer to it than our frontmost nearby shooter, in which case it returns a spot
        just behind that shooter. It is a pure function of where everyone stands, so a bot that
        has tucked in stays tucked in instead of oscillating back to its old spot.
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
        # While holding at the doorways the formation is deliberately packed (HOLD_SPACING), so the
        # keep-apart distances shrink to match; everywhere else the full splash spacing applies.
        if self.mode == "hold":
            hard, rep_start = HOLD_HARD, HOLD_REP
        else:
            hard, rep_start = self.hard, self.rep_start

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
                if d >= rep_start:
                    continue
                if tight[i]:
                    ahead = des[i][0] * (q[j][0] - q[i][0]) + des[i][1] * (q[j][1] - q[i][1])
                    if ahead > 0.0:
                        slow = min(slow, max(0.0, (d - hard) / (rep_start - hard)))
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
                s = min(1.0, (rep_start - d) / (rep_start - hard)) * 1.5
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
            # Nobody to shoot: pre-aim at the nearest enemy shooter (else any enemy, else the
            # payload).
            self.aim.pop(bid, None)
            ang = None
            pool = [(ex, ey) for _, ex, ey in self._threat_pts] or [
                (ex, ey) for _, ex, ey in self._enemy_pts
            ]
            if pool:
                fx, fy = min(pool, key=lambda p: _dist(nx, ny, p[0], p[1]))
                ang = _angle_deg(nx, ny, fx, fy)
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
