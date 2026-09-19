"""The fabricator and the extractors: what to build, when to swap extractors for fighters, and where
the extractors stand."""

from . import BOTS_MAX, BotClass
from .settings import *
from .util import dist


class EconomyMixin:
    # ---- who is who -----------------------------------------------------------------

    def _sync_roles(self, me):
        """Forget the dead and give every new bot the role it was ordered as."""
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
                role = ROLE_MINER if bot.class_ == BotClass.Extractor else ROLE_FIGHTER
            self.role[bid] = role
            self.cls[bid] = bot.class_
        self.pending = None

    # ---- building ---------------------------------------------------------------------

    def _pool_class(self):
        """Class for the next fighter: a healer whenever there are more than SHOOTERS_PER_HEALER
        shooters to a healer, judged by who is actually alive."""
        shooters = healers = 0
        for bid, role in self.role.items():
            if role != ROLE_FIGHTER:
                continue
            if self.cls[bid] == BotClass.Battle:
                shooters += 1
            elif self.cls[bid] == BotClass.Healer:
                healers += 1
        if healers == 0 or shooters / healers > SHOOTERS_PER_HEALER:
            return BotClass.Healer
        return BotClass.Battle

    def _want_extractor(self, state):
        """Should the next build replace a lost extractor?

        Yes while there are fewer than N_MINERS, but only once the army has FIGHTER_TARGET fighters
        to defend them (or there are fewer than EXTRACTOR_FLOOR extractors: no income at all), and
        only if the replacement could still arrive and mine for EXTRACTOR_MIN_MINE_TICKS before
        the swap.
        """
        miners = sum(1 for r in self.role.values() if r == ROLE_MINER)
        if miners >= N_MINERS:
            return False
        fighters = sum(1 for r in self.role.values() if r == ROLE_FIGHTER)
        if fighters < FIGHTER_TARGET and miners >= EXTRACTOR_FLOOR:
            return False
        return state.tick + self.dep_walk_ticks + EXTRACTOR_MIN_MINE_TICKS <= self.push_start

    def _decide_build(self, state, action):
        conf = self.conf
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
        # unambiguously the free bot's.
        if self.built < len(OPENING):
            cls = OPENING[self.built]
            self.built += 1
        elif self._want_extractor(state):
            cls = BotClass.Extractor        # income first: a lost extractor takes the next build
        else:
            cls = self._pool_class()
        role = ROLE_MINER if cls == BotClass.Extractor else ROLE_FIGHTER
        action.fabricator_next = int(cls)
        action.rush_order = not natural_due
        self.pending = (role, cls)

    def _plan_swap(self, state, me):
        """Ids of extractors to self-destruct this tick so their slots can be refilled with fighters
        before the endgame (when nothing can be built any more)."""
        if not SWAP_EXTRACTORS:
            return set()
        conf = self.conf
        tick = state.tick
        last_build = self.endgame_start - 1
        if tick >= last_build:
            return set()
        miners = [bid for bid, role in self.role.items() if role == ROLE_MINER]
        if not miners:
            return set()

        fab = state.fabricator_me
        free_slots = BOTS_MAX - len(me)
        # Replacements we can pay for: 50 tokens each, plus the free build if it will fire.
        builds = int(fab.tokens // conf.fabricator.rush_cost)
        if fab.next_bot_creation <= last_build:
            builds += 1
        # Free slots are filled first; one bot is built per tick, so no more swaps than ticks left.
        k = min(len(miners), builds - free_slots, last_build - tick)
        if k <= 0:
            return set()
        if tick < last_build - SWAP_LEAD_TICKS - k - SWAP_MARGIN:
            return set()

        miners.sort(key=lambda bid: (me[bid].health, bid))
        chosen = set(miners[:k])
        print(
            f"[plan] tick {tick}: self-destructing {k} of {len(miners)} extractors "
            f"({fab.tokens:.0f} tokens banked) to refill the slots with fighters"
        )
        return chosen

    # ---- extractor positions -----------------------------------------------------------

    def _miner_spot(self, bid):
        """The mining spot an extractor stands on (it keeps the one it is given)."""
        idx = self.miner_slot.get(bid)
        if idx is None:
            used = set(self.miner_slot.values())
            idx = next((i for i in range(len(self.mine_pts)) if i not in used), None)
            if idx is None:
                return None
            self.miner_slot[bid] = idx
        return self.mine_pts[idx]

    def _plan_miners(self, me, tick):
        """Move extractors away from enemy shooters, as far as mining allows.

        Every mining spot can mine, so the only question is which is furthest from the nearest
        enemy shooter. Each extractor, most endangered first, takes a free spot that beats its own
        by MINER_MOVE_GAIN or more.
        """
        if not self._threat_pts or tick - self.last_miner_plan < MINER_REPLAN_TICKS:
            return
        self.last_miner_plan = tick
        miners = [b for b in sorted(self.role)
                  if self.role[b] == ROLE_MINER and b in me and b in self.miner_slot]
        if not miners:
            return

        safety = {}

        def safe(idx):
            s = safety.get(idx)
            if s is None:
                x, y = self.mine_pts[idx]
                s = min(MINER_SAFE_CAP, min(dist(x, y, ex, ey) for _, ex, ey in self._threat_pts))
                safety[idx] = s
            return s

        used = set(self.miner_slot.values())
        for _, b in sorted((safe(self.miner_slot[b]), b) for b in miners):
            old = self.miner_slot[b]
            best = None
            for idx in range(len(self.mine_pts)):
                if idx in used:
                    continue
                s = safe(idx)
                if s >= safe(old) + MINER_MOVE_GAIN and (best is None or s > best[0]):
                    best = (s, idx)
            if best is not None:
                used.discard(old)
                used.add(best[1])
                self.miner_slot[b] = best[1]
