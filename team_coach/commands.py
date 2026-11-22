"""Strict wire contract for the registered native coaching actions."""

from dataclasses import dataclass
from math import isfinite
import re
from types import MappingProxyType

from .fight import TACTICS


_FIELDS = ("session_id", "clock_s", "action", "target", "bot_ids", "ttl_s", "reason")
_LANES = ("top", "mid", "bot")
_ABILITY_NAME = r"^[a-z][a-z0-9_]{0,79}$"
_MAX_PLAYER_ID = 23
_MAX_REASON = 140
_MAX_SESSION_ID = 64
_MIN_CLOCK_S, _MAX_CLOCK_S = -900, 36_000


@dataclass(frozen=True, slots=True)
class ActionSpec:
    target_kind: str
    ttl_min_s: int
    ttl_max_s: int
    target_field: str | None = None
    target_choices: tuple[str, ...] = ()


_LANE_ACTION = ActionSpec("lane", 15, 60, "lane", _LANES)
_FIGHT_ACTION = ActionSpec("enemy", 6, 30, "id")
ACTION_REGISTRY = MappingProxyType({
    "push": _LANE_ACTION,
    "defend": _LANE_ACTION,
    "farm": ActionSpec("self", 10, 30),
    "roshan": ActionSpec("roshan", 10, 30),
    "retreat": ActionSpec("self", 3, 15),
    "hold_ultimate": ActionSpec("ability", 2, 10, "name"),
    **dict.fromkeys(TACTICS, _FIGHT_ACTION),
})


@dataclass(frozen=True, slots=True)
class Target:
    kind: str
    lane: str | None = None
    name: str | None = None
    id: int | None = None

    def wire(self):
        return {
            "kind": self.kind,
            **{
                field: value for field in ("lane", "name", "id")
                if (value := getattr(self, field)) is not None
            },
        }


@dataclass(frozen=True, slots=True)
class Command:
    session_id: str
    clock_s: int | float
    action: str
    target: Target
    bot_ids: tuple[int, ...]
    ttl_s: int
    reason: str

    def wire(self):
        return {
            "session_id": self.session_id, "clock_s": self.clock_s, "action": self.action,
            "target": self.target.wire(), "bot_ids": list(self.bot_ids), "ttl_s": self.ttl_s,
            "reason": self.reason,
        }


def _registry(enabled_actions):
    if enabled_actions is None:
        return ACTION_REGISTRY
    enabled = frozenset(enabled_actions)
    return {name: spec for name, spec in ACTION_REGISTRY.items() if name in enabled}


def _player_id(value):
    return type(value) is int and 0 <= value <= _MAX_PLAYER_ID


def is_ability_name(value):
    return isinstance(value, str) and re.fullmatch(_ABILITY_NAME, value) is not None


def _target(value, spec):
    field = spec.target_field
    expected_fields = {"kind", field} if field is not None else {"kind"}
    if not isinstance(value, dict) or value.keys() != expected_fields or value["kind"] != spec.target_kind:
        raise ValueError("Target fields do not match the registered action")
    if field is None:
        return Target(spec.target_kind)

    selected = value[field]
    if spec.target_kind == "enemy":
        valid = _player_id(selected)
    elif spec.target_kind == "ability":
        valid = is_ability_name(selected)
    else:
        valid = isinstance(selected, str) and selected in spec.target_choices
    if not valid:
        raise ValueError("Invalid target for the selected action")
    return Target(spec.target_kind, **{field: selected})


def _printable(value, limit):
    return isinstance(value, str) and 1 <= len(value) <= limit and value.isprintable() and bool(value.strip())


def parse_command(payload, *, enabled_actions=None):
    """Validate JSON shape and return a canonical command, independent of a match."""
    if not isinstance(payload, dict) or payload.keys() != set(_FIELDS):
        raise ValueError("Command must contain session_id, clock_s, action, target, bot_ids, ttl_s and reason")
    session_id, clock_s, action, raw_target, ids, ttl, reason = (payload[key] for key in _FIELDS)
    if not _printable(session_id, _MAX_SESSION_ID):
        raise ValueError("Invalid match session ID")
    if type(clock_s) not in (int, float) or not _MIN_CLOCK_S <= clock_s <= _MAX_CLOCK_S or not isfinite(clock_s):
        raise ValueError("Invalid match clock")
    registry = _registry(enabled_actions)
    if not isinstance(action, str) or action not in registry:
        raise ValueError("Unknown or disabled command action")
    spec = registry[action]
    target = _target(raw_target, spec)
    if not isinstance(ids, list) or not 1 <= len(ids) <= 3 or not all(map(_player_id, ids)):
        raise ValueError("Select one to three bot IDs as integers from 0 to 23")
    if len(set(ids)) != len(ids):
        raise ValueError("Bot IDs must be unique")
    if type(ttl) is not int or not spec.ttl_min_s <= ttl <= spec.ttl_max_s:
        raise ValueError(f"Command lifetime must be {spec.ttl_min_s} to {spec.ttl_max_s} seconds")
    if not _printable(reason, _MAX_REASON):
        raise ValueError("Command reason must contain 1 to 140 printable characters")
    return Command(session_id, clock_s, action, target, tuple(ids), ttl, reason.strip())


def _target_schema(spec):
    fields = {"kind": {"type": "string", "const": spec.target_kind}}
    if spec.target_kind == "enemy":
        fields["id"] = {"type": "integer", "minimum": 0, "maximum": _MAX_PLAYER_ID}
    elif spec.target_kind == "ability":
        fields["name"] = {"type": "string", "pattern": _ABILITY_NAME}
    elif spec.target_field is not None:
        fields[spec.target_field] = {"type": "string", "enum": list(spec.target_choices)}
    return {
        "type": "object", "additionalProperties": False, "required": list(fields), "properties": fields,
    }


def catalog(*, enabled_actions=None):
    """Describe the same action set and shape accepted by parse_command."""
    registry = _registry(enabled_actions)
    return {
        "required": list(_FIELDS),
        "additionalProperties": False,
        "common": {
            "session_id": {"type": "string", "minLength": 1, "maxLength": _MAX_SESSION_ID},
            "clock_s": {"type": "number", "minimum": _MIN_CLOCK_S, "maximum": _MAX_CLOCK_S},
            "action": {"type": "string", "enum": list(registry)},
            "bot_ids": {
                "type": "array", "items": {"type": "integer", "minimum": 0, "maximum": _MAX_PLAYER_ID},
                "minItems": 1, "maxItems": 3,
                "uniqueItems": True,
            },
            "reason": {"type": "string", "minLength": 1, "maxLength": _MAX_REASON},
        },
        "actions": {
            name: {
                "target": _target_schema(spec),
                "ttl_s": {"type": "integer", "minimum": spec.ttl_min_s, "maximum": spec.ttl_max_s},
            }
            for name, spec in registry.items()
        },
    }
