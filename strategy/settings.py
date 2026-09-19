"""Every tunable number of the plan, in one place.

All coordinates are in OUR frame: the engine mirrors the world for the top-right team, so
"our" spawn is always bottom-left, our half is y >= Y_LINE, and a positive `capture` means the
payload is on the enemy's side of centre.
"""

from . import BotClass

ROLE_MINER = "miner"        # an extractor, mining our deposit
ROLE_FIGHTER = "fighter"    # a shooter or a healer, part of the army

# --- economy ----------------------------------------------------------------------------
# Opening: the free tick-0 bot plus 16 rush orders (the 800 starting tokens) = 18 bots.
N_MINERS = 8
OPENING = (
    [BotClass.Extractor] * N_MINERS
    + [BotClass.Battle] * 7
    + [BotClass.Healer] * 3
)
# Everything built after that is a shooter or a healer, at about this many shooters per healer.
SHOOTERS_PER_HEALER = 2.5
# Lost extractors are only replaced once the army has this many fighters (below EXTRACTOR_FLOOR
# extractors nothing would be earned to build with, so one is replaced regardless).
FIGHTER_TARGET = 16
EXTRACTOR_FLOOR = 1
EXTRACTOR_MIN_MINE_TICKS = 300      # a replacement must be able to mine this long to be worth it

# Nothing is built once the endgame starts, so just before it the extractors self-destruct and the
# banked tokens rush shooters and healers into their slots. The swap starts
#     endgame_start - SWAP_LEAD_TICKS - swaps - SWAP_MARGIN
# ticks in, and the army marches for the payload at about the same moment.
SWAP_EXTRACTORS = True
SWAP_MARGIN = 6
SWAP_LEAD_TICKS = 150
WALK_SLACK = 40                     # ticks added to a straight walk (turns, detours)

# --- extractors -------------------------------------------------------------------------
MINER_MAX_DIST = 5.0                # mining spots lie within this of the deposit centre
MINER_REPLAN_TICKS = 20             # how often extractors reconsider their spot ...
MINER_MOVE_GAIN = 1.5               # ... and move only for a spot this much further from the enemy
MINER_SAFE_CAP = 40.0
MINER_SPACING = 0.55

# --- the map ----------------------------------------------------------------------------
# Our half is y >= Y_LINE. Until the march nobody goes north of it (unless the payload is in
# trouble, see EMERGENCY_C).
Y_LINE = 24.0
Y_MARGIN = 0.35

# Gates: the narrow places the enemy has to come through to reach our deposit. Found from the
# map by tracing routes from a grid of points outside our half to the deposit.
GATE_ORIGIN_STEP = 4
GATE_MERGE_DIST = 3.0
GATE_BEFORE = 3                     # route samples (0.5 apart) before the crossing that may hold the gate
GATE_AFTER = 30                     # ... and after it
GATE_TIE = 0.3                      # the first spot this near the narrowest width is the gate
MAX_GATES = 5
GATE_SEEDS = [(30.5, 28.5), (22.5, 23.5), (1.5, 23.5)]     # only if the map yields none
PRIOR_PRIMARY = 0.5                 # the gate on the enemy's shortest route is favoured

# --- phases and the payload ---------------------------------------------------------------
#   defend  until the march: the army guards the deposit at the gates
#   push    from the march: the army walks the payload towards the enemy
#   hold    once the payload is past the centre: stand just outside its capture radius and defend
HOLD_C = 0.12                       # capture at which pushing stops ...
REPUSH_C = 0.04                     # ... and below which it starts again
PAYLOAD_THREAT_C = 0.05             # payload this far onto our side of centre: the gate it is heading for is the post
PAYLOAD_GATE_R = 6.0
# The payload must never sneak past the defence. Once it is pushed this deep into our half the
# whole army goes to it (and stays until it is back above -EMERGENCY_EXIT_C): a bot of ours inside
# its capture radius freezes it whenever the enemy has one there too.
EMERGENCY_C = 0.30
EMERGENCY_EXIT_C = 0.20
TRACK_R = 26.0                      # an enemy shooter this close to our deposit is what the army faces
TARGET_KEEP = 3.0                   # ... and it keeps its current target unless another is this much closer

# Where the group's front stands, as a distance along its track from what it is heading for.
POST_ARC = 4.0                      # at a gate: the blob's centre this far inside it
PUSH_ARC = 2.0                      # at the payload: the blob's centre this close, so it is inside its capture radius
RAID_ARC = 11.0                     # facing a raider: just inside blaster range plus a step
SWAY_R = 0.3                        # a calm group drifts slowly along its track so it is never still
SWAY_PERIOD = 240

# --- the blob -----------------------------------------------------------------------------
# The army is ONE compact blob: shooters on a tight hexagonal lattice around a centre, healers in a
# hub HUB_BEHIND further back (so wounded shooters fall back a good way to be healed). The centre
# slides along a "track", a walkable route from home to whatever the army is facing, and the blob
# comes with it.
BLOB_SPACING = 0.55                 # bots body to body: 0.5 across, so this is touching, not stacked
HUB_BEHIND = 5.0                    # the healers' hub is this far behind the blob's hiding place
ARC_SPEED = 0.045                   # how fast the centre slides along its track (bots walk 0.05)
ARRIVE_R = 1.2                      # a bot this close to its spot has arrived
JOIN_R = 6.0                        # ... and one further than this from its spot is still joining
COHESION = 0.7                      # the blob only advances when this share of those near it have arrived
TAIL_LEN = 14.0                     # the track runs on this far past our deposit, to fall back along
TRACK_REBUILD_MOVE = 2.5            # rebuild the track when its target has moved this far
TRACK_MAX_AGE = 120
OFFSET_TICKS = 8                    # how often the blob's spots are checked against walls again

# --- combat: the whole blob peeks in and out together -----------------------------------------
# hide  the blob sits at the furthest forward place on the track where the enemy cannot see it (a wall,
#       a corner, the payload, or just out of range), cooldowns ticking, wounded healing;
# out   when most of it will be ready, it slides forward to where most of it can hit the enemy,
#       fires, and slides back.
ENGAGE_MARGIN = 6.0                 # an enemy shooter within blaster range + this of the blob: fight
ENGAGE_HYST = 4.0                   # ... and stay fighting until it is this much further
GUARDS = 3                          # how many enemy shooters the hiding place must be hidden from
HULL = 0.25                         # a shot hits a bot's hull, not just its centre
WIN = 8.0                           # how far along the track (either way) the search looks
SAMPLE = 0.5                        # ... and how coarsely
OUT_REACH = 4.0                     # the firing place is at most this far forward of the hiding place
HIDE_MARGIN = 1.0                   # hide this much further back than the first hidden point
GEO_TICKS = 15                      # the hide/out places are worked out again this often
READY_FRAC = 0.6                    # go out when this share of the blob will be ready on arrival
PEEK_LEAD = 6                       # ... allowing this many ticks
MIN_HIDE = 15                       # least ticks in cover
MAX_WAIT = 120                      # most ticks in cover before going out anyway
MAX_OUT = 60                        # most ticks out
FIRED_FRAC = 0.5                    # go back once this share of the blob has fired
RETREAT_HP = 0.5                    # a shooter below this share of its health falls back to the healers
RECOVER_HP = 0.8                    # ... and returns above this
CHASE_ODDS = 1.2                    # advance on an enemy we cannot hit only with this many shooters per enemy
STALL_TICKS = 300                   # nobody has fired for this long: stop being careful

# --- the payload hugger -----------------------------------------------------------------------
# One shooter goes to the payload at the very start and stands as close to it as it can, on the side
# the enemy cannot see (the payload is solid: a shot stops on it). It circles as they move. When
# it dies the shooter nearest the payload takes over. Standing this close it is inside the
# payload's capture radius, so it also pushes the payload towards the enemy whenever no enemy bot is inside.
HUG_GAP = 0.04                      # clearance between its hull and the payload's
HUG_THREAT_R = 16.0                 # enemy shooters this close to the payload count
HUG_TICKS = 6                       # how often it picks its side again
HUG_STAY = 0.4                      # ... and keeps its side unless another is this much better

# --- compute budget -----------------------------------------------------------------------
# The engine charges a bot for its CPU time and stops calling it while it is in debt. Below
# these levels of the bank the plan does less.
BUDGET_LOW = 120_000
BUDGET_CRITICAL = 40_000

# --- stuck detection ----------------------------------------------------------------------
STUCK_WINDOW = 40
STUCK_MIN_MOVE = 0.5
STUCK_ESCAPE_TICKS = 60
STUCK_CLEAR_DIST = 2.0
