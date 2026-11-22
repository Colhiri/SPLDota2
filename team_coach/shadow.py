"""Detect observable team behavior from successive Lua reports."""

from collections import deque


_MIN_SAMPLE_GAP_S = 1
_MAX_SAMPLE_GAP_S = 8
_REPEAT_GAP_S = 30
_LOSS_WINDOW_S = 25


def _committed(mode):
    return mode == "attack" or mode.startswith(("push_", "defend_"))


def _isolated(bot):
    allies = bot.get("nearby_allies")
    enemies = bot.get("nearby_enemies")
    return allies is not None and enemies is not None and allies <= 1 and enemies >= allies + 2


def _base_under_pressure(lane):
    return (
        lane["own_tower"] >= 3 and lane["enemy_near_own"] > 0
        and (lane["enemy_near_own"] >= 2 or lane.get("enemy_wave_near_own", 0) > 0)
    )


class ShadowObserver:
    """Track game-time episodes and distinguish observations from assessments."""

    def __init__(self):
        self._recent = deque(maxlen=80)
        self._last_emitted = {}
        self._latest_clock = None
        self._clear_samples()

    def _clear_samples(self):
        self._clock = None
        self._previous = {}
        self._modes = {}
        self._losses = deque()
        self._pending = {}
        self._plan = None

    def recent(self, limit=8):
        return list(self._recent)[-limit:] if limit > 0 else []

    def modes(self):
        """Return the continuously observed mode and duration of each reporting bot."""
        return {
            bot_id: {"mode": mode, "since_s": start, "duration_s": round(self._clock - start, 1)}
            for bot_id, (_, mode, start) in self._modes.items()
        }

    def _emit(self, result, kind, since, assessment, evidence, **scope):
        event = {
            "kind": kind, "clock_s": self._clock, "since_s": since,
            "duration_s": round(self._clock - since, 1), "assessment": assessment,
            "evidence": evidence, **scope,
        }
        self._recent.append(event)
        result.append(event)

    def _track(self, result, pending, kind, scope, wait, assessment, evidence, **fields):
        key = (kind, scope)
        since = self._pending.get(key, self._clock)
        pending[key] = since
        previous = self._last_emitted.get(key, float("-inf"))
        if previous < since and self._clock - since >= wait and self._clock - previous >= _REPEAT_GAP_S:
            self._emit(result, kind, since, assessment, evidence, **fields)
            self._last_emitted[key] = self._clock

    def _track_bots(self, allies, result):
        previous_modes = self._modes
        self._modes = {}
        for bot_id, bot in allies.items():
            identity = (bot.get("hero"), bot["alive"])
            mode = bot["mode"]
            old_identity, old_mode, start = previous_modes.get(bot_id, (None, None, self._clock))
            self._modes[bot_id] = (
                identity, mode, start if identity == old_identity and mode == old_mode else self._clock,
            )

            old = self._previous.get(bot_id)
            if not old or old.get("hero") != bot.get("hero") or not old["alive"]:
                continue
            if not bot["alive"]:
                self._losses.append((self._clock, bot_id, old["zone"]))
                continue

            old_ults = {ability["name"]: ability for ability in old.get("ultimates", ())}
            for ability in bot.get("ultimates", ()):
                self._track_ultimate(result, bot_id, ability, old_ults.get(ability["name"]))

        while self._losses and self._clock - self._losses[0][0] > _LOSS_WINDOW_S:
            self._losses.popleft()

    def _track_ultimate(self, result, bot_id, ability, old):
        if not old or ability["level"] <= 0:
            return
        evidence = {
            "ability": ability["name"], "level": ability["level"], "active": ability["active"],
            "previous_cooldown_s": old["cooldown_s"], "cooldown_s": ability["cooldown_s"],
        }
        if old["active"] is False and ability["active"] is True:
            self._emit(result, "ultimate_active", self._clock, "observed", evidence, bot_id=bot_id)
        if old["level"] > 0 and old["cooldown_s"] == 0 and ability["cooldown_s"] > 0:
            self._emit(result, "ultimate_cooldown_started", self._clock, "observed", evidence, bot_id=bot_id)

    def _push_observations(self, context, allies, result, pending):
        if any(_base_under_pressure(lane) for lane in context["lanes"].values()):
            return
        enemies_down = sorted(enemy["id"] for enemy in context["enemies"] if not enemy["alive"])
        for choice in context["choices"]:
            if choice["objective"] != "push":
                continue
            lane = choice["lane"]
            board = context["lanes"][lane]
            wave = board.get("ally_wave_near_enemy", 0)
            friendly = board["ally_near_enemy"]
            enemies = board.get("enemy_near_enemy", 0)
            eligible = [
                bot for bot in allies.values()
                if bot["id"] in choice["bot_ids"] and bot["alive"] and bot["hp"] >= 0.68
                and bot["zone"] == lane and bot["mode"] != "retreat"
            ]
            if not wave or friendly <= enemies or len(eligible) < 2:
                continue
            farmers = [bot for bot in eligible if bot["mode"] == "farm" and bot["role"] != 1]
            pushers = [bot["id"] for bot in eligible if bot["mode"] == f"push_{lane}"]
            if len(enemies_down) < 2 or not farmers:
                continue
            evidence = {
                "eligible_ids": [bot["id"] for bot in eligible],
                "farming_ids": [bot["id"] for bot in farmers], "enemies_down": enemies_down,
                "farming_s": {bot["id"]: round(self._clock - self._modes[bot["id"]][2], 1) for bot in farmers},
                "wave_creeps": wave, "friendly_near_tower": friendly, "enemy_near_tower": enemies,
            }
            if not pushers:
                self._track(result, pending, "push_window_unclaimed", lane, 8, "opportunity", evidence, lane=lane)
            elif len(pushers) == 1 and (enemies > 0 or board["enemy_tower"] >= 3):
                self._track(
                    result, pending, "push_split_commitment", lane, 10, "opportunity",
                    {**evidence, "pushing_ids": pushers}, lane=lane,
                )

    def _fight_observations(self, context, allies, result, pending):
        losses = {bot_id: (clock, zone) for clock, bot_id, zone in self._losses}
        dead = frozenset(bot["id"] for bot in allies.values() if not bot["alive"])
        recent_losses = sorted(dead & losses.keys())
        enemy_alive = sum(enemy["alive"] for enemy in context["enemies"])
        ally_alive = len(allies) - len(dead)
        for bot_id, bot in allies.items():
            mode = bot["mode"]
            if not bot["alive"] or not _committed(mode):
                continue
            isolated = _isolated(bot)
            evidence = {
                "hp": bot["hp"], "mode": mode, "mode_s": round(self._clock - self._modes[bot_id][2], 1),
                "nearby_allies": bot.get("nearby_allies"), "nearby_enemies": bot.get("nearby_enemies"),
                "under_fire": bot.get("under_fire"),
            }
            if isolated and bot.get("under_fire") is True and bot["hp"] <= 0.70:
                self._track(
                    result, pending, "isolated_commitment", bot_id, 4, "risk", evidence, bot_id=bot_id,
                )
            zone = bot["zone"]
            same_lane_loss = zone in {"top", "mid", "bot"} and any(
                losses[lost_id][1] == zone for lost_id in recent_losses
            )
            if (
                len(recent_losses) >= 2 and bot["hp"] <= 0.55
                and (isolated or same_lane_loss and enemy_alive > ally_alive)
            ):
                self._track(
                    result, pending, "fight_after_losses", bot_id, 4, "risk",
                    {**evidence, "recent_losses": recent_losses, "allies_alive": ally_alive,
                     "reported_enemies_alive": enemy_alive}, bot_id=bot_id,
                )

    def _plan_observation(self, plan, allies, result, pending):
        if not plan or len(plan.get("bot_ids", ())) < 2 or plan.get("expires_clock_s", float("inf")) <= self._clock:
            self._plan = None
            return
        objective, lane = plan["objective"], plan["lane"]
        ids = tuple(sorted(plan["bot_ids"]))
        expected = {
            "push": f"push_{lane}", "defend": f"defend_{lane}",
            "retreat": "retreat", "farm": "farm", "regroup": "group",
        }.get(objective)
        signature = (plan.get("id"), objective, lane, ids)
        if self._plan != signature:
            self._pending.pop(("objective_uncoordinated", (objective, lane)), None)
            self._plan = signature
        if expected is None or any(bot_id not in allies or not allies[bot_id]["alive"] for bot_id in ids):
            return
        matching = [bot_id for bot_id in ids if allies[bot_id]["mode"] == expected]
        if len(matching) < 2:
            evidence = {
                "objective": objective, "assigned_ids": list(ids), "matching_ids": matching,
                "expected_mode": expected, "observed_modes": {bot_id: allies[bot_id]["mode"] for bot_id in ids},
            }
            self._track(
                result, pending, "objective_uncoordinated", (objective, lane), 10, "risk", evidence, lane=lane,
            )

    def update(self, context, *, plan=None):
        """Return newly detected events. A missing snapshot breaks continuity."""
        if context is None or context["clock_s"] < 0:
            self._clear_samples()
            return []
        clock = context["clock_s"]
        if self._latest_clock is not None and clock < self._latest_clock:
            self._recent.clear()
            self._last_emitted.clear()
            self._clear_samples()
        if self._clock is not None and clock - self._clock < _MIN_SAMPLE_GAP_S:
            return []
        if self._clock is not None and clock - self._clock > _MAX_SAMPLE_GAP_S:
            self._clear_samples()

        self._clock = self._latest_clock = clock
        allies = {bot["id"]: bot for bot in context["allies"]}
        result = []
        pending = {}
        self._track_bots(allies, result)
        self._push_observations(context, allies, result, pending)
        self._fight_observations(context, allies, result, pending)
        self._plan_observation(plan, allies, result, pending)
        self._pending = pending
        self._previous = {
            bot_id: {**bot, "ultimates": [dict(ability) for ability in bot.get("ultimates", ())]}
            for bot_id, bot in allies.items()
        }
        return result
