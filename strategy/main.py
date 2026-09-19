"""The plan.

    DEFEND (before the march)
      * Opening: 8 extractors, 7 shooters and 3 healers. The extractors mine OUR deposit; the army
        guards it. Every later build (free or rush) is a shooter or a healer at about 2.5 shooters
        to a healer; lost extractors are only replaced once the army is big enough to defend them.
      * The army holds the gate the enemy will come through (or the one the payload is heading for),
        and turns on any enemy shooter that gets near the deposit. It never leaves our half.
      * Just before the endgame (nothing can be built once it starts) the extractors self-destruct and
        the banked tokens refill their slots with shooters and healers.
      * If the payload is pushed deep into our half the whole army goes to it at once.
    PUSH (from the march)
      * The whole army walks the payload towards the enemy, fighting whatever it meets.
    HOLD (payload past the centre)
      * The army stands just outside the payload's capture radius and defends; if the payload is
        knocked back it pushes again.

    ALWAYS: the combat technique in `combat.py` (one blob of shooters that peeks in and out together,
    healers well behind), the payload hugger (one shooter that stays against the payload, on the side the
    enemy cannot see), and the extractors keep as far from enemy shooters as they can while mining.

The code is split by concern: settings.py (every number), terrain.py (the map), economy.py
(building and extractors), combat.py (the army), movement.py (walking and unsticking).
"""

import traceback

from . import (
    MAP_SIZE,
    BotClass,
    FleetAction,
    GameState,
    SpecialAction,
    Strategy,
    Vec2,
    get_budget,
    get_config,
    move_bot,
    turn_towards,
)
from .combat import CombatMixin
from .economy import EconomyMixin
from .movement import MovementMixin
from .settings import *
from .terrain import TerrainMixin
from .util import dist, hex_offsets


def get_strategy(team: int) -> Strategy:
    """Both sides run the same plan: the engine mirrors the world for the top-right team."""
    return Plan()


class Plan(TerrainMixin, EconomyMixin, CombatMixin, MovementMixin):
    def __init__(self):
        self.ready = False
        self.conf = None
        self._tick_no = 0
        # who is who
        self.role = {}              # bot id -> ROLE_*
        self.cls = {}               # bot id -> the BotClass it was born as (catches id reuse)
        self.pending = None         # (role, class) of the bot we ordered last tick
        self.built = 0
        # phase
        self.phase = "defend"
        self.emergency = False      # the payload is deep in our half: everyone to it
        # the map
        self.gates = []
        self.primary = 0
        self.gate_i = 0
        self.gate_since = -10 ** 9
        self._gate_checked = -10 ** 9
        self._width_cache = {}
        self.mine_pts = []
        self.miner_slot = {}        # extractor id -> index into mine_pts
        self.last_miner_plan = -10 ** 9
        # per-tick facts
        self._solids = []
        self._capture = 0.0
        self._threat_pts = []       # (id, x, y) of enemy shooters
        self._einfo = {}            # enemy id -> (predicted x, y, is_shooter, ticks to ready, health, invulnerable until)
        self._prev_nft = {}
        self._prev_enft = {}
        self._last_shot = 0         # tick a blaster (either side) last fired
        self.low = False            # the compute bank is getting low
        self.geo_every = GEO_TICKS
        # the army
        self.order = []             # the blob's shooters, in a stable order
        self.hugger = None          # the shooter that hugs the payload
        self.hug_ang = None         # ... and the side of the payload it stands on
        self.hug_t = -10 ** 9
        self.blob_offs = []         # the lattice the blob is laid out on (see setup)
        self._off_cache = {}
        self.recovering = set()     # wounded shooters, at the healers' hub
        self.track = None
        self.track_kind = None
        self.track_obj = (0.0, 0.0)
        self.track_built = -10 ** 9
        self.track_a_y = 0.0
        self.a = 0.0                # arc of the blob's centre on the track
        self.track_pay = (0.0, 0.0) # where the payload was when the track was built
        self.engaged = False        # an enemy shooter is near enough to fight
        self.critical = False       # the compute bank is nearly empty
        self.cycle = "hide"         # "hide" or "out"
        self.cycle_since = 0
        self.out_nft = {}
        self.geo = None
        self.target_id = None
        self._face = (0.0, 0.0)
        self.aim = {}
        # movement
        self.hist = {}
        self.escape = {}
        self._last_err = {}

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

    def _guard(self, name, fn, *args, default=None):
        """Run one step of the tick; if it raises, log it (at most every 300 ticks per step, with the
        traceback) and carry on with `default`, so one bug cannot freeze the whole army."""
        try:
            return fn(*args)
        except Exception:
            if self._tick_no - self._last_err.get(name, -10 ** 9) >= 300:
                self._last_err[name] = self._tick_no
                print(f"[plan] tick {self._tick_no}: step '{name}' raised, carrying on without it")
                traceback.print_exc()
            return default

    # ---------------------------------------------------------------------------------
    # one-time setup
    # ---------------------------------------------------------------------------------

    def _setup(self, state):
        conf = get_config()
        self.conf = conf
        b = conf.bot
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

        # The march starts about when the extractor swap does (see SWAP_EXTRACTORS).
        self.push_start = self.endgame_start - SWAP_LEAD_TICKS - N_MINERS - SWAP_MARGIN

        self.blob_offs = hex_offsets(BLOB_SPACING, 80)
        self.gates = self._find_gates()
        self._set_priors()
        self.gate_i = self.primary
        self.mine_pts = self._mining_spots()
        mine_at = self.mine_pts[0] if self.mine_pts else (self.dep_x, self.dep_y)
        self.dep_walk_ticks = self._walk_ticks(self.spawn, mine_at)

        for i, g in enumerate(self.gates):
            print(f"[plan] gate {i} at ({g.cx:.1f}, {g.cy:.1f}): width {g.width:.1f}, "
                  f"routes {g.hits}, prior {g.prior:.2f}")
        print(f"[plan] our deposit at ({self.dep_x:.1f}, {self.dep_y:.1f}); {len(self.mine_pts)} mining "
              f"spots; march at tick {self.push_start}")
        if len(self.mine_pts) < N_MINERS:
            print(f"[plan] WARNING: only {len(self.mine_pts)} spots with a sightline to the deposit")
        self.ready = True

    # ---------------------------------------------------------------------------------
    # the tick
    # ---------------------------------------------------------------------------------

    def _tick(self, state):
        if not self.ready:
            self._setup(state)
        conf = self.conf
        tick = state.tick
        self._tick_no = tick
        me = {b.id: b for b in state.fleet_me}
        enemies = list(state.fleet_other)

        # Per-tick facts. The payload and both deposits are solid: bots cannot walk through them
        # and a shot stops dead on them.
        payload = state.payload_pos()
        self._solids = [
            (state.deposit_me.pos, conf.deposit.radius),
            (state.deposit_other.pos, conf.deposit.radius),
            (payload, conf.payload.radius),
        ]
        self._capture = state.capture
        self._threat_pts = [
            (e.id, e.pos.x, e.pos.y) for e in enemies if e.class_ == BotClass.Battle
        ]
        # every enemy: where it will be after this tick's move, whether it is a shooter, how many
        # ticks until its blaster is ready, its health, and when its invulnerability ends
        self._einfo = {}
        for e in enemies:
            shooter = e.class_ == BotClass.Battle
            wait = max(0, e.next_fire_tick - tick) if shooter else 0
            self._einfo[e.id] = (
                e.pos.x + e.vel.x, e.pos.y + e.vel.y, shooter, wait, e.health,
                e.invulnerable_until_tick,
            )
        self._track_shots(me, enemies, tick)
        self._read_budget()

        self._sync_roles(me)
        self._update_phase(tick)

        action = FleetAction.new()
        self._decide_build(state, action)
        if not self.critical:
            self._guard("plan_miners", self._plan_miners, me, tick)

        # 1. where does each bot want to stand
        slot = self._guard("army", self._plan_army, me, state, tick, default={})
        targets = {}
        for bid in me:
            if self.role.get(bid) == ROLE_MINER:
                targets[bid] = self._miner_spot(bid)
            else:
                targets[bid] = slot.get(bid)

        # 2. how to get there
        final = {}
        for bid, bot in me.items():
            tgt = targets[bid]
            final[bid] = self._desired(bot, tgt) if tgt is not None else (0.0, 0.0)
        if not self.critical:
            for bid, bot in me.items():
                unstick = self._guard(
                    "stuck_check", self._stuck_check, bid, bot, targets[bid], final[bid], tick
                )
                if unstick is not None:
                    final[bid] = unstick

        speed = conf.bot.speed
        if self.phase == "defend" and not self.emergency:
            # Nobody leaves our half until the march (except the payload hugger).
            for bid, bot in me.items():
                if bid == self.hugger:
                    continue
                fx, fy = final[bid]
                if bot.pos.y + fy * speed < self.y_min:
                    final[bid] = (fx, min(1.0, max(0.0, (self.y_min - bot.pos.y) / speed)))
        npos = {
            bid: (bot.pos.x + final[bid][0] * speed, bot.pos.y + final[bid][1] * speed)
            for bid, bot in me.items()
        }

        # 3. turn / shoot / heal / mine, using where each bot will actually be this tick
        claimed = set()
        swap = self._plan_swap(state, me)
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
                self._guard("shoot", self._shoot, bid, bot, nx, ny, tick, claimed, ba)
            else:
                self._guard("heal", self._heal, bid, bot, nx, ny, me, npos, ba)
        return action

    # ---------------------------------------------------------------------------------
    # bookkeeping
    # ---------------------------------------------------------------------------------

    def _forget(self, bid):
        self.role.pop(bid, None)
        self.cls.pop(bid, None)
        self.aim.pop(bid, None)
        self.hist.pop(bid, None)
        self.escape.pop(bid, None)
        self.recovering.discard(bid)
        self.miner_slot.pop(bid, None)

    def _track_shots(self, me, enemies, tick):
        """A blaster's next_fire_tick changes when it fires: note when anyone last did."""
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

    def _read_budget(self):
        """How much of the compute bank is left decides how much thinking to do this tick."""
        remaining = None
        try:
            remaining = get_budget().remaining
        except Exception:
            pass
        self.low = remaining is not None and remaining < BUDGET_LOW
        self.critical = remaining is not None and remaining < BUDGET_CRITICAL
        self.geo_every = GEO_TICKS * (6 if self.critical else 3 if self.low else 1)

    def _update_phase(self, tick):
        """defend -> push (the march) <-> hold (payload past the centre); and the payload emergency."""
        c = self._capture
        was = self.emergency
        self.emergency = c <= -EMERGENCY_EXIT_C if was else c <= -EMERGENCY_C
        if self.emergency != was:
            print(f"[plan] tick {tick}: payload emergency {'on' if self.emergency else 'over'} "
                  f"(capture {c:.3f})")
            if self.emergency:
                self.gate_since = -10 ** 9

        old = self.phase
        if tick < self.push_start:
            self.phase = "defend"
        elif self.phase == "defend":
            self.phase = "push"
        if self.phase == "push" and c >= HOLD_C:
            self.phase = "hold"
        elif self.phase == "hold" and c <= REPUSH_C:
            self.phase = "push"
        if self.phase != old:
            print(f"[plan] tick {tick}: {old} -> {self.phase} (capture {c:.3f})")
