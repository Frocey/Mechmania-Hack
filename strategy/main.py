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
#   Always: standing spots are on a tight lattice so the group is a compact blob, moving bots pass
#   through each other, and settled bots are never left stacked (see "spacing" below).
# =====================================================================================

N_MINERS = 8
N_OPENING_SHOOTERS = 6
N_OPENING_HEALERS = 3
OPENING = (
    [BotClass.Extractor] * N_MINERS
    + [BotClass.Battle] * N_OPENING_SHOOTERS
    + [BotClass.Healer] * N_OPENING_HEALERS
)

# The defence (and later the payload team) gets one healer for every BASE_SHOOTERS_PER_HEALER
# shooters, but never more than HEALER_CAP healers: below the cap a lost healer is replaced (as
# soon as there are shooters enough), at the cap everything built is a shooter. (The 17-bot
# opening is 6 shooters and 3 healers.)
BASE_SHOOTERS_PER_HEALER = 2
HEALER_CAP = 5

# Our half is y >= Y_LINE (in our own frame; the engine mirrors the world for the other side).
# Bots stay Y_MARGIN below it until the endgame.
Y_LINE = 24.0
Y_MARGIN = 0.35

# --- spacing -------------------------------------------------------------------------
# A shot detonates on the first enemy it meets and hurts every bot whose centre is within
# `base_blaster_splash_radius + bot.radius` of that impact point, so a bot standing in a tight
# clump shares every hit. Bots do not collide, though, so there is no reason to keep them apart
# while they MOVE: passing through each other costs nothing, and pushing moving bots apart is what
# used to block retreats and bend paths. So:
#   * the shooters are one body and the healers another: each is a compact blob, its bots body to
#     body (a bot is 0.5 across, so SPOT_SPACING = 0.55 is touching, not stacked), and a shot that
#     splashes 3-5 of them is a fair price for all of them being able to hit the same target;
#   * a moving bot is never pushed or slowed by anyone; only bots that have SETTLED are nudged
#     apart, and only when they sit closer than SETTLE_APART (two given the same spot, or landing
#     on the same point), so nobody ends up stacked on top of another.
SPOT_SPACING = 0.55
SETTLE_APART = 0.3
MOVING = 0.3                # a bot asking to move at least this fast (0..1) counts as moving

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
#
# The payload counts most of all: it is what wins or loses the game, and the enemy's shooters
# roam. While the payload is on our side of centre (capture below PAYLOAD_THREAT_C), the first of
# our gates that its path to our base comes within PAYLOAD_GATE_R of gets PAYLOAD_GATE_WEIGHT on
# top, enough to outweigh a scattered enemy, so the army gathers where the payload is going and new
# shooters join it there instead of standing guard where nothing is coming.
PAYLOAD_GATE_WEIGHT = 12.0
PAYLOAD_GATE_R = 6.0
PAYLOAD_THREAT_C = 0.05
# And once the enemy has pushed it deep into our half (capture at or below -PAYLOAD_EMERGENCY_C,
# until it is back above -PAYLOAD_EMERGENCY_EXIT) it is an emergency: the gate the payload is
# heading for (or, if it has passed them all, the one nearest it) takes PAYLOAD_EMERGENCY_WEIGHT,
# which nothing else can outweigh, and the whole army goes there at once (no waiting for the
# next rebalance). Stopping the payload comes before anything else.
PAYLOAD_EMERGENCY_C = 0.35
PAYLOAD_EMERGENCY_EXIT = 0.25
PAYLOAD_EMERGENCY_WEIGHT = 1000.0
PRESSURE_TICKS = 15
PRESSURE_RANGE = 90.0
PRIOR_PRIMARY = 0.5
PRIOR_WEIGHT = 1.0
REBALANCE_TICKS = 45
REBALANCE_SLACK = 2
FLANK_POOL = 40             # how many of a gate's best spots a defender may shift between

# --- combat: a cohesive group that re-lays itself out around the enemy ---------------------------
# At every gate the shooters do not just fill a fixed list of spots. Every RELAYOUT_TICKS while an
# enemy shooter is within DYN_RANGE of the gate (and whenever someone is hurt, healed, lost or
# added), the spots are re-scored against where the enemy actually is, and the group is laid out
# again: the healthy shooters take the best "front" spots (ones that can see the enemy's exit
# and are not jammed against a wall), a bot that keeps its spot if it is still a front spot, and
# anyone else moving to the nearest free one. Wounded shooters do not leave the group: they drop
# to a reserve spot behind the front, next to a healer, so the enemy's fire lands on the healthy
# ones and the group as a whole keeps working.
#
# The front spots are chosen to put as many shooters as possible on the same targets at the same
# time: spots are picked one at a time, each for the not-yet-covered targets (the gate's exit points
# and the enemy shooters in the open) it can hit, so the group ends up as whatever shape covers them
# best rather than a line. A shooter's current spot gets STICKY_BONUS, so the shape shifts a step at
# a time as the enemy moves instead of jumping. And the group advances or retreats with the odds:
# with more healthy shooters than enemy shooters near the gate, spots closer to the gate score
# better; with fewer, spots further back and out of sight do (see `advance` in Plan._rescore).
#
# Peeking. While an enemy shooter is close (within blaster range + PEEK_MARGIN of the gate), a
# shooter only stands on its firing spot when its blaster is ready. Each healthy shooter on a front
# spot is given a COVER spot within COVER_MAX of it that the enemy cannot see. After a shot it
# retreats to cover for the reload and steps out again once the remaining cooldown is no more than
# the walk back plus PEEK_LEAD ticks, so it arrives ready to fire. PEEK_HYST stops it flapping at
# the edge. It also waits (up to SYNC_MAX_WAIT ticks) until SYNC_FRAC of its gate's shooters are
# ready, so they step out together: one big volley per peek, not a trickle of single shots.
PEEK_MARGIN = 8.0
PEEK_LEAD = 6
PEEK_HYST = 12
SYNC_FRAC = 0.6
SYNC_MAX_WAIT = 40
COVER_MAX = 2.5
# The cover-spot cycle above has been replaced by the edge peek below; set True to bring it back.
COVER_PEEK = False

# --- the edge peek (what "jiggle peeking" can really be in this engine) --------------------------
# Every tick each bot decides from the state at the start of the tick, then everyone moves (0.05),
# then the shots resolve. A shot hits any bot whose 0.25-radius hull crosses the ray, so ducking back
# one step after being seen does not dodge it: to be safe a bot must be about PEEK_HULL behind the
# edge of a sightline, several ticks of walking. What does work is timing: the state shows when every
# enemy shooter can fire again (`next_fire_tick`), and one that has just fired cannot for 60 ticks.
#
# So each shooter that is engaged (an enemy within blaster range + PEEK_ENGAGE of its spot) keeps
# a pair of points near its spot: a HIDE point where its whole body is out of every nearby enemy
# shooter's sight, and an OUT point, just across the edge, from which it can hit one. It waits at
# HIDE, pre-aimed, and walks to OUT so as to arrive as its blaster comes ready (PEEK_LEAD ticks of
# slack), but only if the peek is safe: no enemy shooter that could answer it (one that is ready
# within PEEK_RESPONSE ticks and can see OUT) or, if some could, at least PEEK_VOLLEY_RATIO times as
# many of ours are ready (within PEEK_READY_WIN) to trade shots with them. After it fires its
# cooldown is 60, so it walks straight back to HIDE. Bots pass through each other freely, so
# there is no fixed formation while this is going on: each is just moving in and out at its edge.
# The points are searched within PEEK_TETHER of the shooter's spot and refreshed every
# PEEK_SCAN_TICKS ticks or when its spot moves.
PEEK_ENGAGE = 3.0
PEEK_TETHER = 1.8
PEEK_HULL = 0.32
PEEK_RESPONSE = 6
PEEK_VOLLEY_RATIO = 2.0
PEEK_READY_WIN = 10
PEEK_SCAN_TICKS = 12
PEEK_GUARDS = 3
# Stall-breaker: if no blaster has fired anywhere (either side) for STALL_TICKS, the safe-peek rule
# relaxes: our shooters step out as a volley as soon as as many are ready as there are enemies
# that could answer, so a standoff cannot go on for ever.
STALL_TICKS = 300
# Never still: a shooter waiting hidden for its blaster shifts between its HIDE point and a second
# hidden point every PEEK_SWAY_TICKS (offset by its id, so they are not all in step); and while
# nothing is in range the whole blob drifts in a slow circle of radius SWAY_R (one common offset,
# so it moves as one body), taking SWAY_PERIOD ticks a lap. (No drift while holding the payload
# doorways: the payload must not be pushed past its hold point.)
PEEK_SWAY_TICKS = 25
SWAY_R = 0.6
SWAY_PERIOD = 240

# --- healers: a hub well behind the front ---------------------------------------------------
# Healers stand in a hub HUB_BEHIND behind the shooters' centre (on the side away from the gate or
# the enemy), out of sight of the enemy, within HUB_RADIUS of that point. Wounded shooters fall
# back all the way to it to be healed, so the enemy's fire never reaches the healers first.
HUB_BEHIND = 4.0
HUB_RADIUS = 2.5

# --- one group, always together -------------------------------------------------------------
# The army is a single blob. It is never split between gates (ALLOW_SPLIT), everyone's target is kept
# within TOGETHER_R of the group's centre (anyone whose is not is sent to the centre), the group
# only walks on when the bots that have reached it are together (see TRANSIT_JOIN_R: bots still on
# their way to join just come at full speed), and in an assault nobody leaves to push: the blob
# stands on the payload and pushes it as one body (whenever no enemy shooter is within PUSH_CLEAR of
# it, the front is simply the spots nearest the payload).
ALLOW_SPLIT = False
TOGETHER_R = 9.0
PUSH_BONUS = 3.0
PUSH_RING = 2.4
RELAYOUT_TICKS = 10
STICKY_BONUS = 1.5
FRONT_DEFAULT = 14           # front size before there is a group to size it by (start-up)
ELIG_MAX = 110               # the greedy pick only looks at this many of the best-scoring spots
# Keeping the shooters one blob: each spot picked earns CLUSTER_W for each already-chosen spot within
# CLUSTER_R of it (up to 3), so the front fills outwards from one place instead of scattering. Only
# spots within ROI_R of the group's focus point are considered, so it moves as one body a step at a
# time; SCAN_STRIDE is how coarsely the whole gate is scanned once, at start-up, to place the focus.
CLUSTER_W = 1.0
CLUSTER_R = 0.8
ROI_R = 5.5
SCAN_STRIDE = 6
DYN_RANGE = 24.0             # enemy shooters this close to a gate count as approaching it
DYN_MAX_ENEMIES = 8
DYN_WEIGHT = 2.0             # an enemy that is out in the open counts for this many kill points
MIN_FRONT_SEEN = 3           # a front spot must be able to hit at least this many kill points
WALL_CLEAR = 0.3             # ... and have this much room beyond the bot's radius (not wall-hugging)
HUG_PENALTY = 2.0
FAR_SPOT = 7.5               # spots further than this from the gate lose score and are not front

# How willing the group is to trade blood: the health fraction below which a shooter falls back,
# the fraction at which it counts as healed and steps up again, how much being seen from the
# enemy's approach costs a spot, and whether enemies still in the approach count as targets to
# see (aggressive) or only those already out of the gate (careful). The last number is a standing
# lean towards advancing (+) or falling back (-), added to the odds-based one (-1..+1 in all).
#                   retreat  recover  lane weight  targets in the approach  advance lean
STANCE_HOME = (0.5, 0.8, LANE_WEIGHT, False, 0.0)
STANCE_HOLD = (0.6, 0.85, LANE_WEIGHT, False, -0.2)    # winning: play it safe
STANCE_DEFEND = (0.3, 0.6, 0.5, True, 0.3)             # losing: take more risks
STANCE_ASSAULT = (0.4, 0.7, 0.5, True, 0.3)            # taking the payload: bold, but not reckless

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
# it (x = 25.6, y from 15 to 16.8). The formation is chosen to hit both, and to put as many
# shooters as possible on the same enemy at the same time; it is not a line.
HOLD_KILL = [((21.6, 13.8), (24.5, 13.8)), ((25.6, 15.0), (25.6, 16.8))]
# HOLD_LINE: the L that was drawn along the west side and the south wall. It no longer decides
# where anyone stands (only a faint tie-break); it decides where the PAYLOAD is left: the
# payload is pushed until it is HOLD_CLEAR from every spot on it.
HOLD_LINE = [(18.4, 14.5), (18.4, 17.5), (24.5, 17.5)]
# The formation may stand anywhere within PUSH_REGION_R of the doorways (on our side of them, and
# outside the payload's capture radius). Its spots are HOLD_SPACING apart, a little inside one
# blaster splash of each other on purpose, so that many bots fit.
PUSH_REGION_R = 10.0
HOLD_SPACING = 0.55
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

# --- the late game: which side of centre is the payload on? -----------------------------
# From the march on, the team's stance follows the payload's capture value (positive = on the
# enemy's side of centre, i.e. we are winning the timeout tiebreak):
#   winning or level: push it to the doorways (above) and HOLD there, careful (STANCE_HOLD);
#   losing (capture at or below -DEFEND_ENTER): go back and DEFEND our own gates, i.e. the
#       chokes between the payload and our base, aggressively (STANCE_DEFEND), and keep doing so
#       until the payload is back at centre (DEFEND_EXIT).
# A losing team does not defend for ever, because sitting still loses on timeout. It goes and
# takes the payload instead (see Plan._should_attack) when any of these holds:
#   * the enemy has no shooters left;
#   * we are stronger overall: our force (shooters + half a point per healer) is at least
#     AGGR_OUTNUMBER times theirs (and, once attacking, stays at least AGGR_KEEP times theirs);
#   * few enemy shooters are anywhere near the payload: we out-number those within NEAR_PAYLOAD_R
#     of it AGGR_NEAR to 1 (they have retreated, so the payload is there for the taking);
#   * time is running out: the ticks left are no more than TIME_FACTOR times what it would take to
#     push the payload back to centre, plus the walk there, plus TIME_SLACK. Then it is attack or
#     lose, whatever the odds.
# A stance is kept for at least STANCE_MIN_TICKS before it can change.
DEFEND_ENTER = 0.04
DEFEND_EXIT = 0.0
AGGR_OUTNUMBER = 1.15
AGGR_KEEP = 0.9
AGGR_NEAR = 2.0
NEAR_PAYLOAD_R = 16.0
TIME_FACTOR = 1.25
TIME_SLACK = 300
STANCE_MIN_TICKS = 120
# Also attack when we are losing and nothing has changed for STANDSTILL_TICKS: a standoff that
# neither side will break is a loss for the side the payload is closer to.
STANDSTILL_TICKS = 900
STANDSTILL_C = 0.003

# --- the assault: taking the payload back as a fighting group --------------------------------
# Attacking is not a different game. The army keeps doing everything it does at a gate (front
# spots that put many shooters on the same enemy, peeking on cooldown, wounded falling back to
# the healers), around a group centre that walks towards the payload:
#   1. it first assembles: the front spots are the ones nearest the centre, and the centre only
#      moves on when TRANSIT_COHESION of the shooters are within TRANSIT_R of it, so the forces
#      from the left and the right join up before anything advances;
#   2. it advances TRANSIT_STEP each layout (every RELAYOUT_TICKS) along the walking route;
#   3. once enemy shooters are almost within range of the group's centre (ASSAULT_CONTACT_R), the
#      usual layout takes over: spots that can hit them, in one blob, peeking and retreating, closer
#      if the odds allow (`advance`), further back if they do not;
#   4. when no enemy shooter is within PUSH_CLEAR of the payload the whole blob keeps walking (step
#      2) right onto the payload and pushes it; the moment one appears it lays out against it.
# ASSAULT_FAR is how far from the payload the formation may stand.
TRANSIT_R = 3.5
TRANSIT_COHESION = 0.7
TRANSIT_STEP = 0.5
# Only the bots that have reached the group count for its cohesion: one further than
# TRANSIT_JOIN_R from the centre is still on its way to join, walks there at full speed and is not
# waited for. While there are any, the centre walks at the slower TRANSIT_STEP_JOIN so they catch up.
TRANSIT_JOIN_R = 9.0
TRANSIT_STEP_JOIN = 0.3
# The group keeps walking (assembled, one blob) until the nearest enemy shooter that is out at the
# payload is within blaster range + ASSAULT_CONTACT_R of its centre; only then does it lay out
# spots against the enemy. Without this the first front spots jump ahead of the group, into range.
ASSAULT_CONTACT_R = 3.0
# In a fight the assault and the defend stance count a spot that a second (third...) enemy shooter
# can also see as worse by this much per extra enemy: the group fights from where few can answer.
EXPOSE_W = 1.2
PUSHERS = 0                 # nobody leaves the group to push (see TOGETHER_R above)
PUSH_CLEAR = 6.0
ASSAULT_FAR = 9.0
# The army is one group. It is split between gates only if a second gate's claim is at least
# SPLIT_FRACTION of the strongest gate's AND each group would have at least MIN_GROUP shooters;
# otherwise everybody goes to the strongest gate. No lone sentries at the other gates.
SPLIT_FRACTION = 0.6
MIN_GROUP = 5
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
        self.focus = (cx, cy)     # where the group is centred; spots are only looked at near it
        self.rank = {}            # grid index -> place in `order` (0 = the best, front-most spot)
        self.info = {}            # grid index -> (exit points seen, lane points seen), static
        self.grid = []            # the standing spots `order` refers to
        self.cands = []           # every grid index that may be used at this gate
        self.hug = {}             # grid index -> True if it is jammed against a wall
        self.hug_excludes = True  # a wall-hugging spot cannot be a front spot (else just a penalty)
        self.vis = {}             # grid index -> which exit points it can hit (indices into `exit`)
        self.bias = {}            # grid index -> fixed score adjustment
        self.solid = None         # (centre, radius) that blocks shots here, e.g. the payload
        self.far = FAR_SPOT       # spots further than this from the gate are not front spots
        self.quality = set()      # the front spots right now (see Plan._rescore)
        self.pull = None          # (x, y): spots within PUSH_RING of it get PUSH_BONUS (assault push)
        self.last_layout = -10 ** 9
        self.sig = None


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
        self.emergency = False  # the payload is deep in our half: every man to the gate it is heading for
        self._last_err = {}     # step name -> tick its last error was logged
        self.ring_of = {}
        self._ring_tick = -1
        self._ring_cache = {}
        self._width_cache = {}
        # "home" (holding the gates) -> "assault" (taking the payload as a fighting group)
        # <-> "hold" (holding the doorways with the payload where we want it); or "defend" (losing:
        # back at our own gates, aggressively).
        self.mode = "home"
        self.gates = []         # the gates of the front in use (home or payload chokes)
        self.fgrid = []         # ... and the standing spots they refer to
        self.swap_done = False
        self.recovering = set()  # shooters that have fallen back to be healed
        self.cover_of = {}       # shooter id -> grid index of its cover spot (hidden, near its own)
        self.peek = {}           # shooter id -> "out" (on its firing spot) or "cover"
        self.cover_since = {}    # shooter id -> tick it went into cover
        self.in_cover = set()    # shooters that are in cover this tick
        self.pk = {}             # shooter id -> its current peek points (see the edge peek above)
        self._prev_nft = {}      # our shooter id -> its blaster's next_fire_tick last tick
        self._prev_enft = {}     # enemy shooter id -> the same
        self._last_shot = 0      # tick a blaster (either side) last fired
        self._sway_on = False    # the blob drifts this tick (nothing in range)
        self._tick_no = 0
        self._einfo = {}         # enemy id -> (predicted x, y, is_shooter, ticks to ready, health, inv)
        self.stance_since = 0    # tick the current late-game stance began
        self.pushers = set()     # shooters pushing the payload (assault stance, no enemy near it)
        self._cap_ref = (0, 0.0)  # (tick, capture) when the payload last moved, for standstills
        self._capture = 0.0
        self._enemy_healers = 0
        self._last_pos = {}
        self._pay = None
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
        self.spacing = SPOT_SPACING                     # lattice spacing of every standing spot
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

        # Front 3: the assault. One "gate" whose centre is wherever the payload is, over a lattice of
        # every standing spot on the map; its spots are picked near the group and against the enemy.
        self.assault_grid, self.assault_gates = self._build_assault_front()

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

    def _build_assault_front(self):
        """The assault's spots and its single gate.

        The gate has no fixed doorway: its centre is set to the payload's position every tick, it
        has no exit points or approach lane of its own (the targets are the enemy shooters near the
        payload and the ring around it, worked out at each layout), and any standing spot on the map
        can be used, so the group can stand wherever the fight is. Only spots within ASSAULT_FAR of
        the payload count as front spots.
        """
        grid = []
        y = 0.5
        while y <= self.map_max:
            x = 0.5
            while x <= self.map_max:
                if self._standable(x, y):
                    grid.append((x, y))
                x += self.spacing
            y += self.spacing
        gate = Gate(16.0, 16.0, [], [])
        gate.prior = 1.0
        gate.grid = grid
        gate.cands = list(range(len(grid)))
        gate.far = ASSAULT_FAR
        return grid, [gate]

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
        # (the wounded stay wounded across a change of front: they keep falling back to be healed)
        self.cover_of = {}
        self.peek = {}
        self.cover_since = {}
        self.in_cover = set()
        self.pk = {}
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
        # The kill zone is the doorway line itself (across the narrowest point) as well as the
        # route just past it: the group should be able to hit an enemy the moment it steps into the
        # doorway, not only after it has walked through.
        exit_pts = self._doorway_line(route, ci) + route[ci:ci + GATE_EXIT]
        gate = Gate(route[ci][0], route[ci][1], route[max(0, ci - GATE_LANE):ci], exit_pts)
        gate.width = wmin
        return gate

    def _doorway_line(self, route, i):
        """Points across the route at sample i, at right angles to it, 0.6 apart, as far to each
        side as a bot can stand: the doorway as a line."""
        px, py = route[i]
        ax, ay = route[max(0, i - 1)]
        bx, by = route[min(len(route) - 1, i + 1)]
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy)
        if n < 1e-9:
            return [(px, py)]
        nx, ny = -dy / n, dx / n
        extent = []
        for sgn in (1.0, -1.0):
            reach = 0.0
            while reach < 6.0 and point_free(
                Vec2(px + sgn * nx * (reach + 0.25), py + sgn * ny * (reach + 0.25))
            ):
                reach += 0.25
            extent.append(reach)
        out = []
        t = -extent[1]
        while t <= extent[0] + 1e-9:
            out.append((px + nx * t, py + ny * t))
            t += 0.6
        return out or [(px, py)]

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
        g.grid = grid
        g.solid = solid
        g.cands = [
            idx for idx, (x, y) in enumerate(grid)
            if idx not in exclude and GATE_SPOT_MIN <= _dist(x, y, g.cx, g.cy) <= GATE_SPOT_MAX
        ]
        g.info, g.hug, g.vis = {}, {}, {}
        self._init_focus(g)
        self._rescore(g, [], LANE_WEIGHT)

    def _static(self, g, idx):
        """The map-only facts about one spot at a gate: which exit points it can hit, how much of
        the enemy's approach can see it, and whether it is jammed against a wall.

        Worked out the first time a spot is looked at and kept, so only the spots near the group
        ever cost anything (the lattice is dense, and most of it is never near the action).
        """
        if idx in g.info:
            return
        rng = self.conf.bot.blaster_range
        x, y = g.grid[idx]
        here = Vec2(x, y)
        seen_pts = tuple(
            k for k, (ex, ey) in enumerate(g.exit)
            if _dist(x, y, ex, ey) <= rng - 1.0 and self._sees(here, ex, ey, g.solid)
        )
        g.vis[idx] = seen_pts
        g.info[idx] = (len(seen_pts), sum(1 for lx, ly in g.lane if self._sees(here, lx, ly, g.solid)))
        # Jammed against a wall means a bad angle on anything coming round the corner.
        g.hug[idx] = not disc_free(here, self.conf.bot.radius + WALL_CLEAR)

    def _init_focus(self, g):
        """Where the group starts out: the best spot in a coarse scan of the gate's spots. The
        group then drifts from there as `_rescore` re-centres it on where it actually stands."""
        best = None
        for idx in g.cands[::SCAN_STRIDE]:
            self._static(g, idx)
            if g.hug[idx] and g.hug_excludes:
                continue
            x, y = g.grid[idx]
            d = _dist(x, y, g.cx, g.cy)
            score = g.info[idx][0] - LANE_WEIGHT * g.info[idx][1] + g.bias.get(idx, 0.0)
            if d > g.far:
                score -= d - g.far
            if best is None or score > best[0]:
                best = (score, x, y)
        g.focus = (best[1], best[2]) if best is not None else (g.cx, g.cy)

    def _sees(self, here, tx, ty, solid):
        """Could a shot from `here` reach (tx, ty): no wall in the way, and not through `solid`."""
        target = Vec2(tx, ty)
        if not line_of_sight(here, target):
            return False
        if solid is not None and point_seg_dist(solid[0], here, target) < solid[1]:
            return False
        return True

    def _rescore(self, g, targets, lane_w, n_front=None, sticky=None, advance=0.0, avoid=None):
        """Choose the front spots for a gate, and rank all its spots, counting the enemy too.

        The targets are the gate's own exit points plus `targets`, (x, y, weight) points for the
        enemy shooters that are out in the open. The front is chosen greedily, one spot at a time:
        each pick is the spot that adds the most target weight, discounted by how many spots
        already cover that target (weight / (1 + covered)). That is what puts as many shooters as
        possible on the same targets at once, and it gives whatever shape does that best, not a line.

        Also counted for each spot: minus `lane_w` per point of the enemy's approach that can see it;
        minus a penalty for hugging a wall or standing far from the gate; STICKY_BONUS if `sticky`
        (a set of spots) contains it, so the shape shifts gradually; and `advance` (-1..+1), which
        prefers spots nearer the gate when positive and further back when negative.

        `n_front` is how many front spots to pick (None: FRONT_DEFAULT); `avoid` is a set of spots
        that are not available. `g.order` comes out as the front spots in the order they were
        picked, then everything else best-first; `g.quality` is the front set.
        """
        rng = self.conf.bot.blaster_range
        n_static = len(g.exit)
        weights = [1.0] * n_static + [w for _, _, w in targets]
        vis, terms, dist, pos, rows, elig = {}, {}, {}, {}, [], []
        fx, fy = g.focus
        # A shot stops on the payload, so where it is now matters for who can hit whom.
        dyn_solid = g.solid
        if dyn_solid is None and self._pay is not None:
            if _dist(self._pay[0].x, self._pay[0].y, g.cx, g.cy) <= 14.0:
                dyn_solid = self._pay
        for idx in g.cands:
            x, y = g.grid[idx]
            # Only the spots around where the group already is are considered: that is what makes
            # it move as one body, a step at a time, and what keeps this cheap on a dense lattice.
            if _dist(x, y, fx, fy) > ROI_R and not (sticky and idx in sticky):
                continue
            self._static(g, idx)
            pos[idx] = (x, y)
            v = list(g.vis.get(idx, ()))
            expo = 0                  # enemy shooters that could hit a bot standing here
            if targets:
                here = Vec2(x, y)
                for j, (tx, ty, tw) in enumerate(targets):
                    if _dist(x, y, tx, ty) <= rng - 1.0 and self._sees(here, tx, ty, dyn_solid):
                        v.append(n_static + j)
                        if tw >= DYN_WEIGHT:
                            expo += 1
            d = _dist(x, y, g.cx, g.cy)
            hug = g.hug.get(idx, False)
            term = -lane_w * g.info[idx][1] + g.bias.get(idx, 0.0) + advance * 0.4 * (5.0 - d)
            if expo > 1 and self.mode in ("assault", "defend"):
                term -= EXPOSE_W * (expo - 1)
            if hug:
                term -= HUG_PENALTY
            if d > g.far:
                term -= d - g.far
            if g.pull is not None and _dist(x, y, g.pull[0], g.pull[1]) <= PUSH_RING:
                term += PUSH_BONUS            # no enemy shooter near: the blob stands on the payload
            vis[idx], terms[idx], dist[idx] = v, term, d
            seen_w = sum(weights[t] for t in v)
            rows.append((seen_w + term, -d, idx))
            if (seen_w >= MIN_FRONT_SEEN and d <= g.far + 1.0
                    and not (hug and g.hug_excludes) and not (avoid and idx in avoid)):
                elig.append((seen_w + term, idx))

        # Only the best few are worth the greedy pass (it is the expensive part).
        elig = [idx for _, idx in sorted(elig, reverse=True)[:ELIG_MAX]]
        n = min(FRONT_DEFAULT if n_front is None else n_front, len(elig))
        chosen = []
        cover = [0.0] * len(weights)
        avail = set(elig)
        neighbours = {idx: 0 for idx in avail}      # chosen spots right next to each candidate
        for _ in range(n):
            best = None
            for idx in avail:
                gain = terms[idx] + sum(weights[t] / (1.0 + cover[t]) for t in vis[idx])
                gain += CLUSTER_W * min(neighbours[idx], 3)      # stay in one tight group
                if sticky and idx in sticky:
                    gain += STICKY_BONUS
                key = (gain, -dist[idx], -idx)
                if best is None or key > best[0]:
                    best = (key, idx)
            idx = best[1]
            chosen.append(idx)
            avail.discard(idx)
            for t in vis[idx]:
                cover[t] += 1.0
            px, py = pos[idx]
            for j in avail:
                if _dist(px, py, pos[j][0], pos[j][1]) <= CLUSTER_R:
                    neighbours[j] += 1

        rows.sort(reverse=True)
        picked = set(chosen)
        g.order = chosen + [r[2] for r in rows if r[2] not in picked]
        g.rank = {idx: k for k, idx in enumerate(g.order)}
        g.quality = picked
        if chosen:
            # the group's centre moves towards where the chosen spots are, not all the way at once
            cx = sum(pos[i][0] for i in chosen) / len(chosen)
            cy = sum(pos[i][1] for i in chosen) / len(chosen)
            g.focus = (0.6 * fx + 0.4 * cx, 0.6 * fy + 0.4 * cy)

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
        """The formation: one Gate whose spots are chosen to cover both doorways.

        Candidates are every standing spot within PUSH_REGION_R of the doorways that is on our side
        of them, outside the payload's capture radius (so nobody in the formation can push the
        payload on), and a short walk from the doorways (which keeps out spots on the far side of
        a wall). There is no line: which of them are used, and where, is decided by `_rescore`
        as the enemy moves, to put as many shooters as possible on the same targets. The payload
        is the one solid that blocks sight and shots, as it will be at its hold point.
        """
        hx, hy = self.hold_pos
        gx = sum(p[0] for p in self.kill_pts) / len(self.kill_pts)
        gy = sum(p[1] for p in self.kill_pts) / len(self.kill_pts)
        mid = self.kill_pts[len(self.kill_pts) // 2]
        grid = []
        y = 0.5
        while y <= self.map_max:
            x = 0.5
            while x <= self.map_max:
                if (_dist(x, y, gx, gy) <= PUSH_REGION_R
                        and _dist(x, y, hx, hy) >= HOLD_CLEAR
                        and self._on_our_side(x, y, 0.3)
                        and self._standable(x, y)
                        and self._connected(x, y, mid[0], mid[1])):
                    grid.append((x, y))
                x += HOLD_SPACING
            y += HOLD_SPACING

        gate = Gate(gx, gy, [], list(self.kill_pts))
        gate.prior = 1.0
        rng = self.conf.bot.blaster_range
        gate.grid = grid
        gate.solid = self.hold_solid
        gate.far = 99.0                # no distance limit: the range check does that job
        gate.hug_excludes = False      # hugging a wall costs a little, but is allowed here
        gate.cands = list(range(len(grid)))
        # the map-only facts (which doorway points a spot can hit) are worked out lazily, by
        # `_static`, for the spots near the group; only the cheap tie-break is set up here
        for idx, (x, y) in enumerate(grid):
            # a faint pull towards the line that was drawn, only to break ties between equals
            gate.bias[idx] = -0.15 * self._poly_dist(x, y, HOLD_LINE)
        self._init_focus(gate)
        self._rescore(gate, [], LANE_WEIGHT)
        return grid, [gate]

    def _connected(self, x, y, tx, ty):
        """Is (x, y) a short walk from (tx, ty), not round a wall? (Walking distance within
        1.6x the straight line, plus a little.)"""
        walk = path_length(Vec2(x, y), Vec2(tx, ty))
        return walk is not None and walk <= 1.6 * _dist(x, y, tx, ty) + 1.5

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
        self._tick_no = tick
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
        self._capture = state.capture
        self._enemy_healers = sum(1 for e in enemies if e.class_ == BotClass.Healer)
        self._last_pos = {bid: (bot.pos.x, bot.pos.y) for bid, bot in me.items()}
        self._pay = (payload, conf.payload.radius)
        # Only enemy shooters are a danger to stand near: extractors and healers cannot hurt us.
        self._threat_pts = [
            (e.id, e.pos.x, e.pos.y) for e in enemies if e.class_ == BotClass.Battle
        ]
        self._clear_cache = {}
        # For every enemy: where it will be after this tick's move, whether it is a shooter, how many
        # ticks until its blaster is ready (0 = ready now), its health, and its invulnerability.
        self._einfo = {}
        for e in enemies:
            shooter = e.class_ == BotClass.Battle
            wait = max(0, e.next_fire_tick - tick) if shooter else 0
            self._einfo[e.id] = (
                e.pos.x + e.vel.x, e.pos.y + e.vel.y, shooter, wait, e.health,
                e.invulnerable_until_tick,
            )

        # A blaster's next_fire_tick changes when it fires: note when anyone (either side) last did.
        for b, bot in me.items():
            if bot.class_ == BotClass.Battle:
                v = bot.next_fire_tick
                if b in self._prev_nft and self._prev_nft[b] != v:
                    self._last_shot = tick
                self._prev_nft[b] = v
        for e in enemies:
            if e.class_ == BotClass.Battle:
                v = e.next_fire_tick
                if e.id in self._prev_enft and self._prev_enft[e.id] != v:
                    self._last_shot = tick
                self._prev_enft[e.id] = v

        self._sync_roles(me, tick)
        self._guard("update_mode", self._update_mode, state, tick)
        # Nothing in range of any gate: the blob is free to drift (see SWAY_R).
        self._sway_on = self.mode in ("home", "defend") and not any(
            _dist(ex, ey, g.cx, g.cy) <= DYN_RANGE for _, ex, ey in self._threat_pts
            for g in self.gates
        )
        holding = self.mode == "home"

        action = FleetAction.new()
        self._decide_build(state, conf, action)
        if self.mode == "assault":
            # the assault's "gate" is wherever the payload is
            self.gates[0].cx, self.gates[0].cy = payload.x, payload.y
        # Each step below is guarded: a bug in one of them costs that step (and is printed to the
        # log, with its traceback), instead of freezing the whole army for the rest of the match.
        self._guard("plan_defense", self._plan_defense, me, tick)
        self._guard("update_pushers", self._update_pushers, me, state, payload)
        self._guard("plan_miners", self._plan_miners, me, tick)
        if COVER_PEEK:
            self._guard("update_peek", self._update_peek, me, tick)
        else:
            self.in_cover = set()

        # 1. where does each bot want to go
        targets = {}
        for bid, bot in me.items():
            targets[bid] = self._guard("target", self._target, bid, bot, state)
        # ...except that healers and extractors never lead: near an enemy they tuck in
        # behind our shooters.
        for bid, bot in me.items():
            if bot.class_ != BotClass.Battle:
                targets[bid] = self._guard(
                    "cover_target", self._cover_target, bot, targets[bid], me, default=targets[bid]
                )

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

        # Engaged shooters peek in and out at the edge of the enemy's sight instead of standing on
        # their spot: this overrides where they were going (and takes them off the stuck watch).
        self._guard("peek_moves", self._peek_moves, me, targets, des, tick)

        # 2. keep out of each other's splash...
        final = self._guard("spread", self._spread, me, des, conf, default=None)
        if final is None:
            final = dict(des)
        # ...unless a bot has stopped making progress, in which case getting it moving again
        # comes first: it steps off along whichever way shortens its route.
        for bid, bot in me.items():
            unstick = self._guard(
                "stuck_check", self._stuck_check, bid, bot, targets[bid], des[bid], tick
            )
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
                self._guard("shoot", self._shoot, bid, bot, nx, ny, enemies, state, conf,
                            payload, claimed, ba)
            else:
                self._guard("heal", self._heal, bid, bot, nx, ny, me, npos, conf, ba)

        return action

    def _guard(self, name, fn, *args, default=None):
        """Run one step of the tick; if it raises, log it (at most every 300 ticks per step, with
        the traceback) and carry on with `default`."""
        try:
            return fn(*args)
        except Exception:
            if self._tick_no - self._last_err.get(name, -10 ** 9) >= 300:
                self._last_err[name] = self._tick_no
                print(f"[plan] tick {self._tick_no}: step '{name}' raised, carrying on without it")
                traceback.print_exc()
            return default

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
        self.cover_of.pop(bid, None)
        self.peek.pop(bid, None)
        self.cover_since.pop(bid, None)
        self.in_cover.discard(bid)
        self.pk.pop(bid, None)
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

        From the march on there is also a fourth stance, "defend": if the payload has ended up on
        our side of centre (we are losing), the team goes back to our own gates, the chokes between
        the payload and our base, and fights there aggressively instead of marching out to be
        picked off one by one. See DEFEND_ENTER and friends at the top of the file.
        """
        c = state.capture
        # has the payload moved lately? (a standoff is a payload that has not); tracked from the start
        if abs(c - self._cap_ref[1]) > STANDSTILL_C:
            self._cap_ref = (tick, c)
        if self.mode == "home" and tick < self.push_start:
            return
        mine = sum(
            1 for b, r in self.role.items()
            if r == ROLE_DEFENDER and self.cls.get(b) == BotClass.Battle
        )
        theirs = len(self._threat_pts)

        losing = c < DEFEND_EXIT if self.mode == "defend" else c <= -DEFEND_ENTER
        if losing:
            want = "assault" if self._should_attack(state, c) else "defend"
        else:
            want = "assault" if self.mode in ("home", "defend") else None

        if self.mode == "home":
            self._enter(want, tick, c, mine, theirs)
        elif want is not None and want != self.mode and tick - self.stance_since >= STANCE_MIN_TICKS:
            self._enter(want, tick, c, mine, theirs)

        # Winning or level: take the payload to the doorways, hold there, and take it again if it
        # is knocked back. (All of it as a fighting group: see the assault comment at the top.)
        if self.mode in ("assault", "hold") and self.push_gates:
            remaining = (self.hold_capture - c) * self.path_len
            if self.mode == "assault" and c >= 0 and remaining <= PUSH_LEAD_ARC:
                self._enter("hold", tick, c, mine, theirs)
            elif self.mode == "hold" and remaining >= REPUSH_ARC:
                self._enter("assault", tick, c, mine, theirs)

    def _start_assault(self):
        """Begin an assault: the group's centre starts where the army is (the average of its bots),
        so it forms up around itself and then walks."""
        g = self.assault_gates[0]
        pts = [self._last_pos[b] for b, r in self.role.items()
               if r == ROLE_DEFENDER and b in self._last_pos]
        if pts:
            g.focus = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        g.order, g.rank, g.quality = [], {}, set()
        g.sig = None
        g.last_layout = -10 ** 9

    def _should_attack(self, state, c):
        """We are losing the payload (it is on our side). Should we go and take it back?

        See the comment on AGGR_OUTNUMBER: yes if the enemy is gone, or we are stronger, or few
        of them are near the payload, or there is no time left to wait.
        """
        conf = self.conf
        ours = {b for b, r in self.role.items() if r == ROLE_DEFENDER and b in self.cls}
        my_force = sum(
            1.0 if self.cls[b] == BotClass.Battle else 0.5 for b in ours
        )
        their_force = len(self._threat_pts) + 0.5 * self._enemy_healers
        if not self._threat_pts:
            return True
        # a standoff: the payload has not moved for a long time and it is on our side
        if state.tick - self._cap_ref[0] >= STANDSTILL_TICKS:
            return True

        # time: can we still afford to wait? (ticks to push the payload back to centre, plus the
        # walk there from where the team is, plus a margin)
        p = state.payload_pos()
        push_ticks = (-c) * self.path_len / conf.payload.speed
        pts = [self._last_pos[b] for b in ours if b in self._last_pos]
        if pts:
            mx = sum(q[0] for q in pts) / len(pts)
            my = sum(q[1] for q in pts) / len(pts)
            walk = path_length(Vec2(mx, my), Vec2(p.x, p.y))
            if walk is None:
                walk = 1.4 * _dist(mx, my, p.x, p.y)
            walk_ticks = walk / conf.bot.speed
        else:
            walk_ticks = 600.0
        ticks_left = conf.max_ticks - state.tick
        if ticks_left <= TIME_FACTOR * (push_ticks + walk_ticks) + TIME_SLACK:
            return True

        # odds: overall, and around the payload
        ratio = AGGR_KEEP if self.mode == "assault" else AGGR_OUTNUMBER
        if my_force >= ratio * their_force:
            return True
        near = sum(1 for _, ex, ey in self._threat_pts if _dist(ex, ey, p.x, p.y) <= NEAR_PAYLOAD_R)
        return my_force >= AGGR_NEAR * max(1, near)

    def _front_of(self, mode):
        """The (gates, standing spots) a stance fights on."""
        if mode in ("home", "defend"):
            return self.home_gates, self.grid
        if mode == "assault":
            return self.assault_gates, self.assault_grid
        return self.push_gates, self.push_grid           # "hold"

    def _enter(self, new, tick, c, mine, theirs):
        """Switch stance, moving the team onto the right front (our gates, the assault's, or the
        payload doorways')."""
        old = self.mode
        self.mode = new
        self.stance_since = tick
        gates, grid = self._front_of(new)
        if gates is not self.gates:
            self._use_front(gates, grid)
            self.ring_of = {}
            self.pushers = set()
            if new == "assault":
                self._start_assault()
        print(
            f"[plan] tick {tick}: {old} -> {new} (capture {c:.3f}, our shooters {mine} "
            f"vs their {theirs})"
        )

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
        if healers < HEALER_CAP and shooters >= BASE_SHOOTERS_PER_HEALER * (healers + 1):
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

    @staticmethod
    def _split(n, weights):
        """n men in proportion to the weights, whole numbers, adding up to n."""
        total = sum(weights)
        if n <= 0 or total <= 0.0:
            return [0] * len(weights)
        shares = [n * w / total for w in weights]
        counts = [int(s) for s in shares]
        left = n - sum(counts)
        for i in sorted(range(len(weights)), key=lambda i: shares[i] - counts[i], reverse=True)[:left]:
            counts[i] += 1
        return counts

    def _apportion(self, n, weights):
        """How many men each gate gets: one group at the strongest gate, unless it is worth two.

        A gate other than the strongest only gets men if its claim is at least SPLIT_FRACTION of
        the strongest's and every group that results has MIN_GROUP or more. Otherwise everybody goes
        to the strongest gate, so there are no lone sentries at the others, and the army fights
        as one body.
        """
        counts = [0] * len(weights)
        if n <= 0 or sum(weights) <= 0.0:
            return counts
        top = max(range(len(weights)), key=lambda i: weights[i])
        if not ALLOW_SPLIT:
            counts[top] = n                    # one group, always: everybody to the strongest gate
            return counts
        chosen = [i for i in range(len(weights)) if weights[i] >= SPLIT_FRACTION * weights[top]]
        while len(chosen) > 1:
            shares = self._split(n, [weights[i] for i in chosen])
            if all(s >= MIN_GROUP for s in shares):
                for i, s in zip(chosen, shares):
                    counts[i] = s
                return counts
            chosen.remove(min(chosen, key=lambda i: weights[i]))
        counts[top] = n
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
        bonus = self._payload_gate_bonus()
        for gi in range(n_g):
            self.pressure[gi] = 0.8 * self.pressure[gi] + 0.2 * raw[gi]
            self.weights[gi] = (
                PRIOR_WEIGHT * self.gates[gi].prior + self.pressure[gi] + bonus[gi]
            )

    def _payload_gate_bonus(self):
        """Extra weight for the gate the payload is heading for.

        Only while we are defending (home or defend stance) and the payload is on our side of
        centre. Follows the payload's path from where it is now towards our base (in our frame,
        negative capture runs that way) and picks the first gate that path passes within
        PAYLOAD_GATE_R of: the next one it has to come through.
        """
        bonus = [0.0] * len(self.gates)
        if self.mode not in ("home", "defend") or self._capture >= PAYLOAD_THREAT_C:
            self.emergency = False
            return bonus
        c = self._capture
        was = self.emergency
        if was:
            self.emergency = c <= -PAYLOAD_EMERGENCY_EXIT
        else:
            self.emergency = c <= -PAYLOAD_EMERGENCY_C
        if self.emergency and not was:
            self.last_rebalance = -10 ** 9          # move everyone now, not at the next rebalance
            print(f"[plan] tick {self._tick_no}: payload emergency (capture {c:.3f})")
        weight = PAYLOAD_EMERGENCY_WEIGHT if self.emergency else PAYLOAD_GATE_WEIGHT
        t = c
        while t >= -1.0:
            p = payload_pos(t)
            for gi, g in enumerate(self.gates):
                if _dist(p.x, p.y, g.cx, g.cy) <= PAYLOAD_GATE_R:
                    bonus[gi] = weight
                    return bonus
            t -= 0.01
        if self.emergency and self.gates:
            # it is already past every gate: the one nearest to it
            p = payload_pos(c)
            near = min(range(len(self.gates)),
                       key=lambda i: _dist(p.x, p.y, self.gates[i].cx, self.gates[i].cy))
            bonus[near] = weight
        return bonus

    def _busy(self):
        """Standing spots that are not free: defenders' spots, plus (while the defenders are on
        the home grid) the extractors', which use the same grid."""
        busy = set(self.taken)
        busy |= set(self.cover_of.values())       # cover spots are reserved too
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
        """A healer's place at gate gi: the hub, HUB_BEHIND behind the shooters' centre.

        "Behind" is straight away from the gate (or, in an assault, the payload). Of the free spots
        within HUB_RADIUS of that point, the least exposed to the enemy shooters in range is taken,
        nearest the point first, so the healers gather in one out-of-sight cluster. If there is no
        such spot, the healer settles for the closest safe place behind the shooters.
        """
        g = self.gates[gi]
        mates = [
            self.fgrid[self.spot_of[b]] for b, gj in self.gate_of.items()
            if gj == gi and b != bid and self.cls.get(b) == BotClass.Battle and b in self.spot_of
        ]
        if not mates:
            return self._take_spot(bid, gi)
        mx = sum(p[0] for p in mates) / len(mates)
        my = sum(p[1] for p in mates) / len(mates)
        ax, ay = mx - g.cx, my - g.cy
        n = math.hypot(ax, ay)
        if n < 1e-6:
            ax, ay, n = 0.0, 1.0, 1.0
        hx, hy = mx + ax / n * HUB_BEHIND, my + ay / n * HUB_BEHIND
        watch = sorted(
            ((ex, ey) for _, ex, ey in self._threat_pts if _dist(ex, ey, g.cx, g.cy) <= DYN_RANGE),
            key=lambda p: _dist(p[0], p[1], g.cx, g.cy),
        )[:8]
        busy = self._busy()
        best = None
        for idx in g.cands:
            x, y = g.grid[idx]
            d = _dist(x, y, hx, hy)
            if d > HUB_RADIUS or idx in busy:
                continue
            self._static(g, idx)
            if g.hug[idx] and g.hug_excludes:
                continue
            exposure = g.info[idx][1] + sum(1 for ex, ey in watch if self._exposed(x, y, ex, ey))
            key = (exposure, d)
            if best is None or key < best[0]:
                best = (key, idx)
        if best is not None:
            self._assign_spot(bid, best[1])
            return best[1]
        return self._take_support_near(bid, gi)

    def _take_support_near(self, bid, gi):
        """The fallback: behind the shooters and within heal range of them."""
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

        self._relayout_all(me, tick)

    def _stance_params(self):
        """(retreat fraction, recover fraction, lane weight, aggressive) for the current stance."""
        if self.mode == "defend":
            return STANCE_DEFEND
        if self.mode == "assault":
            return STANCE_ASSAULT
        if self.mode == "hold":
            return STANCE_HOLD
        return STANCE_HOME

    def _release_spot(self, bid):
        """Give up a standing spot but keep the gate."""
        idx = self.spot_of.pop(bid, None)
        if idx is not None and self.taken.get(idx) == bid:
            del self.taken[idx]

    def _dynamic_targets(self, gi, aggressive):
        """Enemy shooters a gate's spots should be able to see: (x, y, weight) points.

        Careful: only enemies already out of the gate (near the kill zone), the ones a shooter
        must be able to hit the moment they peek. Aggressive: also the ones still in the approach
        within DYN_RANGE/2, so the group starts trading earlier."""
        g = self.gates[gi]
        if self.mode == "assault":
            return self._assault_targets(g)
        out = []
        for _, ex, ey in self._threat_pts:
            nearest = min(range(len(self.gates)),
                          key=lambda j: _dist(ex, ey, self.gates[j].cx, self.gates[j].cy))
            if nearest != gi:
                continue
            near_exit = min((_dist(ex, ey, px, py) for px, py in g.exit), default=99.0)
            close = _dist(ex, ey, g.cx, g.cy) <= DYN_RANGE / 2.0
            if near_exit <= 9.0 or (aggressive and close):
                out.append((_dist(ex, ey, g.cx, g.cy), ex, ey))
        out.sort()
        return [(ex, ey, DYN_WEIGHT) for _, ex, ey in out[:DYN_MAX_ENEMIES]]

    def _assault_targets(self, g):
        """What an assault's spots should be able to hit: the enemy shooters near the payload
        (out to twice the formation range, nearest first) and the ring around the payload where
        the enemy stands to push it. The ring points count for less than a shooter."""
        px, py = g.cx, g.cy
        near = sorted(
            (_dist(ex, ey, px, py), ex, ey) for _, ex, ey in self._threat_pts
            if _dist(ex, ey, px, py) <= 2.0 * ASSAULT_FAR
        )
        out = [(ex, ey, DYN_WEIGHT) for _, ex, ey in near[:DYN_MAX_ENEMIES]]
        for k in range(8):
            a = 2.0 * math.pi * k / 8.0
            rx, ry = px + 1.8 * math.cos(a), py + 1.8 * math.sin(a)
            if self._standable(rx, ry):
                out.append((rx, ry, 1.0))
        return out

    def _transit(self, g, me, healthy, busy):
        """Nothing in range to hit yet: keep the group together and walk it towards the payload.

        The front is simply the spots nearest the group's centre, so it stays one blob while it
        walks. The centre steps TRANSIT_STEP along the walking route to the payload, but only when
        TRANSIT_COHESION of the shooters that have reached the group are within TRANSIT_R of it: the
        forces from the left and the right join up, and nobody in the group is left behind. Bots
        still on their way to it are not part of that count (see TRANSIT_JOIN_R).

        Also the assault's push: with no enemy shooter near the payload the centre walks right up
        onto it, so the nearest spots are the ones round the payload, inside its capture radius.
        """
        fx, fy = g.focus
        # Only the shooters that have reached the group count towards its cohesion; the ones still
        # on their way to join it (further than TRANSIT_JOIN_R) are not waited for, and just come.
        pts = [(me[b].pos.x, me[b].pos.y) for b in healthy]
        group = [(x, y) for x, y in pts if _dist(x, y, fx, fy) <= TRANSIT_JOIN_R]
        joining = len(group) < len(pts)
        near = sum(1 for x, y in group if _dist(x, y, fx, fy) <= TRANSIT_R)
        if group and near >= TRANSIT_COHESION * len(group):
            v = navigate_to(Vec2(fx, fy), Vec2(g.cx, g.cy))
            n = math.hypot(v.x, v.y)
            if n > 1e-6:
                # slower while others are still coming, so they catch the group up
                step = min(TRANSIT_STEP_JOIN if joining else TRANSIT_STEP,
                           _dist(fx, fy, g.cx, g.cy))
                g.focus = (fx + v.x / n * step, fy + v.y / n * step)
                fx, fy = g.focus
        # nobody stands inside the payload's body
        body = None
        if self._pay is not None:
            body = (self._pay[0].x, self._pay[0].y, self._pay[1] + self.conf.bot.radius + 0.1)
        ranked = sorted(
            (_dist(g.grid[i][0], g.grid[i][1], fx, fy), i) for i in g.cands
            if i not in busy and _dist(g.grid[i][0], g.grid[i][1], fx, fy) <= ROI_R
            and (body is None or _dist(g.grid[i][0], g.grid[i][1], body[0], body[1]) >= body[2])
        )
        # The nearest spots, but on our side of any wall: a spot just across a thin wall from the
        # centre would split the blob, so each needs a clear line to the centre (relaxed only if that
        # leaves fewer than half the spots we need).
        here = Vec2(fx, fy)
        front, blocked = [], []
        for _, i in ranked:
            self._static(g, i)
            if g.hug[i]:
                continue
            if line_of_sight(here, Vec2(g.grid[i][0], g.grid[i][1])):
                front.append(i)
            else:
                blocked.append(i)
            if len(front) >= len(healthy):
                break
        if len(front) < len(healthy) // 2:
            front += blocked[:len(healthy) - len(front)]
        picked = set(front)
        g.order = front + [i for i in g.order if i not in picked]
        g.rank = {idx: k for k, idx in enumerate(g.order)}
        g.quality = picked

    def _reserve_spot(self, g, front_set, healer_pts, reach):
        """A spot behind the front for a wounded (or surplus) shooter: not a front spot, free,
        near a healer if there is one, and out of the approach's sight where possible."""
        used = self._busy()
        best = None
        # Any spot in the gate will do, not just the ones near the front: the healers are a hub
        # well behind it, and that is where the wounded need to go.
        for idx in g.cands:
            if idx in used or idx in front_set:
                continue
            x, y = g.grid[idx]
            if healer_pts:
                d = min(_dist(x, y, hx, hy) for hx, hy in healer_pts)
                if d > reach:
                    continue
            else:
                d = -_dist(x, y, g.cx, g.cy)
            self._static(g, idx)
            key = (g.info[idx][1], d, g.rank.get(idx, 10 ** 9))
            if best is None or key < best[0]:
                best = (key, idx)
        return best[1] if best is not None else None

    def _relayout_all(self, me, tick):
        """Decide, gate by gate, whether the group needs laying out again, and do it if so.

        A gate is laid out again when its membership or anyone's health class changed (a shooter
        hurt below the retreat line, healed above the recover line, lost or added), and every
        RELAYOUT_TICKS while an enemy shooter is close, since then the best spots are moving.
        """
        retreat, recover, lane_w, aggressive, lean = self._stance_params()
        full = self.conf.bot.health
        for gi, g in enumerate(self.gates):
            shooters = [
                b for b, gj in self.gate_of.items()
                if gj == gi and b in me and self.cls.get(b) == BotClass.Battle
            ]
            healers = [
                b for b, gj in self.gate_of.items()
                if gj == gi and b in me and self.cls.get(b) == BotClass.Healer
            ]
            # hurt / healed, with a gap between the two lines so a bot does not flicker
            for b in shooters:
                hp = me[b].health
                if b in self.recovering:
                    if hp >= recover * full:
                        self.recovering.discard(b)
                elif hp < retreat * full:
                    self.recovering.add(b)

            sig = (
                tuple(sorted(shooters)),
                tuple(sorted(b for b in shooters if b in self.recovering)),
                tuple(sorted(healers)),
                self.mode,
            )
            enemy_near = any(
                _dist(ex, ey, g.cx, g.cy) <= DYN_RANGE for _, ex, ey in self._threat_pts
            )
            age = tick - g.last_layout
            # an assault re-lays out every cycle: the payload and the enemy are what it is aimed at
            if sig == g.sig and not ((enemy_near or self.mode == "assault") and age >= RELAYOUT_TICKS):
                continue
            if sig != g.sig and age < 5:
                continue
            g.sig = sig
            g.last_layout = tick
            self._relayout(gi, me, lane_w, aggressive, lean)

    def _relayout(self, gi, me, lane_w, aggressive, lean):
        """Lay one gate's group out again around where the enemy is.

        1. Work out the odds (healthy shooters against enemy shooters near the gate) and from them
           whether to advance or fall back, then re-choose the front spots against the enemy
           shooters that are out in the open. A shooter's current spot is favoured, so the shape
           moves a step at a time.
        2. Healthy shooters take the front spots (one each). A shooter already on a front spot
           stays put; the others walk to the nearest free one.
        3. Healers make way if they were standing on a front spot, then stand behind the shooters.
        4. Wounded shooters (and any healthy surplus) go to reserve spots behind the front, in
           heal range of a healer, so the enemy's first shots land on the healthy ones.
        """
        g = self.gates[gi]
        members = [b for b, gj in self.gate_of.items() if gj == gi and b in me]
        shooters = [b for b in members if self.cls.get(b) == BotClass.Battle]
        healers = [b for b in members if self.cls.get(b) == BotClass.Healer]
        healthy = [b for b in shooters if b not in self.recovering]
        wounded = [b for b in shooters if b in self.recovering]
        for b in shooters:
            self.cover_of.pop(b, None)       # cover spots are chosen again below

        # Spots that other bots hold: not this gate's shooters (they are being re-placed) and not
        # its healers (they make way if they are in the front).
        mine = {self.spot_of[b] for b in shooters if b in self.spot_of}
        hs = {self.spot_of[h] for h in healers if h in self.spot_of}
        busy = self._busy() - mine - hs

        # Odds: more healthy shooters than enemy shooters near the gate means press forward, fewer
        # means fall back; the stance adds its own lean. (-1 = fall back .. +1 = advance)
        near = sum(1 for _, ex, ey in self._threat_pts if _dist(ex, ey, g.cx, g.cy) <= DYN_RANGE)
        advance = lean
        if near:
            advance += len(healthy) / near - 1.0
        advance = max(-1.0, min(1.0, advance))
        sticky = {self.spot_of[b] for b in healthy if b in self.spot_of}
        # In an assault with no enemy shooter near the payload, pull the whole blob onto it: it
        # pushes as one body, and nobody has to leave the group to do it.
        g.pull = None
        targets = self._dynamic_targets(gi, aggressive)
        walk = False
        if self.mode == "assault" and healthy:
            if not any(_dist(ex, ey, g.cx, g.cy) <= PUSH_CLEAR for _, ex, ey in self._threat_pts):
                # nobody to fight: the blob stands on the payload and pushes it as one body
                g.pull = (g.cx, g.cy)
                walk = True
            else:
                # enemy shooters out at the payload: keep walking as one blob until the nearest of
                # them is almost in range of the group, and only then lay spots out against them
                fx, fy = g.focus
                contact = min(
                    (_dist(ex, ey, fx, fy) for ex, ey, w in targets if w >= DYN_WEIGHT),
                    default=None,
                )
                walk = contact is None or contact > self.conf.bot.blaster_range + ASSAULT_CONTACT_R
        if walk:
            self._transit(g, me, healthy, busy)
        else:
            self._rescore(g, targets, lane_w,
                          n_front=len(healthy), sticky=sticky, advance=advance, avoid=busy)
            if self.mode == "assault" and not g.quality and healthy:
                self._transit(g, me, healthy, busy)      # nothing in range yet: assemble and walk
        front = [i for i in g.order if i in g.quality and i not in busy][:len(healthy)]
        front_set = set(front)

        for h in healers:
            if self.spot_of.get(h) in front_set:
                self._release_spot(h)

        keep = {b for b in healthy if self.spot_of.get(b) in front_set}
        movers = [b for b in healthy if b not in keep]
        for b in movers:
            self._release_spot(b)
        held = {self.spot_of[b] for b in keep}
        for idx in front:
            if idx in held or not movers:
                continue
            x, y = self.fgrid[idx]
            b = min(movers, key=lambda b: _dist(me[b].pos.x, me[b].pos.y, x, y))
            movers.remove(b)
            self._assign_spot(b, idx)

        # healers behind the shooters
        for h in healers:
            if h not in self.spot_of:
                self._take_support_spot(h, gi)

        # the wounded, and any healthy shooter that found no front spot, drop back
        healer_pts = [self.fgrid[self.spot_of[h]] for h in healers if h in self.spot_of]
        reach = self.conf.bot.base_heal_range - 0.5
        for b in wounded + movers:
            cur = self.spot_of.get(b)
            if cur is not None and cur not in front_set:
                if not healer_pts or min(
                    _dist(self.fgrid[cur][0], self.fgrid[cur][1], hx, hy) for hx, hy in healer_pts
                ) <= reach:
                    continue                                   # already tucked in behind the front
            self._release_spot(b)
            spot = self._reserve_spot(g, front_set, healer_pts, reach)
            if spot is not None:
                self._assign_spot(b, spot)
            else:
                self._take_spot(b, gi)

        if COVER_PEEK:
            self._assign_covers(g, healthy, front_set)

    def _assign_covers(self, g, healthy, front_set):
        """Give each healthy shooter on a front spot a cover spot to reload behind.

        A cover spot is a free spot within COVER_MAX of the shooter's firing spot that the enemy
        cannot see (the fewest enemy shooters, and none of the approach, have a line to it), and
        strictly safer than the firing spot itself: if nothing is safer, or nobody can see the
        firing spot at the moment, the shooter has no cover and simply stays put.
        """
        rng = self.conf.bot.blaster_range
        used = self._busy() | front_set
        watch = sorted(
            ((ex, ey) for _, ex, ey in self._threat_pts if _dist(ex, ey, g.cx, g.cy) <= DYN_RANGE),
            key=lambda p: _dist(p[0], p[1], g.cx, g.cy),
        )[:DYN_MAX_ENEMIES + 4]
        cache = {}

        def exposure(idx):
            e = cache.get(idx)
            if e is None:
                x, y = self.fgrid[idx]
                here = Vec2(x, y)
                e = g.info[idx][1] + sum(
                    1 for ex, ey in watch
                    if _dist(x, y, ex, ey) <= rng and self._sees(here, ex, ey, g.solid)
                )
                cache[idx] = e
            return e

        for b in sorted(healthy):
            fire = self.spot_of.get(b)
            if fire is None or fire not in front_set:
                continue
            fx, fy = self.fgrid[fire]
            best = None
            for idx in g.order:
                if idx in used:
                    continue
                x, y = self.fgrid[idx]
                d = _dist(x, y, fx, fy)
                if d > COVER_MAX:
                    continue
                key = (exposure(idx), d)
                if best is None or key < best[0]:
                    best = (key, idx)
            if best is not None and best[0][0] < exposure(fire):
                self.cover_of[b] = best[1]
                used.add(best[1])

    def _update_peek(self, me, tick):
        """Decide which shooters are stepping out to fire this tick and which are in cover.

        Only while an enemy shooter is close to the gate. Each shooter with a cover spot has a
        state, "out" (on its firing spot) or "cover", and changes it on its blaster's cooldown:
        out -> cover as soon as the cooldown left exceeds the walk to cover plus PEEK_LEAD plus
        PEEK_HYST (i.e. it has just fired); cover -> out once the cooldown left is within the walk
        back plus PEEK_LEAD, so it arrives as the blaster comes ready, and provided SYNC_FRAC of the
        gate's shooters are ready too (or it has waited SYNC_MAX_WAIT ticks), so they go together.
        """
        self.in_cover = set()
        speed = self.conf.bot.speed
        rng = self.conf.bot.blaster_range
        for gi, g in enumerate(self.gates):
            shooters = [
                b for b, gj in self.gate_of.items()
                if gj == gi and b in me and self.cls.get(b) == BotClass.Battle
                and b not in self.recovering and b in self.spot_of and b in self.cover_of
            ]
            if not shooters:
                continue
            engaged = any(
                _dist(ex, ey, g.cx, g.cy) <= rng + PEEK_MARGIN for _, ex, ey in self._threat_pts
            )
            if not engaged:
                for b in shooters:
                    self.peek[b] = "out"
                continue

            lead, wait = {}, {}
            for b in shooters:
                fx, fy = self.fgrid[self.spot_of[b]]
                cx, cy = self.fgrid[self.cover_of[b]]
                lead[b] = _dist(fx, fy, cx, cy) / speed + PEEK_LEAD
                wait[b] = max(0, me[b].next_fire_tick - tick)
            ready = {b: wait[b] <= lead[b] for b in shooters}
            frac = sum(1 for b in shooters if ready[b]) / len(shooters)
            for b in shooters:
                state = self.peek.get(b, "out")
                if state == "out":
                    if wait[b] > lead[b] + PEEK_HYST:
                        state = "cover"
                        self.cover_since[b] = tick
                elif ready[b] and (
                    frac >= SYNC_FRAC or tick - self.cover_since.get(b, tick) >= SYNC_MAX_WAIT
                ):
                    state = "out"
                self.peek[b] = state
                if state == "cover":
                    self.in_cover.add(b)

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
        # Defenders: pushing the payload (only in an assault, and only while no enemy shooter is
        # near it), or standing on their spot in the formation.
        if bid in self.pushers:
            return self._payload_target(bid, bot, state)
        idx = self.spot_of.get(bid)
        if idx is None:
            return None
        if bot.class_ == BotClass.Battle and bid not in self.recovering:
            if bid in self.in_cover and bid in self.cover_of:
                return self.fgrid[self.cover_of[bid]]  # reloading: wait out of sight
            idx = self._hold_flank(bid, bot, idx)     # (a wounded bot stays with its healer)
        x, y = self.fgrid[idx]

        gi = self.gate_of.get(bid)
        g = self.gates[gi] if gi is not None and gi < len(self.gates) else None
        if g is not None:
            # Always together: a target far from the group's centre is no use to anyone. Go to the
            # group (its best front spot) instead of standing alone.
            if _dist(x, y, g.focus[0], g.focus[1]) > TOGETHER_R and g.order:
                return self.fgrid[g.order[0]]
            # Never still: while nothing is in range the whole blob drifts in one slow circle.
            # (Every bot gets the same offset, so it moves as a single body.)
            if self._sway_on:
                a = 2.0 * math.pi * self._tick_no / SWAY_PERIOD
                sx, sy = x + SWAY_R * math.cos(a), y + SWAY_R * math.sin(a)
                if self._standable(sx, sy):
                    return (sx, sy)
        return (x, y)

    def _update_pushers(self, me, state, payload):
        """In an assault, pick the shooters that push the payload: only while it is safe to.

        Pushing means standing within the payload's capture radius, which is exactly where the
        enemy shoots. So: none while an enemy shooter is within PUSH_CLEAR of the payload (the
        formation deals with them first), and none until the group is close (half the shooters
        within 12 units). Then the PUSHERS healthy shooters nearest the payload go and push, and
        rejoin the formation the moment an enemy shooter appears.
        """
        if self.mode != "assault":
            self.pushers = set()
            return
        threat_near = any(
            _dist(ex, ey, payload.x, payload.y) <= PUSH_CLEAR for _, ex, ey in self._threat_pts
        )
        healthy = [b for b, gj in self.gate_of.items()
                   if b in me and self.cls.get(b) == BotClass.Battle and b not in self.recovering]
        if threat_near or not healthy:
            self.pushers = set()
            return
        dist = {b: _dist(me[b].pos.x, me[b].pos.y, payload.x, payload.y) for b in healthy}
        if sum(1 for d in dist.values() if d <= 12.0) < len(healthy) / 2.0:
            self.pushers = set()
            return
        keep = [b for b in self.pushers if b in dist]
        rest = sorted((b for b in healthy if b not in keep), key=lambda b: dist[b])
        self.pushers = set((keep + rest)[:PUSHERS])

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
        """Stop settled bots from sitting on top of each other; never get in a moving bot's way.

        A bot is "moving" if it is asking to travel (des >= MOVING) and "settled" otherwise. Moving
        bots are left exactly as asked: they pass through anyone, so a retreat is never blocked and
        paths are not bent. Only a settled bot is moved, and only away from another settled bot
        that is closer than SETTLE_APART, e.g. two that were given the same spot or landed on the
        same point. (Once nudged it walks back towards its spot, so a shared spot ends up as two
        bots side by side rather than one on top of the other.)
        """
        final = {bid: des[bid] for bid in me}
        settled = [bid for bid in me if math.hypot(des[bid][0], des[bid][1]) < MOVING]
        for i in settled:
            px, py = me[i].pos.x, me[i].pos.y
            rx = ry = 0.0
            for j in settled:
                if i == j:
                    continue
                dx, dy = px - me[j].pos.x, py - me[j].pos.y
                d = math.hypot(dx, dy)
                if d >= SETTLE_APART:
                    continue
                if d < 1e-4:
                    # Exactly on top of each other: pick a direction both bots agree on.
                    a = math.radians(((min(i, j) * 53 + max(i, j) * 29) * 137.508) % 360.0)
                    sign = 1.0 if i > j else -1.0
                    ux, uy = sign * math.cos(a), sign * math.sin(a)
                else:
                    ux, uy = dx / d, dy / d
                s = (SETTLE_APART - d) / SETTLE_APART
                rx += ux * s
                ry += uy * s
            if rx or ry:
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
    # the edge peek
    # ---------------------------------------------------------------------------------

    def _exposed(self, x, y, ex, ey):
        """Could a shot from an enemy at (ex, ey) reach a bot centred at (x, y)? In range, with no
        wall, deposit or payload in between."""
        if _dist(x, y, ex, ey) > self.conf.bot.blaster_range + 0.3:
            return False
        if not line_of_sight(Vec2(x, y), Vec2(ex, ey)):
            return False
        return not self._blocked(x, y, ex, ey)

    def _hull_hidden(self, x, y, ex, ey):
        """Is a whole bot at (x, y) out of the enemy's line, not just its centre? A shot hits the
        0.25-radius hull, so the centre and a point PEEK_HULL to each side must all be hidden."""
        dx, dy = ex - x, ey - y
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return False
        nx, ny = -dy / d, dx / d
        for s in (0.0, PEEK_HULL, -PEEK_HULL):
            if self._exposed(x + nx * s, y + ny * s, ex, ey):
                return False
        return True

    def _peek_geometry(self, H0, near, tick):
        """The HIDE and OUT points for a shooter whose spot is H0, against the enemies `near` it.

        Samples points around H0 (within PEEK_TETHER, and a straight walk from it). OUT points can
        hit the target (the nearest enemy) and HIDE points are hidden from it and from the nearest
        few enemy shooters, body and all. The pair chosen is the closest together, near H0, with a
        longer sightline preferred (up to the blaster's range). Returns None if there is no pair.
        """
        rng = self.conf.bot.blaster_range
        tid = min(near, key=lambda e: _dist(H0[0], H0[1], self._einfo[e][0], self._einfo[e][1]))
        tx, ty = self._einfo[tid][0], self._einfo[tid][1]
        guards = sorted(
            (e for e in near if self._einfo[e][2]),
            key=lambda e: _dist(H0[0], H0[1], self._einfo[e][0], self._einfo[e][1]),
        )[:PEEK_GUARDS]
        gpts = [(self._einfo[e][0], self._einfo[e][1]) for e in guards]

        samples = [H0]
        for r in (0.5, 1.0, 1.5):
            for k in range(8):
                a = 2.0 * math.pi * k / 8.0
                samples.append((H0[0] + r * math.cos(a), H0[1] + r * math.sin(a)))

        outs, hides = [], []
        here = Vec2(H0[0], H0[1])
        for p in samples:
            if _dist(p[0], p[1], H0[0], H0[1]) > PEEK_TETHER or not self._standable(p[0], p[1]):
                continue
            if p != H0 and not corridor_clear(here, Vec2(p[0], p[1])):
                continue
            if (_dist(p[0], p[1], tx, ty) <= rng - 0.5 and self._exposed(p[0], p[1], tx, ty)):
                outs.append(p)
            if all(self._hull_hidden(p[0], p[1], gx, gy) for gx, gy in gpts):
                hides.append(p)
        if not outs or not hides:
            return None

        pairs = []
        for o in outs:
            for h in hides:
                d = _dist(o[0], o[1], h[0], h[1])
                if d < 1e-6:
                    continue
                score = (d + 0.25 * _dist(h[0], h[1], H0[0], H0[1])
                         + 0.1 * _dist(o[0], o[1], H0[0], H0[1])
                         - 0.05 * min(_dist(o[0], o[1], tx, ty), rng - 0.5))
                pairs.append((score, o, h))
        pairs.sort()
        for _, o, h in pairs[:6]:
            if corridor_clear(Vec2(h[0], h[1]), Vec2(o[0], o[1])):
                # a second hidden point to shift to while waiting, so a bot is never standing still
                h2 = None
                for p in hides:
                    d = _dist(p[0], p[1], h[0], h[1])
                    if 0.5 <= d <= 1.4 and corridor_clear(Vec2(h[0], h[1]), Vec2(p[0], p[1])):
                        if h2 is None or abs(d - 0.9) < abs(_dist(h2[0], h2[1], h[0], h[1]) - 0.9):
                            h2 = p
                return {"t": tick, "h0": H0, "hide": h, "hide2": h2, "out": o, "eid": tid}
        return None

    def _peek_ok(self, out, ready_n, stalled):
        """Is it safe to step out to `out` now: could any enemy shooter answer? An enemy answers if
        it is ready within PEEK_RESPONSE ticks and can see `out`. Safe if none can, or if we have at
        least PEEK_VOLLEY_RATIO times as many shooters ready as there are enemies that could; after
        a stall (no blaster fired for STALL_TICKS) as many ready as could answer is enough."""
        responders = 0
        for px, py, shooter, wait, _, _ in self._einfo.values():
            if shooter and wait <= PEEK_RESPONSE and self._exposed(out[0], out[1], px, py):
                responders += 1
        ratio = 1.0 if stalled else PEEK_VOLLEY_RATIO
        return responders == 0 or ready_n >= ratio * responders

    def _peek_moves(self, me, targets, des, tick):
        """Override the movement of every engaged, healthy shooter with the edge peek.

        Each waits at its HIDE point, pre-aimed, and walks to its OUT point to arrive as its blaster
        comes ready if it is safe to (see `_peek_ok`); after it fires the cooldown sends it straight
        back. Everything else about it (its spot, its gate, healing) is unchanged: the spot is just
        the place it peeks from.
        """
        if not self._einfo:
            self.pk = {}
            return
        conf = self.conf
        speed = conf.bot.speed
        rng = conf.bot.blaster_range
        shooters = [
            b for b, r in self.role.items()
            if r == ROLE_DEFENDER and b in me and me[b].class_ == BotClass.Battle
            and b not in self.recovering and b not in self.pushers and b in self.spot_of
        ]
        ready = {b: max(0, me[b].next_fire_tick - tick) for b in shooters}
        ready_n = sum(1 for w in ready.values() if w <= PEEK_READY_WIN)
        stalled = tick - self._last_shot >= STALL_TICKS

        for b in shooters:
            H0 = self.fgrid[self.spot_of[b]]
            bot = me[b]
            px, py = bot.pos.x, bot.pos.y
            if _dist(px, py, H0[0], H0[1]) > PEEK_TETHER + 0.6:
                self.pk.pop(b, None)                # still walking to its spot
                continue
            near = [
                e for e, info in self._einfo.items()
                if _dist(H0[0], H0[1], info[0], info[1]) <= rng + PEEK_ENGAGE
            ]
            if not near:
                self.pk.pop(b, None)
                continue

            st = self.pk.get(b)
            if (st is None or st["h0"] != H0 or tick - st["t"] >= PEEK_SCAN_TICKS
                    or (not st.get("none") and st["eid"] not in self._einfo)):
                geo = self._peek_geometry(H0, near, tick)
                st = geo if geo is not None else {"t": tick, "h0": H0, "none": True, "eid": None}
                self.pk[b] = st
            if st.get("none"):
                continue

            hide, out = st["hide"], st["out"]
            travel = _dist(px, py, out[0], out[1]) / speed        # from where it stands right now
            tx, ty = self._einfo[st["eid"]][0], self._einfo[st["eid"]][1]
            go = (
                ready[b] <= travel + PEEK_LEAD
                and self._exposed(out[0], out[1], tx, ty)     # it can still hit its target from OUT
                and self._peek_ok(out, ready_n, stalled)
            )
            if go:
                aim = out
            else:
                # waiting: shift between two hidden points, so it is never standing still
                aim = hide
                if st.get("hide2") and ((tick // PEEK_SWAY_TICKS) + b) % 2 == 1:
                    aim = st["hide2"]
            dx, dy = aim[0] - px, aim[1] - py
            if math.hypot(dx, dy) > 1e-3:
                des[b] = _clip(dx / speed, dy / speed, 1.0)
            else:
                des[b] = (0.0, 0.0)
            targets[b] = None                # off the stuck watch: moving in and out is not stuck

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
            pk = self.pk.get(bid)
            if pk and not pk.get("none") and pk.get("eid") in self._einfo:
                # peeking against a particular enemy: face it now, from cover, so that the moment
                # the bot steps out it is already aimed and can fire on the first tick
                pool = [(self._einfo[pk["eid"]][0], self._einfo[pk["eid"]][1])]
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
