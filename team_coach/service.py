"""Loopback exchange and match state for one coached team."""

from dataclasses import dataclass, replace
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import ceil, isfinite
from threading import Lock
from time import monotonic
from uuid import uuid4
import json

from .policy import base_threat, base_under_attack, decide
from .shadow import ShadowObserver
from .commands import ACTION_REGISTRY, catalog, is_ability_name, parse_command
from .calls import parse_call
from .fight import FightPlanner, GANK_TTL, SIGHT_TTL, TACTICS, TACTIC_TTL, visible_enemies
from .playbook import BRANCHES, OPENERS, Execution, parse_playbook, playbook_schema


TEAMS = ("radiant", "dire")
LANES = ("top", "mid", "bot")
OBJECTIVES = ("push", "defend")
NATIVE_MODES = {"farm", "retreat", "roshan"} | {f"{action}_{lane}" for action in OBJECTIVES for lane in LANES}
REPORT_TTL = 8
OBSERVATION_INTERVAL = 2
EVENT_REVIEW_GAP = 5
MAX_BODY = 16_384
REASON_LIMIT = 140
RUNTIME_REVISION = 4
DEFAULT_TUNING = {"bot_difficulty": 4, "micro": 100, "macro": 100, "coach": 100}


def _tuning(value):
    values = _mapping(value) if value is not None else {}
    return {
        key: _int(values.get(key, default), 0, 4 if key == "bot_difficulty" else 100)
        for key, default in DEFAULT_TUNING.items()
    }


def _mapping(value):
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _number(value, low, high):
    if type(value) not in (int, float) or not low <= value <= high or not isfinite(value):
        raise ValueError("Invalid numeric value in match report")
    return value


def _int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Invalid integer in match report")
    return value


def _bool(value):
    if type(value) is not bool:
        raise ValueError("Expected a boolean")
    return value


def _label(value, limit=48):
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise ValueError("Invalid text field")
    if not value.isascii() or not value.isprintable():
        raise ValueError("Expected a printable game label")
    return value


def _ability_name(value):
    if not is_ability_name(value):
        raise ValueError("Invalid internal ability name")
    return value


def _position(value):
    return {axis: _number(value.get(axis), -10_000, 10_000) for axis in ("x", "y")}


def _list(value, limit):
    if value == {}:  # Open Hyper AI encodes an empty Lua table as an object.
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("Invalid tactical observations")
    return value


def _fight_sample(value):
    sample = _mapping(value)
    enemies = []
    for raw in _list(sample.get("enemies"), 5):
        raw = _mapping(raw)
        enemy = {
            "id": _int(raw.get("id"), 0, 23), "hp": _number(raw.get("hp"), 0, 1),
            **_position(raw),
        }
        for name in ("bkb", "bkb_ready", "support", "under_tower"):
            if name in raw:
                enemy[name] = _bool(raw[name])
        if "level" in raw:
            enemy["level"] = _int(raw["level"], 1, 30)
        for name, low, high in (("role", 1, 5), ("kills", 0, 1000), ("deaths", 0, 1000)):
            if name in raw:
                enemy[name] = _int(raw[name], low, high)
        if "ultimate" in raw:
            enemy["ultimate"] = _ability_name(raw["ultimate"])
        if "ultimate_cooldowns" in raw:
            enemy["ultimate_cooldowns"] = [
                {"name": _ability_name(spell.get("name")), "cooldown_s": _number(spell.get("cooldown_s"), 0, 3600)}
                for spell in map(_mapping, _list(raw["ultimate_cooldowns"], 8))
            ]
        enemies.append(enemy)
    return {**_position(sample), "enemies": enemies}


def _bot(value):
    bot = _mapping(value)
    report = {
        "id": _int(bot.get("id"), 0, 23),
        "hero": _label(bot.get("hero")),
        "role": _int(bot.get("role"), 0, 5),
        "alive": _bool(bot.get("alive")),
        "hp": _number(bot.get("hp"), 0, 1),
        "zone": _label(bot.get("zone"), 24),
        "mode": _label(bot.get("mode"), 24),
        "tp_ready": _bool(bot.get("tp_ready")),
    }
    difficulty = bot.get("difficulty")
    if type(difficulty) is int and 0 <= difficulty <= 4:
        report["difficulty"] = difficulty
    level = bot.get("level")
    if type(level) is int and 1 <= level <= 30:
        report["level"] = level
    if bot.get("desires") is not None:
        report["desires"] = {
            mode: _number(score, 0, 1)
            for mode, score in _mapping(bot["desires"]).items()
            if mode in NATIVE_MODES
        }
    for key in ("nearby_allies", "nearby_enemies"):
        if bot.get(key) is not None:
            report[key] = _int(bot[key], 0, 24)
    for key in ("mana", "mode_desire"):
        if bot.get(key) is not None:
            report[key] = _number(bot[key], 0, 1)
    if bot.get("under_fire") is not None:
        report["under_fire"] = _bool(bot["under_fire"])
    if bot.get("ultimates") is not None:
        report["ultimates"] = []
        for ability in _list(bot["ultimates"], 8):
            ability = _mapping(ability)
            report["ultimates"].append({
                "name": _ability_name(ability.get("name")),
                "level": _int(ability.get("level"), 0, 30),
                "cooldown_s": _number(ability.get("cooldown_s"), 0, 3600),
                "active": _bool(ability.get("active")),
            })
    if bot.get("fight") is not None:
        try:
            report["fight"] = _fight_sample(bot["fight"])
        except ValueError:
            # An invalid optional combat observation must not stop tower and hero reporting.
            pass
    if bot.get("tactical_openers") is not None:
        report["tactical_openers"] = [name for name in _list(bot["tactical_openers"], len(OPENERS)) if name in OPENERS]
    return report


def _map(value):
    report = _mapping(value)
    lanes = _mapping(report.get("lanes"))
    enemies = report.get("enemies")
    if enemies == {}:
        enemies = []
    if not isinstance(enemies, list) or len(enemies) > 5:
        raise ValueError("Expected up to five opponents")
    clean_lanes = {}
    for name in LANES:
        lane = _mapping(lanes.get(name))
        clean = {
            "ally_front": _number(lane.get("ally_front"), 0, 1),
            "enemy_front": _number(lane.get("enemy_front"), 0, 1),
            "own_tower": _int(lane.get("own_tower"), 0, 4),
            "enemy_tower": _int(lane.get("enemy_tower"), 0, 4),
            "enemy_near_own": _int(lane.get("enemy_near_own"), 0, 5),
            "ally_near_enemy": _int(lane.get("ally_near_enemy"), 0, 5),
        }
        for key in ("own_tower_hp", "enemy_tower_hp"):
            if lane.get(key) is not None:
                clean[key] = _number(lane[key], 0, 1)
        for key in ("own_tower_pos", "enemy_tower_pos"):
            if lane.get(key) is not None:
                clean[key] = _position(_mapping(lane[key]))
        for key in ("enemy_near_enemy", "ally_wave_near_enemy", "enemy_wave_near_own"):
            if lane.get(key) is not None:
                clean[key] = _int(lane[key], 0, 30)
        clean_lanes[name] = clean
    clean_enemies = []
    for entry in enemies:
        enemy = _mapping(entry)
        clean = {
            "id": _int(enemy.get("id"), 0, 23),
            "hero": _label(enemy.get("hero")),
            "alive": _bool(enemy.get("alive")),
            "zone": _label(enemy.get("zone"), 24),
        }
        if enemy.get("seen_s") is not None:
            clean["seen_s"] = _number(enemy["seen_s"], 0, 36_000)
        clean_enemies.append(clean)
    optional = {"tuning": _tuning(report.get("tuning"))}
    if report.get("ancient_threat") is not None:
        optional["ancient_threat"] = _bool(report["ancient_threat"])
    if report.get("human_ids") is not None:
        optional["human_ids"] = sorted({_int(value, 0, 23) for value in _list(report["human_ids"], 5)})
    if report.get("roshan") is not None:
        roshan = _mapping(report["roshan"])
        optional["roshan"] = {key: _bool(roshan.get(key)) for key in ("visible", "estimated_alive")}
    return {"lanes": clean_lanes, "enemies": clean_enemies, **optional}


def _choices(allies, lanes, *, ancient_threat=False):
    choices = []
    available = [
        bot["id"] for bot in allies
        if bot["alive"] and bot["hp"] >= 0.65 and bot["mode"] != "retreat"
    ]
    if not available:
        return choices
    defending_base = ancient_threat or any(base_threat(lane) for lane in lanes.values())
    for name, lane in lanes.items():
        if lane["own_tower"] and lane["enemy_near_own"]:
            choices.append({"objective": "defend", "lane": name, "bot_ids": available})

        friendly_presence = lane["ally_near_enemy"]
        nearby_enemies = lane.get("enemy_near_enemy", 0)
        if (
            not defending_base and lane["enemy_tower"] and friendly_presence
            and friendly_presence >= nearby_enemies and lane.get("ally_wave_near_enemy", 1)
        ):
            choices.append({"objective": "push", "lane": name, "bot_ids": available})
    return choices


def _signals(context):
    signals = {
        "ancient_base_threat": context.get("ancient_threat", False),
        "tuning": tuple(context.get("tuning", DEFAULT_TUNING).values()),
        "allies_down": tuple(bot["id"] for bot in context["allies"] if not bot["alive"]),
        "enemies_down": tuple(sorted(bot["id"] for bot in context["enemies"] if not bot["alive"])),
    }
    for name, lane in context["lanes"].items():
        signals[f"{name}_towers"] = (lane["own_tower"], lane["enemy_tower"])
        signals[f"{name}_pressure"] = min(lane["enemy_near_own"], 3)
        signals[f"{name}_base_threat"] = base_threat(lane)
        signals[f"{name}_siege"] = bool(
            lane["ally_near_enemy"]
            and lane.get("ally_wave_near_enemy", 1)
            and lane["ally_near_enemy"] >= lane.get("enemy_near_enemy", 0)
        )
    return signals


@dataclass(frozen=True)
class _Plan:
    id: str
    objective: str
    lane: str
    bot_ids: tuple
    expires_clock_s: float
    tower: int
    reason: str
    source: str

    def wire(self):
        return {
            "id": self.id, "objective": self.objective, "lane": self.lane,
            "bot_ids": list(self.bot_ids), "expires_clock_s": self.expires_clock_s,
            "source": self.source, "tower": self.tower,
        }

    def end_reason(self, context):
        if context is None:
            return "telemetry_missing"
        if context["clock_s"] >= self.expires_clock_s:
            return "expired"
        tower_key = "own_tower" if self.objective == "defend" else "enemy_tower"
        if context["lanes"][self.lane][tower_key] != self.tower:
            return "tower_changed"
        eligible = next((choice["bot_ids"] for choice in context["choices"]
                         if choice["objective"] == self.objective and choice["lane"] == self.lane), None)
        if eligible is None:
            return "lane_window_closed"
        if not set(self.bot_ids).issubset(eligible):
            return "participants_changed"
        return None

    def summary(self, clock_s):
        return {**self.wire(), "remaining_s": round(self.expires_clock_s - clock_s)}


@dataclass(frozen=True)
class _ActiveCommand:
    id: str
    action: str
    target: dict
    bot_ids: tuple
    expires_clock_s: float
    source: str = "manual"

    def wire(self):
        return {
            "id": self.id, "action": self.action, "target": self.target,
            "bot_ids": list(self.bot_ids), "expires_clock_s": self.expires_clock_s,
            "source": self.source,
        }


def _plan_command(plan):
    return _ActiveCommand(
        plan.id, plan.objective, {"kind": "lane", "lane": plan.lane},
        plan.bot_ids, plan.expires_clock_s, plan.source,
    ).wire()


def _command_fits(action, target, bot, context):
    if not bot["alive"]:
        return False
    if action == "hold_ultimate":
        name = target["name"]
        return any(
            ult["name"] == name and ult["level"] > 0 and ult["cooldown_s"] <= 0.5 and not ult["active"]
            for ult in bot.get("ultimates", ())
        )
    if bot.get("desires", {}).get(action, 0) <= 0:
        return False
    if action == "farm":
        return (
            bot["hp"] >= 0.65 and bot["mode"] not in ("attack", "retreat", "roshan")
            and not bot["mode"].startswith(("defend_", "push_"))
            and bot.get("nearby_enemies") == 0 and bot.get("under_fire") is False
            and not base_under_attack(context)
        )
    if action == "roshan":
        roshan = context.get("roshan", {})
        return (
            (roshan.get("visible") or roshan.get("estimated_alive"))
            and bot["hp"] >= 0.65 and bot["mode"] not in ("retreat", "attack")
            and not bot["mode"].startswith(("push_", "defend_"))
            and bot.get("under_fire") is not True and bot.get("nearby_enemies") == 0
            and not base_under_attack(context)
        )
    if action == "retreat":
        return bot.get("under_fire") is True or (
            bot["hp"] <= 0.55 and bot.get("nearby_enemies", 0) > bot.get("nearby_allies", 0) + 1
        )
    return False


def _eligible(context, objective, lane, bot_ids):
    return any(
        choice["objective"] == objective
        and choice["lane"] == lane
        and set(bot_ids).issubset(choice["bot_ids"])
        for choice in context["choices"]
    )


class CoachService:
    def __init__(
        self, *, team="radiant", mode="model", cooldown_s=15, max_calls=4096, coach=None,
        decision_interval_s=2, clock=monotonic, journal=None,
        accept_player_calls=False, caller_id=None, opponent_pressure=False,
    ):
        if team not in TEAMS or mode not in ("model", "rules", "observe", "shadow"):
            raise ValueError("Unknown team or operating mode")
        if cooldown_s < 0 or max_calls < 0:
            raise ValueError("Invalid request budget")
        if not 2 <= decision_interval_s <= 20:
            raise ValueError("Decision interval must be within 2..20 seconds")
        self.team = team
        self.accept_player_calls = accept_player_calls
        self.opponent_pressure = opponent_pressure and mode != "shadow"
        self.caller_id = _int(caller_id, 0, 23) if caller_id is not None else None
        self.mode = mode
        self.cooldown_s = cooldown_s
        self.max_calls = max_calls
        self.decision_interval_s = decision_interval_s
        self.coach = coach
        self.model_primary = bool(getattr(coach, "primary", False))
        self.clock = clock
        self.journal = journal
        self.lock = Lock()
        self.pending = False
        self.session_id = None
        self._workers = {}
        self._reset_match()

    def worker_update(self, name, *, starting=False, error=None):
        with self.lock:
            previous = self._workers.get(name, {})
            now = self.clock()
            if starting:
                self._workers[name] = {**previous, "at": now, "busy": True}
            else:
                self._workers[name] = {"at": now, "busy": False, "error": error}
                if error and (error != previous.get("error") or now - previous.get("reported", 0) >= 10):
                    self._record("worker_error", worker=name, message=error[:600])
                    self._workers[name]["reported"] = now
                elif error:
                    self._workers[name]["reported"] = previous.get("reported", now)

    def _record(self, event, **fields):
        if self.journal:
            self.journal({"event": event, "session_id": self.session_id, **fields})

    def _reset_match(self):
        self.session_id = None
        self.game_clock = None
        self.reports = {}
        self.map_report = None
        self.current_plan = None
        self.applied = set()
        self.commands = {}
        self.command_applied = {}
        self.fights = FightPlanner(self._record, gank_enabled=self.opponent_pressure)
        self.pressure_progress_at = None
        self.pressure_blocked = {}
        self.pressure_interrupted = None
        self.pressure_retried = {}
        self.pressure_seen_fight = None
        self.playbook = {}
        self.playbook_revision = 0
        self.lane_resolution = None
        self.manual_revision = 0
        self.calls = 0
        self.usage = {}
        self.strategy_hint = None
        self.reviews = 0
        self.player_call = None
        self.pending_player_call = None
        self.seen_player_calls = deque(maxlen=128)
        self.last_player_chat_clock = float("-inf")
        self.last_review_clock = None
        self.review_signals = None
        self.last_decision = None
        self.observer = ShadowObserver()
        self.observations = 0
        self.last_observation_clock = None
        self.last_signals = None
        self.next_call = 0
        self.last_call_started = None
        self.last_consult_clock = None
        self.last_consult_event = None
        self.last_model_result = None

    def _runtime(self, context):
        tuning = context.get("tuning", DEFAULT_TUNING) if context else DEFAULT_TUNING
        macro, coach = tuning["macro"] / 100, tuning["coach"] / 100
        return {
            "macro": macro,
            "coach": coach,
            "decision_interval_s": round(self.decision_interval_s + (20 - self.decision_interval_s) * (1 - macro), 2),
            "cooldown_s": round(self.cooldown_s * (3 - 2 * coach), 2),
            "max_calls": ceil(self.max_calls * coach),
        }

    def _apply_tuning(self, context):
        runtime = self._runtime(context)
        if runtime["coach"] == 0:
            self.strategy_hint = None
        if runtime["macro"] == 0 or self.model_primary and runtime["coach"] == 0:
            if self.current_plan and self.current_plan.source in ("local", "model") and (
                runtime["macro"] == 0 or not self.opponent_pressure or self.current_plan.source == "model"
            ):
                self._end_plan()
            fight = self.fights.plan
            if fight and fight.source in ("local", "model") and (
                runtime["macro"] == 0 or fight.tactic != "gank" or fight.source == "model"
            ):
                self.fights.end(self.game_clock, "automatic_macro_disabled")
            for command in tuple(self.commands.values()):
                if command.source in ("local", "model"):
                    self._drop_command(command.id)
        return runtime

    def _context(self, now):
        if self.map_report is None or now - self.map_report[0] > REPORT_TTL:
            return None
        reports = [entry for _, entry in sorted(self.reports.items()) if now - entry[0] <= REPORT_TTL]
        allies = [
            bot if self.game_clock - at <= SIGHT_TTL else {key: value for key, value in bot.items() if key != "fight"}
            for _, at, bot in reports
        ]
        if not allies:
            return None
        board = self.map_report[2]
        context = {
            "clock_s": self.game_clock, "team": self.team, "allies": allies,
            "tuning": dict(board.get("tuning", DEFAULT_TUNING)),
            "objective_window": (
                self.fights.last_success is not None
                and 0 <= self.game_clock - self.fights.last_success <= 12
            ),
            "enemies": board["enemies"], "lanes": board["lanes"],
            **({"roshan": board["roshan"]} if "roshan" in board else {}),
            **({"ancient_threat": board["ancient_threat"]} if "ancient_threat" in board else {}),
            "choices": _choices(allies, board["lanes"], ancient_threat=board.get("ancient_threat", False)),
        }
        if any("fight" in bot for bot in allies):
            opponents = {enemy["id"] for enemy in board["enemies"] if enemy["alive"]}
            context["fight_enemies"] = [
                enemy for enemy in visible_enemies(reports, self.game_clock) if enemy["id"] in opponents
            ]
        return context

    def _active_plan(self, context):
        plan = self.current_plan
        cause = plan.end_reason(context) if plan else None
        if cause:
            self._end_plan(cause)
        return self.current_plan

    def _end_plan(self, reason=None):
        plan = self.current_plan
        if plan is not None:
            self.lane_resolution = (plan.id, reason)
            self._record("plan_ended", plan_id=plan.id, clock_s=self.game_clock, reason=reason)
            if self.opponent_pressure and reason in ("tower_changed", "lane_window_closed", "participants_changed", "no_bot_response"):
                self.pressure_interrupted = (plan.id, reason)
            self.current_plan = None
            self.applied.clear()

    def _active_hint(self, context):
        hint = self.strategy_hint
        if hint is not None and hint.end_reason(context):
            self.strategy_hint = None
        return self.strategy_hint

    def _drop_command(self, command_id):
        command = self.commands.pop(command_id)
        self.command_applied.pop(command_id, None)
        self._record("command_ended", command_id=command.id, action=command.action, clock_s=self.game_clock)

    def _retarget_command(self, command, ids):
        if not ids:
            self._drop_command(command.id)
        elif ids != command.bot_ids:
            self.commands[command.id] = replace(command, bot_ids=ids)
            self.command_applied[command.id].intersection_update(ids)

    def _active_commands(self, context):
        allies = {bot["id"]: bot for bot in context["allies"]} if context is not None else {}
        plan = self._active_plan(context)
        fight = self.fights.plan
        assigned = set(plan.bot_ids if plan else ()) | set(fight.bot_ids if fight else ())
        for command in tuple(self.commands.values()):
            ids = tuple(
                bot_id for bot_id in command.bot_ids
                if command.expires_clock_s > self.game_clock and bot_id in allies
                and not (command.action in ("farm", "roshan") and bot_id in assigned)
                and _command_fits(command.action, command.target, allies[bot_id], context)
            )
            self._retarget_command(command, () if command.action == "roshan" and len(ids) < 2 else ids)
        return tuple(self.commands.values())

    def _fight_reserved(self, context):
        plan = self._active_plan(context)
        return set(plan.bot_ids if plan else ()).union(
            bot_id for command in self._active_commands(context) if command.action in ("retreat", "roshan")
            for bot_id in command.bot_ids
        )

    def _accept_command_reports(self, bot, applied):
        if not isinstance(applied, list) or len(applied) > 3 or not bot["alive"]:
            return
        for command_id in applied:
            command = self.commands.get(command_id) if isinstance(command_id, str) else None
            if (
                command and command.expires_clock_s > self.game_clock
                and bot["id"] in command.bot_ids and bot["id"] not in self.command_applied[command_id]
                and (command.action == "hold_ultimate" or bot["mode"] == command.action)
            ):
                self.command_applied[command_id].add(bot["id"])
                self._record("command_applied", command_id=command_id, action=command.action,
                             bot_id=bot["id"], clock_s=self.game_clock)

    def _end_match(self):
        if self.session_id is not None:
            self.fights.end(self.game_clock, "match_ended")
            self._record("match_ended", clock_s=self.game_clock, calls=self.calls, usage=self.usage)
            self._reset_match()

    def _update_player_call(self, call, status, reason):
        self.player_call = {
            "id": call.id, "player_id": call.player_id, "action": call.action, "lane": call.lane,
            "source": call.source, "clock_s": call.clock_s, "status": status, "reason": reason,
        }
        self._record("player_call", **self.player_call)

    def _cancel_player_orders(self):
        if self.current_plan and self.current_plan.source == "player_call":
            self._end_plan()
        for entry in tuple(self.commands.values()):
            if entry.source == "player_call":
                self._drop_command(entry.id)

    def _process_player_calls(self, packet, context):
        if not self.accept_player_calls or context is None:
            return
        humans = self.map_report[2].get("human_ids", ())
        caller = self.caller_id if self.caller_id is not None else humans[0] if len(humans) == 1 else None
        if caller not in humans or caller in self.reports or any(enemy["id"] == caller for enemy in context["enemies"]):
            return
        reports = packet.get("calls", ())
        clock = context["clock_s"]
        if isinstance(reports, list) and len(reports) <= 8:
            for entry in reports:
                try:
                    call = parse_call(entry, clock_s=clock)
                except ValueError:
                    continue
                if call.player_id != caller or call.id in self.seen_player_calls:
                    continue
                self.seen_player_calls.append(call.id)
                if call.source == "ping":
                    if self.pending_player_call is None:
                        self._update_player_call(call, "hint", "Player ping")
                    continue
                if call.clock_s < self.last_player_chat_clock:
                    continue
                self.last_player_chat_clock = call.clock_s
                if self.mode in ("observe", "shadow"):
                    self._update_player_call(call, "observed", "Automatic player calls are disabled in this mode")
                elif call.action == "cancel":
                    self.pending_player_call = None
                    self._cancel_player_orders()
                    self._update_player_call(call, "cancelled", "Player call orders cancelled")
                else:
                    self.pending_player_call = call
                    self._update_player_call(call, "waiting", "Waiting for an eligible opening")

        call = self.pending_player_call
        if call is None:
            return
        expires_after = 30 if call.action == "roshan" else 8
        if clock - call.clock_s > expires_after:
            self.pending_player_call = None
            self._update_player_call(call, "expired", self.player_call["reason"])
        elif clock >= 0:
            blocker = self._execute_player_call(call, context)
            if blocker is None:
                self.pending_player_call = None
                self._update_player_call(call, "accepted", "Assigned to eligible allied bots")
            elif self.player_call["reason"] != blocker:
                self._update_player_call(call, "waiting", blocker)

    def _execute_player_call(self, call, context):
        if base_under_attack(context):
            return "Allies are needed at the base"
        fight = self.fights.plan
        reserved = set(fight.bot_ids if fight else ())
        reserved.update(bot for command in self._active_commands(context) if command.action == "retreat" for bot in command.bot_ids)
        allies = [bot for bot in context["allies"] if bot["id"] not in reserved]
        clock = context["clock_s"]
        if call.action == "roshan":
            target = {"kind": "roshan"}
            roshan = context.get("roshan", {})
            if not (roshan.get("visible") or roshan.get("estimated_alive")):
                return "Roshan is unavailable or unconfirmed"
            candidates = [bot for bot in allies if _command_fits("roshan", target, bot, context)]
            plan = self.current_plan
            if plan and plan.source == "manual":
                candidates = [bot for bot in candidates if bot["id"] not in plan.bot_ids]
            if len(candidates) < 2:
                if fight or any(
                    bot["alive"] and (bot["mode"] == "attack" or bot.get("nearby_enemies", 0) > 0)
                    for bot in context["allies"]
                ):
                    return "Allies are engaged with enemy heroes"
                return "Fewer than two allies are ready for Roshan"
            ids = tuple(bot["id"] for bot in sorted(
                candidates, key=lambda bot: (-bot["desires"]["roshan"], -bot["hp"], bot["id"]),
            )[:3])
            self._cancel_player_orders()
            if self.current_plan and self.current_plan.source == "local" and set(ids).intersection(self.current_plan.bot_ids):
                self._end_plan()
            self._publish_command(context, "roshan", target, ids, clock + 30, "Player called Roshan", "player_call")
            return None
        choice = next(
            (choice for choice in context["choices"] if choice["objective"] == "push" and choice["lane"] == call.lane),
            None,
        )
        if choice is None:
            return "No viable tower push on the requested lane"
        desire = f"push_{call.lane}"
        candidates = [
            bot for bot in allies if bot["id"] in choice["bot_ids"] and bot["mode"] != "attack"
            and bot.get("under_fire") is not True and bot.get("desires", {}).get(desire, 0) > 0
        ]
        ids = [bot["id"] for bot in sorted(
            candidates, key=lambda bot: (bot["zone"] != call.lane, -bot["desires"][desire], bot["id"]),
        )[:3]]
        if not ids:
            return "No allied bots are ready to push the requested lane"
        decision = {"objective": "push", "lane": call.lane, "bot_ids": ids, "ttl_s": 30, "reason": "Player called a push"}
        self._cancel_player_orders()
        self.manual_revision += 1
        self._publish(self._validated_plan(decision, context, context, "player_call"))

    def end_match(self):
        with self.lock:
            self._end_match()

    def exchange(self, packet):
        packet = _mapping(packet)
        if packet.get("team") != self.team:
            raise ValueError("Report from a team outside this match configuration")
        game_clock = _number(packet.get("clock_s"), -900, 36_000)
        bot = _bot(packet.get("bot"))
        board = _map(packet["map"]) if packet.get("map") is not None else None
        with self.lock:
            now = self.clock()
            if (
                board is not None and self.game_clock is not None and game_clock < self.game_clock - 15
            ):
                self._end_match()
            if self.session_id is None:
                self.session_id = uuid4().hex
                self._record("match_connected", team=self.team, clock_s=game_clock)
            if self.game_clock is None or game_clock > self.game_clock:
                self.game_clock = game_clock
            previous = self.reports.get(bot["id"])
            if previous is None or game_clock >= previous[1]:
                self.reports[bot["id"]] = (now, game_clock, bot)
            if board is not None and (self.map_report is None or game_clock >= self.map_report[1]):
                self.map_report = (now, game_clock, board)

            context = self._context(now)
            runtime = self._apply_tuning(context)
            plan = self._active_plan(context)
            ack = packet.get("applied")
            if (
                plan and isinstance(ack, dict) and ack.get("plan_id") == plan.id
                and ack.get("lane") == plan.lane and ack.get("objective") == plan.objective
                and bot["alive"] and (bot["mode"] == f"{plan.objective}_{plan.lane}" or (
                    plan.objective == "defend" and ack.get("rotation") is True and bot["mode"] == "roam"
                ))
                and bot["id"] in plan.bot_ids and (previous is None or game_clock >= previous[1])
            ):
                self.pressure_progress_at = self.game_clock
                if bot["id"] not in self.applied:
                    self.applied.add(bot["id"])
                    self._record("bot_applied", plan_id=plan.id, bot_id=bot["id"], clock_s=game_clock,
                                 rotation=ack.get("rotation") is True)
            self._accept_command_reports(bot, packet.get("applied_commands"))
            if context is not None:
                self.fights.observe(context)
            if previous is None or game_clock >= previous[1]:
                self.fights.accept_report(bot, packet.get("applied_fight"), game_clock)
                self.fights.accept_report(bot, packet.get("fight_ready"), game_clock, ready=True)
                self._accept_opener(bot, packet.get("applied_opener"), game_clock)
            self.fights.tick(context, self._fight_reserved(context))
            self._process_player_calls(packet, context)
            self._tick_playbook(context, runtime)
            self._run_opponent_pressure(context, runtime)
            plan = self._active_plan(context)
            commands = [
                command.wire() for command in self._active_commands(context)
                if bot["id"] in command.bot_ids
            ]
            reply = {"session_id": self.session_id, "plan": plan.wire() if plan else None}
            if packet.get("reply_team") is True:
                reply["team"] = self.team
            if commands:
                reply["commands"] = commands
            fight = self.fights.plan
            if fight and bot["id"] in fight.bot_ids:
                reply["fight"] = fight.wire()
            return reply

    def status(self):
        with self.lock:
            context = self._context(self.clock())
            runtime = self._apply_tuning(context)
            plan = self._active_plan(context)
            hint = self._active_hint(context)
            reserved = self._fight_reserved(context)
            self.fights.tick(context, reserved)
            fight = self.fights.plan
            commands = [command.wire() for command in self._active_commands(context)]
            if plan:
                commands.insert(0, _plan_command(plan))
            if fight:
                commands.insert(0, fight.command())
            return {
                "runtime_revision": RUNTIME_REVISION,
                "controller": "session" if self.model_primary else self.mode,
                "coach_session": self.coach.status() if hasattr(self.coach, "status") else None,
                "worker_health": {
                    name: {"healthy": state.get("error") is None and self.clock() - state["at"] < 45,
                           "busy": state["busy"], "age_s": round(self.clock() - state["at"], 2),
                           "last_error": state.get("error")}
                    for name, state in self._workers.items()
                },
                "phase": "active" if context is not None else "idle",
                "team": self.team, "mode": self.mode, "session_id": self.session_id,
                "calls": self.calls, "max_calls": runtime["max_calls"], "pending": self.pending,
                "usage": dict(self.usage), "applied": sorted(self.applied), "context": context,
                "plan": plan.wire() if plan else None, "reason": plan.reason if plan else None,
                "strategy_hint": hint.wire() if hint else None, "reviews": self.reviews,
                "decision_interval_s": runtime["decision_interval_s"], "last_decision": self.last_decision,
                "model_cooldown_s": runtime["cooldown_s"],
                "last_model_result": self.last_model_result,
                "tuning": dict(context["tuning"] if context else DEFAULT_TUNING),
                "observations": self.observations, "events": self.observer.recent(),
                "last_observation_clock": self.last_observation_clock,
                "fight": fight.wire() if fight else None,
                "fight_applied": sorted(self.fights.applied),
                "fight_ready": sorted(self.fights.ready),
                "fight_choices": self.fights.choices(context, reserved),
                "playbook": [chain.wire() for chain in self.playbook.values()],
                "opener_applied": bool(fight and self.fights.opener_applied == fight.id),
                "commands": commands, "command_applied": {
                    key: sorted(ids) for key, ids in self.command_applied.items() if ids
                },
                "calls_enabled": self.accept_player_calls,
                **({"player_call": self.player_call} if self.accept_player_calls else {}),
            }

    def _validated_plan(self, decision, original, current, source):
        decision = _mapping(decision)
        objective, lane = decision.get("objective"), decision.get("lane")
        ids = decision.get("bot_ids")
        reason = decision.get("reason")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= REASON_LIMIT or not reason.isprintable():
            raise ValueError("Invalid plan reason")
        if objective == "hold" and lane == "none" and ids == [] and decision.get("ttl_s") == 0:
            return None
        if (
            objective not in OBJECTIVES or lane not in LANES
            or not isinstance(ids, list) or not 1 <= len(ids) <= 3
            or any(type(bot_id) is not int for bot_id in ids) or len(ids) != len(set(ids))
        ):
            raise ValueError("Invalid objective, lane or bot assignments")
        ttl = _int(decision.get("ttl_s"), 15, 60)
        tower_key = "own_tower" if objective == "defend" else "enemy_tower"
        tower = current["lanes"][lane][tower_key]
        if (
            original["lanes"][lane][tower_key] != tower
            or not _eligible(original, objective, lane, ids)
            or not _eligible(current, objective, lane, ids)
        ):
            raise ValueError("Plan is no longer eligible on the selected lane")
        return _Plan(
            uuid4().hex[:12], objective, lane, tuple(ids), current["clock_s"] + ttl,
            tower, reason.strip(), source,
        )

    def _publish(self, plan):
        previous = self.current_plan
        same_plan = (
            previous and previous.source == plan.source and previous.objective == plan.objective
            and previous.lane == plan.lane and previous.tower == plan.tower
            and set(previous.bot_ids) == set(plan.bot_ids)
        )
        if same_plan:
            plan = replace(plan, id=previous.id, bot_ids=previous.bot_ids)
        else:
            self.applied.clear()
            self.pressure_progress_at = self.game_clock
        self.current_plan = plan
        if self.fights.plan and set(plan.bot_ids).intersection(self.fights.plan.bot_ids):
            self.fights.end(self.game_clock, "lane_plan")
        event = "plan_renewed" if same_plan else "plan_accepted"
        self._record(event, source=plan.source, clock_s=self.game_clock, plan=plan.wire(), reason=plan.reason)
        return plan.wire()

    def submit_plan(self, payload):
        if self.mode == "shadow":
            raise ValueError("The shadow observer only reads match data")
        payload = _mapping(payload)
        if payload.keys() != {"session_id", "clock_s", "objective", "lane", "bot_ids", "ttl_s", "reason"}:
            raise ValueError("Unexpected plan fields")
        if type(payload["ttl_s"]) is not int:
            raise ValueError("Plan lifetime must be an integer")
        observed = _number(payload.get("clock_s"), -900, 36_000)
        with self.lock:
            context = self._context(self.clock())
            self._apply_tuning(context)
            if (
                not context or payload.get("session_id") != self.session_id
                or not 0 <= context["clock_s"] - observed <= 12
            ):
                raise ValueError("Plan is based on a stale match snapshot")
            plan = self._validated_plan(payload, context, context, "manual")
            self.manual_revision += 1
            if plan is None:
                self._end_plan()
                return None
            return self._publish(plan)

    def command_catalog(self):
        return {"catalog": catalog(), "active": self.status()["commands"]}

    def playbook_status(self):
        with self.lock:
            return {"session_id": self.session_id, "catalog": playbook_schema(),
                    "chains": [item.wire() for item in self.playbook.values()]}

    def submit_playbook(self, payload):
        if self.mode == "shadow" or not isinstance(payload, dict) or payload.keys() != {"session_id", "clock_s", "playbook"}:
            raise ValueError("Expected an active match, session_id, clock_s and playbook")
        window, chains = parse_playbook(payload["playbook"])
        clock = _number(payload["clock_s"], 0, 36_000)
        with self.lock:
            context = self._context(self.clock())
            if not context or payload["session_id"] != self.session_id or not 0 <= context["clock_s"] - clock <= 12:
                raise ValueError("Playbook is based on a stale match snapshot")
            runtime = self._apply_tuning(context)
            if chains and runtime["macro"] == 0:
                raise ValueError("Macro control is disabled")
            self._replace_playbook(chains, window, context, "manual")
            self.manual_revision += 1
            self._tick_playbook(context, runtime)
            return {"chains": [item.wire() for item in self.playbook.values()]}

    def _replace_playbook(self, chains, window, context, source):
        allies = {bot["id"] for bot in context["allies"]}
        enemies = {enemy["id"] for enemy in context["enemies"]}
        for chain in chains.values():
            for branch in BRANCHES:
                step = getattr(chain, branch)
                if step and (not set(step.bot_ids).issubset(allies) or (step.action in TACTICS and step.target.id not in enemies)):
                    raise ValueError("A playbook step refers to a bot or target outside this team's snapshot")
        if source == "model" and any(item.source == "manual" for item in self.playbook.values()):
            raise ValueError("An explicitly submitted playbook has priority")

        now = context["clock_s"]
        replacement = {}
        for key, chain in chains.items():
            current = self.playbook.get(key)
            if (current and current.source == source and current.expires_at > now
                    and (chain == current.chain or (current.status in ("running", "waiting")
                         and getattr(chain, current.branch) == current.step))):
                current.chain = chain
                current.expires_at = now + window
                replacement[key] = current
            else:
                replacement[key] = Execution(chain, now + window, now, source)
        for key, item in self.playbook.items():
            if replacement.get(key) is not item:
                self._release_step(item)
        self.playbook = replacement
        self._record("playbook_accepted", clock_s=now, source=source, chains=list(replacement))

    def _release_step(self, execution):
        key = execution.order_id
        if not key:
            return
        if self.current_plan and self.current_plan.id == key:
            self._end_plan("playbook_revised")
        elif self.fights.plan and self.fights.plan.id == key:
            self.fights.end(self.game_clock, "playbook_revised")
        elif key in self.commands:
            self._drop_command(key)

    def _playbook_event(self, item):
        self.playbook_revision += 1
        self._record("playbook_step", clock_s=self.game_clock, chain_id=item.chain.id,
                     branch=item.branch, status=item.status, reason=item.reason, order_id=item.order_id)

    def _cancel_chain_order(self, order_id):
        for item in self.playbook.values():
            if item.status == "running" and item.order_id == order_id:
                item.status, item.reason = "ended", "explicit_cancel"
                self._playbook_event(item)
                return

    def _finished_step(self, item, context):
        step, key = item.step, item.order_id
        if step.action in TACTICS:
            plan = self.fights.plan
            if plan and plan.id == key:
                if item.branch == "start" and item.chain.on_failure and step.action in ("gank", "pickoff", "flank"):
                    if plan.phase == "reset":
                        self.fights.end(self.game_clock, "enemy_defense_activated")
                        return False, "enemy_defense_activated"
                    if (plan.phase == "commit" and plan.opener and self.fights.opener_applied != key
                            and context["clock_s"] - plan.phase_clock_s >= 5):
                        self.fights.end(self.game_clock, "opening_cast_unconfirmed")
                        return False, "opening_cast_unconfirmed"
                return None
            resolution = self.fights.last_resolution
            cause = resolution[2] if resolution and resolution[0].id == key else "superseded"
            return cause == "target_down", cause
        if step.action in OBJECTIVES:
            if self.current_plan and self.current_plan.id == key:
                return None
            cause = self.lane_resolution[1] if self.lane_resolution and self.lane_resolution[0] == key else "superseded"
            lane = context["lanes"][step.target.lane]
            success = cause == "tower_changed" if step.action == "push" else (
                cause == "lane_window_closed" and lane["own_tower"] > 0 and lane["enemy_near_own"] == 0
            )
            return success, cause or "superseded"
        if key in self.commands:
            return None
        allies = {bot["id"]: bot for bot in context["allies"]}
        success = step.action == "retreat" and all(
            (bot := allies.get(bot_id)) and bot["alive"] and bot.get("under_fire") is False
            and bot.get("nearby_enemies") == 0 for bot_id in step.bot_ids
        )
        return success, "disengaged" if success else "support_order_ended"

    def _dispatch_step(self, item, context):
        step = item.step
        active = self._active_commands(context)
        ids = set(step.bot_ids)
        owned = {other.order_id for other in self.playbook.values() if other.status == "running"}
        lane_plan = self.current_plan
        fight_plan = self.fights.plan
        if any(ids.intersection(command.bot_ids) and command.action != "hold_ultimate" for command in active):
            raise ValueError("A current support order owns a selected bot")
        if lane_plan and (step.action in OBJECTIVES or ids.intersection(lane_plan.bot_ids)):
            if step.action not in OBJECTIVES or lane_plan.id in owned or lane_plan.source != "local":
                raise ValueError("The lane group is already assigned")
        if fight_plan and (step.action in TACTICS or ids.intersection(fight_plan.bot_ids)):
            if (step.action not in TACTICS or fight_plan.id in owned or fight_plan.source != "local"
                    or fight_plan.phase != "stage"):
                raise ValueError("The fight group is already assigned")
        now = context["clock_s"]
        ttl = min(step.ttl_s, int(item.expires_at - now))
        if ttl < ACTION_REGISTRY[step.action].ttl_min_s:
            raise ValueError("Too little time remains in the playbook window")
        request = replace(step.request(self.session_id, now), ttl_s=ttl)
        return self._submit_command_locked(request, context, item.source, opener=step.opener)

    def _tick_playbook(self, context, runtime):
        if not context or not self.playbook:
            return
        now = context["clock_s"]
        for item in self.playbook.values():
            if item.status not in ("waiting", "running"):
                continue
            if now >= item.expires_at or runtime["macro"] == 0 or (item.source == "model" and runtime["coach"] == 0):
                self._release_step(item)
                item.status, item.reason = "ended", "window_closed"
                self._playbook_event(item)
                continue
            # A failed starting order can move to its contingency in the same exchange.
            for _ in range(2):
                if item.status == "running":
                    outcome = self._finished_step(item, context)
                    if outcome is None:
                        break
                    success, cause = outcome
                else:
                    if now - self.map_report[1] > SIGHT_TTL:
                        break
                    try:
                        order = self._dispatch_step(item, context)
                    except ValueError as error:
                        item.reason = str(error)
                        if now - item.waiting_since < item.chain.wait_s:
                            break
                        success, cause = False, "opening_window_missed"
                    else:
                        item.order_id, item.status, item.reason = order["id"], "running", None
                        self._playbook_event(item)
                        break
                item.reason = cause
                branch = "on_success" if success else "on_failure"
                if item.branch == "start" and cause not in ("superseded", "manual_cancel", "coach_cancel", "lane_plan") and getattr(item.chain, branch):
                    item.branch, item.status, item.waiting_since, item.order_id = branch, "waiting", now, None
                    self._playbook_event(item)
                else:
                    item.status = "completed" if success else "ended"
                    self._playbook_event(item)
                    break

    def _accept_opener(self, bot, acknowledgement, clock):
        plan = self.fights.plan
        if (not plan or plan.phase not in ("commit", "reset") or plan.expires_clock_s <= clock
                or not plan.opener or plan.opener[0] != bot["id"] or not isinstance(acknowledgement, dict)
                or acknowledgement.get("id") != plan.id or acknowledgement.get("ability") != plan.opener[1]):
            return
        if self.fights.opener_applied != plan.id:
            self.fights.opener_applied = plan.id
            self.playbook_revision += 1
            self._record("opener_applied", clock_s=clock, fight_id=plan.id, bot_id=bot["id"],
                         ability=plan.opener[1], target_id=plan.target_id)

    def submit_command(self, payload):
        if self.mode == "shadow":
            raise ValueError("The shadow observer only reads match data")
        request = parse_command(payload)
        with self.lock:
            context = self._context(self.clock())
            if (
                not context or request.session_id != self.session_id
                or not 0 <= context["clock_s"] - request.clock_s <= 12
                or context["clock_s"] >= request.clock_s + request.ttl_s
            ):
                raise ValueError("Command is based on a stale match snapshot")
            result = self._submit_command_locked(request, context, "manual")
            self.manual_revision += 1
            return result

    def _submit_command_locked(self, request, context, source, *, opener=None):
        if source == "model":
            protected = [command for command in self._active_commands(context)
                         if command.source in ("manual", "player_call")]
            if self.current_plan and self.current_plan.source in ("manual", "player_call"):
                if request.action in OBJECTIVES or set(request.bot_ids).intersection(self.current_plan.bot_ids):
                    raise ValueError("Human lane call has priority")
            if self.fights.plan and self.fights.plan.source == "manual":
                if request.action in TACTICS or set(request.bot_ids).intersection(self.fights.plan.bot_ids):
                    raise ValueError("Human fight call has priority")
            if any(set(request.bot_ids).intersection(command.bot_ids) for command in protected):
                raise ValueError("Human command has priority")
        if request.action in OBJECTIVES:
            decision = {
                "objective": request.action, "lane": request.target.lane,
                "bot_ids": list(request.bot_ids), "ttl_s": request.ttl_s, "reason": request.reason,
            }
            plan = self._validated_plan(decision, context, context, source)
            self._publish(replace(plan, expires_clock_s=request.clock_s + request.ttl_s))
            return _plan_command(self.current_plan)
        if request.action in TACTICS:
            choice = next((
                choice for choice in self.fights.choices(context, self._fight_reserved(context), bot_ids=request.bot_ids)
                if choice["tactic"] == request.action and choice["target_id"] == request.target.id
            ), None)
            if choice is None:
                raise ValueError("The selected enemy and bots do not have a viable fight setup")
            if opener:
                caster_id, ability = opener
                caster = next((bot for bot in context["allies"] if bot["id"] == caster_id), None)
                if not caster or ability not in caster.get("tactical_openers", ()):
                    raise ValueError("The selected opening spell is unavailable")
                choice = {**choice, "opener": opener}
                # Start the entry with the designated caster in the staged group.
                choice["anchor_id"] = caster_id
                choice["commit_now"] = False
            return self.fights.start(
                choice, self.game_clock, request.clock_s + request.ttl_s, source=source, reason=request.reason,
            )
        return self._publish_command(
            context, request.action, request.target.wire(), request.bot_ids,
            request.clock_s + request.ttl_s, request.reason, source,
        )

    def _publish_command(self, context, action, target, bot_ids, expires_clock_s, reason, source):
        if action == "roshan" and len(bot_ids) < 2:
            raise ValueError("Roshan requires at least two eligible bots")
        allies = {bot["id"]: bot for bot in context["allies"]}
        plan = self._active_plan(context)
        if action in ("farm", "roshan") and plan and set(bot_ids).intersection(plan.bot_ids):
            raise ValueError("A selected bot has an active lane plan")
        fight = self.fights.plan
        if action in ("farm", "roshan") and fight and set(bot_ids).intersection(fight.bot_ids):
            raise ValueError("A selected bot has an active fight plan")
        if not all(
            bot_id in allies and _command_fits(action, target, allies[bot_id], context) for bot_id in bot_ids
        ):
            raise ValueError("Command is unavailable for at least one assigned bot")
        active = self._active_commands(context)
        if action in ("farm", "roshan") and any(
            command.action == "retreat" and set(bot_ids).intersection(command.bot_ids) for command in active
        ):
            raise ValueError("A selected bot has an active retreat command")
        for command in active:
            if (command.action == "hold_ultimate") == (action == "hold_ultimate"):
                remaining = tuple(bot_id for bot_id in command.bot_ids if bot_id not in bot_ids)
                self._retarget_command(command, remaining)
        command = _ActiveCommand(uuid4().hex[:12], action, target, bot_ids, expires_clock_s, source)
        self.commands[command.id] = command
        self.command_applied[command.id] = set()
        if action == "retreat" and fight and set(bot_ids).intersection(fight.bot_ids):
            self.fights.end(self.game_clock, "retreat")
        wire = command.wire()
        self._record("command_accepted", command=wire, clock_s=context["clock_s"], reason=reason, source=source)
        return wire

    def cancel_command(self, payload):
        if self.mode == "shadow":
            raise ValueError("The shadow observer only reads match data")
        payload = _mapping(payload)
        command_id = payload.get("command_id")
        if payload.keys() != {"session_id", "command_id"} or not isinstance(command_id, str):
            raise ValueError("Expected session_id and command_id")
        with self.lock:
            if self.session_id is None or payload["session_id"] != self.session_id:
                raise ValueError("Match session has changed")
            if self.current_plan and self.current_plan.id == command_id:
                self._end_plan()
                self.manual_revision += 1
            elif self.fights.plan and self.fights.plan.id == command_id:
                self.fights.end(self.game_clock, "manual_cancel")
            elif command_id in self.commands:
                self._drop_command(command_id)
            else:
                return {"removed": False}
            self._cancel_chain_order(command_id)
            return {"removed": True}

    def observe(self):
        """Record the live match and recognize repeatable patterns in bot behavior."""
        with self.lock:
            context = self._context(self.clock())
            runtime = self._apply_tuning(context)
            if context is None:
                self.observer.update(None)
                self._active_commands(None)
                self.fights.tick(None, set())
                return []
            clock_s = context["clock_s"]
            if (
                self.last_observation_clock is not None
                and clock_s - self.last_observation_clock < OBSERVATION_INTERVAL
            ):
                return []

            self.last_observation_clock = clock_s
            self.observations += 1
            plan = self._active_plan(context)
            fight = self.fights.plan
            self._record("observation", context=context, plan=plan.wire() if plan else None,
                         fight=fight.wire() if fight else None)
            events = self.observer.update(context, plan=plan.wire() if plan else None)
            for event in events:
                self._record("match_event", **event)
            automatic_rules = self.mode in ("rules", "model") and not self.model_primary
            if automatic_rules and runtime["macro"] > 0:
                self._react_to_fight(context, events)
            if automatic_rules and not self.opponent_pressure and runtime["macro"] > 0:
                objective = decide(context) if context["objective_window"] else None
                take_tower = objective is not None and objective["objective"] == "push"
                delay = (1 - runtime["macro"]) * 12
                if self.fights.plan is None and not take_tower and clock_s >= self.fights.next_auto + delay:
                    reserved = self._fight_reserved(context) | {
                        bot_id for command in self._active_commands(context)
                        if command.action == "farm" and command.source in ("manual", "player_call")
                        for bot_id in command.bot_ids
                    }
                    choice = next((
                        item for item in self.fights.choices(context, reserved)
                        if item["tactic"] != "gank"
                    ), None)
                    if choice:
                        ttl = 26 if choice["tactic"] == "bait" else TACTIC_TTL
                        self.fights.start(
                            choice, clock_s, clock_s + ttl, source="local",
                            reason="Visible target with nearby healthy allies",
                        )
            return events

    def _run_opponent_pressure(self, context, runtime):
        if not self.opponent_pressure or context is None or runtime["macro"] == 0:
            return
        now = context["clock_s"]
        if now < 0 or now - self.map_report[1] > SIGHT_TTL:
            return
        plan = self.current_plan
        if plan and now - self.pressure_progress_at >= 6 and plan.source != "player_call":
            expected_mode = f"{plan.objective}_{plan.lane}"
            if not any(bot["id"] in plan.bot_ids and bot["mode"] == expected_mode for bot in context["allies"]):
                for bot_id in plan.bot_ids:
                    self.pressure_blocked[plan.objective, plan.lane, bot_id] = now + 12
                self._end_plan("no_bot_response")
                self._tick_playbook(context, runtime)
        if self.fights.plan:
            return

        plan = self.current_plan
        reserved = {bot_id for command in self._active_commands(context)
                    if command.action in ("farm", "retreat", "roshan") for bot_id in command.bot_ids}
        if plan:
            reserved.update(plan.bot_ids)
        lane_choices = []
        for choice in () if plan else context["choices"]:
            objective, lane = choice["objective"], choice["lane"]
            tower_key = "own_tower_pos" if objective == "defend" else "enemy_tower_pos"
            if tower_key in context["lanes"][lane]:
                ids = [bot_id for bot_id in choice["bot_ids"] if bot_id not in reserved
                       and now >= self.pressure_blocked.get((objective, lane, bot_id), 0)]
                lane_choices.append({**choice, "bot_ids": ids})
        decision = decide({**context, "choices": lane_choices}, pressure=True)

        interruption, self.pressure_interrupted = self.pressure_interrupted, None
        resolution = self.fights.last_resolution
        retry = None
        covering = None
        if resolution is not None:
            previous, ended, cause = resolution
            fresh = ended is not None and now - ended <= SIGHT_TTL
            if fresh and cause == "numbers_or_health_changed" and now >= self.pressure_retried.get(previous.target_id, 0):
                retry = previous
            if fresh and resolution != self.pressure_seen_fight and cause not in ("manual_cancel", "replaced", "lane_plan"):
                interruption = (previous.id, cause)
                if cause in ("numbers_or_health_changed", "target_under_tower", "target_not_visible"):
                    target = {"kind": "self"}
                    endangered = tuple(bot["id"] for bot in context["allies"]
                                       if bot["id"] in previous.bot_ids and bot["id"] not in reserved
                                       and bot["hp"] < 0.62 and bot["mode"] != "retreat"
                                       and _command_fits("retreat", target, bot, context))[:3]
                    if endangered:
                        covering = self._publish_command(context, "retreat", target, endangered, now + 6,
                                                         "Break contact after losing the fight setup", "local")
                        reserved.update(endangered)
            self.pressure_seen_fight = resolution

        fight_choices = self.fights.choices(context, reserved)
        replacement = next((choice for choice in fight_choices if retry and choice["target_id"] == retry.target_id
                            and set(choice["bot_ids"]) != set(retry.bot_ids)
                            and choice["tactic"] in ("gank", "pickoff", "counterattack")
                            and (not retry.priority_target or choice.get("priority_target"))), None)
        delay = 12 * (1 - runtime["macro"])
        gank = next((choice for choice in fight_choices if choice["tactic"] == "gank"
                     and now >= self.fights.next_gank.get(choice["target_id"], 0)), None)
        choice = replacement
        if choice is None and interruption and decision["objective"] == "hold" and resolution is not None:
            previous, ended, cause = resolution
            if previous.id == interruption[0] and cause in ("numbers_or_health_changed", "target_not_visible", "target_under_tower"):
                visible = {enemy["id"] for enemy in context.get("fight_enemies", ()) if enemy.get("under_tower") is False}
                choice = next((item for item in fight_choices if item["target_id"] != previous.target_id
                               and item["target_id"] in visible and now >= self.pressure_retried.get(item["target_id"], 0)
                               and (item["tactic"] == "counterattack" or item.get("commit_now"))), None)
                if choice:
                    self.pressure_retried[previous.target_id] = now + 18
        if choice is None and now >= self.fights.next_auto + delay and not context["objective_window"]:
            if gank and (gank["priority_target"] or decision["objective"] == "hold"):
                choice = gank
            elif decision["objective"] == "hold" and self.mode in ("rules", "model") and not self.model_primary:
                choice = next((item for item in fight_choices if item["tactic"] != "gank"), None)

        command = None
        if choice:
            ttl = {"gank": GANK_TTL, "bait": 26}.get(choice["tactic"], TACTIC_TTL)
            reason = "Rebuild the fight with fresh participants" if replacement else "Pressure a visible target"
            command = self.fights.start(choice, now, now + ttl, source="local", reason=reason)
            if replacement:
                self.pressure_retried[retry.target_id] = now + 18
                interruption = (retry.id, "numbers_or_health_changed")
        elif decision["objective"] != "hold":
            next_plan = self._validated_plan(decision, context, context, "local")
            self._publish(next_plan)
            command = _plan_command(next_plan)
        if interruption:
            self._record("pressure_replanned", clock_s=now, previous_id=interruption[0], cause=interruption[1],
                         replacement=command, withdrawal=covering)

    def _react_to_fight(self, context, events):
        threatened = {
            event["bot_id"] for event in events if event["kind"] in ("isolated_commitment", "fight_after_losses")
        }
        if not threatened:
            return
        retreating = {
            bot_id for command in self._active_commands(context) if command.action == "retreat"
            for bot_id in command.bot_ids
        }
        responders = tuple(
            bot["id"] for bot in context["allies"]
            if bot["id"] in threatened - retreating
            and _command_fits("retreat", {"kind": "self"}, bot, context)
        )[:3]
        if responders:
            self._publish_command(
                context, "retreat", {"kind": "self"}, responders, context["clock_s"] + 8,
                "Disengage after sustained exposure in a losing fight", "local",
            )

    def review(self):
        """Choose the current team action from telemetry, independent of model requests."""
        with self.lock:
            context = self._context(self.clock())
            runtime = self._apply_tuning(context)
            plan = self._active_plan(context)
            if (self.opponent_pressure or self.model_primary or self.mode in ("observe", "shadow")
                    or runtime["macro"] == 0 or not context or context["clock_s"] < 0):
                return None

            clock_s = context["clock_s"]
            signals = _signals(context)
            previous = self.review_signals
            if self.last_review_clock is not None:
                elapsed = clock_s - self.last_review_clock
                take_objective = (
                    self.fights.last_success is not None
                    and self.fights.last_success > self.last_review_clock
                    and clock_s - self.fights.last_success <= REPORT_TTL
                )
                major_event = any(
                    value != previous[key] for key, value in signals.items()
                    if key.endswith(("_towers", "_down", "_base_threat"))
                )
                interval = runtime["decision_interval_s"]
                if not take_objective and elapsed < interval and (not major_event or elapsed < min(interval, EVENT_REVIEW_GAP)):
                    return None

            self.last_review_clock = clock_s
            self.review_signals = signals
            self.reviews += 1
            if plan and plan.source in ("manual", "player_call"):
                self.last_decision = {"clock_s": clock_s, "objective": plan.objective, "source": plan.source}
                return plan.wire()

            if plan:
                context["previous_plan"] = plan.summary(clock_s)
            hint = self._active_hint(context)
            if hint:
                context["strategy_hint"] = hint.summary(clock_s)
            roshan_bots = {bot for command in self._active_commands(context) if command.action == "roshan" for bot in command.bot_ids}
            busy_bots = roshan_bots | set(self.fights.plan.bot_ids if self.fights.plan else ())
            if busy_bots:
                for choice in context["choices"]:
                    choice["bot_ids"] = [bot_id for bot_id in choice["bot_ids"] if bot_id not in busy_bots]
            decision = decide(context)
            next_plan = self._validated_plan(decision, context, context, "local")
            self.last_decision = {"clock_s": clock_s, **decision}
            self._record("review", **self.last_decision)
            if next_plan is None:
                self._end_plan()
                return None
            return self._publish(next_plan)

    def _add_usage(self, usage):
        for key, value in usage.items():
            if key.endswith("_tokens") and type(value) is int and value >= 0:
                self.usage[key] = self.usage.get(key, 0) + value

    def _coaching_context(self, context):
        """All facts and command choices are scoped to this service's team."""
        active = [command.wire() for command in self._active_commands(context)]
        if self.current_plan:
            active.insert(0, _plan_command(self.current_plan))
        if self.fights.plan:
            active.insert(0, self.fights.plan.command())
        available = [
            {"action": choice["objective"], "target": {"kind": "lane", "lane": choice["lane"]},
             "bot_ids": choice["bot_ids"], "ttl_s": [15, 60]}
            for choice in context["choices"]
        ]
        for action in ("farm", "retreat", "roshan", "hold_ultimate"):
            spec = ACTION_REGISTRY[action]
            targets = ([{"kind": "ability", "name": ability["name"]}
                        for bot in context["allies"] for ability in bot.get("ultimates", ())]
                       if action == "hold_ultimate" else [{"kind": spec.target_kind}])
            seen = set()
            for target in targets:
                key = tuple(target.items())
                if key in seen:
                    continue
                seen.add(key)
                ids = [bot["id"] for bot in context["allies"] if _command_fits(action, target, bot, context)]
                if ids and (action != "roshan" or len(ids) >= 2):
                    available.append({"action": action, "target": target, "bot_ids": ids,
                                      "ttl_s": [spec.ttl_min_s, spec.ttl_max_s]})
        result = {
            **context, "session_id": self.session_id, "available_commands": available,
            "commands": active, "applied": sorted(self.applied),
            "command_applied": {key: sorted(ids) for key, ids in self.command_applied.items()},
            "fight": self.fights.plan.wire() if self.fights.plan else None,
            "fight_choices": self.fights.choices(context, self._fight_reserved(context)),
            "fight_applied": sorted(self.fights.applied), "fight_ready": sorted(self.fights.ready),
            "playbook": [chain.wire() for chain in self.playbook.values()],
            "opener_applied": bool(self.fights.plan and self.fights.opener_applied == self.fights.plan.id),
            "events": self.observer.recent(), "last_result": self.last_model_result,
        }
        if self.accept_player_calls:
            result["player_call"] = self.player_call
        return result

    def _consult_signals(self, context):
        signals = _signals(context)
        signals["playbook_revision"] = self.playbook_revision
        signals["endangered_bots"] = tuple(
            bot["id"] for bot in context["allies"]
            if bot["alive"] and (bot["hp"] <= 0.55 or bot.get("under_fire") is True)
        )
        signals["fight_phase"] = (
            (self.fights.plan.id, self.fights.plan.phase) if self.fights.plan else None
        )
        signals["enemy_resources"] = tuple(
            (enemy["id"], enemy.get("bkb"), enemy.get("bkb_ready"), enemy.get("ultimate"),
             enemy["hp"] <= 0.70,
             tuple((spell["name"], spell["cooldown_s"] <= 0.25)
                   for spell in enemy.get("ultimate_cooldowns", ())))
            for enemy in context.get("fight_enemies", ())
        )
        signals["counter_windows"] = tuple(sorted(self.fights.windows.items()))
        signals["fight_opportunities"] = tuple(
            (choice["tactic"], choice["target_id"], tuple(choice["bot_ids"]))
            for choice in self.fights.choices(context, self._fight_reserved(context))
        )
        if self.accept_player_calls:
            signals["player_call"] = (
                (self.player_call["id"], self.player_call["status"]) if self.player_call else None
            )
        return signals

    def _apply_model_orders(self, decision, original, current):
        from .strategist import validate_decision

        decision = validate_decision(decision)
        accepted, rejected = [], []
        for key in decision["cancel_ids"]:
            if self.current_plan and self.current_plan.id == key and self.current_plan.source == "model":
                self._end_plan()
                accepted.append({"cancelled": key})
            elif self.fights.plan and self.fights.plan.id == key and self.fights.plan.source == "model":
                self.fights.end(self.game_clock, "coach_cancel")
                accepted.append({"cancelled": key})
            elif key in self.commands and self.commands[key].source == "model":
                self._drop_command(key)
                accepted.append({"cancelled": key})
            else:
                rejected.append({"cancelled": key, "error": "Order is absent or belongs to the human"})
                continue
            self._cancel_chain_order(key)
        for order in decision["orders"]:
            try:
                age = current["clock_s"] - original["clock_s"]
                if age > (30 if order["action"] in OBJECTIVES else min(12, order["ttl_s"])):
                    raise ValueError("Tactical observation expired during consultation")
                request = parse_command({"session_id": self.session_id, "clock_s": current["clock_s"], **order})
                if request.action in OBJECTIVES:
                    self._validated_plan({"objective": request.action, "lane": request.target.lane,
                                          "bot_ids": list(request.bot_ids), "ttl_s": request.ttl_s,
                                          "reason": request.reason}, original, current, "model")
                accepted.append(self._submit_command_locked(request, current, "model"))
            except ValueError as error:
                rejected.append({"action": order["action"], "error": str(error)})
        if decision.get("playbook") is not None:
            try:
                if current["clock_s"] - original["clock_s"] > 12:
                    raise ValueError("Tactical observation expired during consultation")
                window, chains = parse_playbook(decision["playbook"])
                self._replace_playbook(chains, window, current, "model")
                self._tick_playbook(current, self._runtime(current))
                accepted.append({"playbook": [chain.wire() for chain in self.playbook.values()]})
            except ValueError as error:
                rejected.append({"action": "playbook", "error": str(error)})
        result = {"clock_s": current["clock_s"], "reason": decision["reason"],
                  "accepted": accepted, "rejected": rejected}
        self.last_model_result = result
        self.last_decision = {"clock_s": current["clock_s"], "source": "model", "reason": decision["reason"]}
        self._record("coach_orders", **result)
        return result

    def _session_step(self):
        with self.lock:
            started = self.clock()
            context = self._context(started)
            runtime = self._apply_tuning(context)
            if (self.mode != "model" or self.pending or not context or context["clock_s"] < 0
                    or runtime["coach"] == 0 or runtime["macro"] == 0 or self.calls >= runtime["max_calls"]):
                return None
            clock_s = context["clock_s"]
            signals = self._consult_signals(context)
            events = self.observer.recent()
            event_key = json.dumps(events[-1], sort_keys=True) if events else None
            first = self.last_consult_clock is None
            elapsed = clock_s - self.last_consult_clock if not first else float("inf")
            changed = [key for key, value in signals.items()
                       if self.last_signals is not None and self.last_signals.get(key) != value]
            event_due = changed or event_key != self.last_consult_event
            timer_due = elapsed >= runtime["cooldown_s"]
            if not first and not timer_due and not (event_due and elapsed >= 2
                                                   and started - self.last_call_started >= 2):
                return None
            context = self._coaching_context(context)
            context["trigger"] = (["match_start"] if first else changed +
                                  (["observed_event"] if event_key != self.last_consult_event else [])
                                  or ["interval"])
            self.pending = True
            self.last_signals, self.last_consult_event = signals, event_key
            self.last_consult_clock, self.last_call_started = clock_s, started
            self.next_call = started + runtime["cooldown_s"]
            self.calls += 1
            session_id = self.session_id
            self._record("coach_requested", call=self.calls, context=context)
        try:
            decision, usage = self.coach(context)
        except Exception as error:
            with self.lock:
                self.pending = False
                if self.session_id == session_id:
                    self._add_usage(getattr(error, "usage", {}))
                    self.last_model_result = {"clock_s": self.game_clock, "error": str(error)[:600]}
                    self._record("coach_error", message=str(error)[:600], usage=getattr(error, "usage", {}))
            return None
        with self.lock:
            self.pending = False
            if self.session_id != session_id:
                return None
            self._add_usage(usage)
            now = self.clock()
            current = self._context(now)
            runtime = self._apply_tuning(current)
            self._record("coach_replied", decision=decision, usage=usage, duration_s=round(now - started, 2))
            try:
                if (not current or runtime["coach"] == 0 or runtime["macro"] == 0
                        or now - started > 30 or current["clock_s"] - context["clock_s"] > 30):
                    raise ValueError("Match snapshot expired or coaching was disabled")
                return self._apply_model_orders(decision, context, current)
            except ValueError as error:
                self.last_model_result = {"clock_s": self.game_clock, "error": str(error)}
                self._record("orders_rejected", message=str(error))
                return None

    def step(self):
        if self.model_primary:
            return self._session_step()
        with self.lock:
            started = self.clock()
            context = self._context(started)
            runtime = self._apply_tuning(context)
            plan = self._active_plan(context)
            if (
                self.coach is None or self.mode != "model" or self.pending
                or not context or context["clock_s"] < 0 or not context["choices"]
                or runtime["coach"] == 0 or runtime["macro"] == 0
                or self.calls >= runtime["max_calls"]
                or self.last_call_started is not None and started - self.last_call_started < runtime["cooldown_s"]
                or (plan and plan.source in ("manual", "player_call"))
            ):
                return None
            signals = _signals(context)
            previous = self.last_signals
            if signals == previous:
                return None
            context["trigger"] = [
                key for key, value in signals.items() if previous is not None and previous.get(key) != value
            ] or ["first_map_opportunity"]
            if plan:
                context["previous_plan"] = plan.summary(context["clock_s"])
            self.pending = True
            self.last_signals = signals
            self.last_call_started = started
            self.next_call = started + runtime["cooldown_s"]
            self.calls += 1
            session_id = self.session_id
            manual_revision = self.manual_revision
            self._record("coach_requested", call=self.calls, context=context)

        try:
            decision, usage = self.coach(context)
        except Exception as error:
            with self.lock:
                self.pending = False
                if self.session_id == session_id:
                    usage = getattr(error, "usage", {})
                    self._add_usage(usage)
                    self._record("coach_error", message=str(error)[:300], usage=usage)
            return None

        with self.lock:
            self.pending = False
            if self.session_id != session_id:
                return None
            self._add_usage(usage)
            now = self.clock()
            current = self._context(now)
            runtime = self._apply_tuning(current)
            self._record("coach_replied", decision=decision, usage=usage, duration_s=round(now - started, 2))
            try:
                if self.manual_revision != manual_revision:
                    raise ValueError("A newer manual plan was submitted")
                if runtime["coach"] == 0 or runtime["macro"] == 0:
                    raise ValueError("Automatic coaching was disabled while consulting")
                if (
                    current is None or current["clock_s"] - context["clock_s"] > 30
                    or now - started > 30
                ):
                    raise ValueError("Match snapshot expired while waiting for a plan")
                hint = self._validated_plan(decision, context, current, "model")
                self.strategy_hint = hint
                self._record("strategy_hint", hint=hint.wire() if hint else None)
                return hint.wire() if hint else None
            except ValueError as error:
                self._record("hint_rejected", message=str(error))
                return None


def _unique_fields(pairs):
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError("JSON fields must be unique")
    return data


class _Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(4)
        super().setup()

    def log_message(self, *_):
        pass

    def _reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"ok": True})
        elif self.path == "/status":
            self._reply(200, self.server.service.status())
        elif self.path == "/commands":
            self._reply(200, self.server.service.command_catalog())
        elif self.path == "/playbook":
            self._reply(200, self.server.service.playbook_status())
        else:
            self._reply(404, {"error": "Unknown endpoint"})

    def do_POST(self):
        service = self.server.service
        endpoints = {"/exchange": service.exchange, "/plan": service.submit_plan, "/commands": service.submit_command,
                     "/playbook": service.submit_playbook}
        self._read_body(endpoints.get(self.path))

    def do_DELETE(self):
        self._read_body(self.server.service.cancel_command if self.path == "/commands" else None)

    def _read_body(self, handler):
        if handler is None:
            self._reply(404, {"error": "Unknown endpoint"})
            return
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Expected application/json")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                raise ValueError("Invalid JSON body length")
            packet = json.loads(self.rfile.read(size), object_pairs_hook=_unique_fields)
            reply = handler(packet)
        except (ValueError, TimeoutError) as error:
            self._reply(400, {"error": str(error)})
            return
        self._reply(200, reply)


def make_server(service, port=32125):
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.service = service
    return server
