"""Bounded tactical chains that the coach can prepare before an opening."""

from dataclasses import dataclass
import re

from .commands import Command, Target, catalog, parse_command
from .fight import TACTICS


OPENERS = (
    "axe_berserkers_call", "beastmaster_primal_roar", "crystal_maiden_frostbite",
    "doom_bringer_doom", "lion_voodoo", "skeleton_king_hellfire_blast",
)
CHAIN_ACTIONS = (*TACTICS, "push", "defend", "retreat", "roshan")
BRANCHES = ("start", "on_success", "on_failure")


@dataclass(frozen=True, slots=True)
class Step:
    action: str
    target: Target
    bot_ids: tuple[int, ...]
    ttl_s: int
    reason: str
    opener: tuple[int, str] | None

    def request(self, session, clock):
        return Command(session, clock, self.action, self.target, self.bot_ids, self.ttl_s, self.reason)

    def wire(self):
        request = self.request("", 0).wire()
        del request["session_id"], request["clock_s"]
        request["opener"] = {"bot_id": self.opener[0], "ability": self.opener[1]} if self.opener else None
        return request


@dataclass(frozen=True, slots=True)
class Chain:
    id: str
    wait_s: int
    start: Step
    on_success: Step | None
    on_failure: Step | None


@dataclass(slots=True)
class Execution:
    chain: Chain
    expires_at: float
    waiting_since: float
    source: str
    branch: str = "start"
    order_id: str | None = None
    status: str = "waiting"
    reason: str | None = None

    @property
    def step(self):
        return getattr(self.chain, self.branch)

    def wire(self):
        return {"id": self.chain.id, "expires_clock_s": self.expires_at, "branch": self.branch,
                "status": self.status, "reason": self.reason, "order_id": self.order_id,
                **{name: step.wire() if (step := getattr(self.chain, name)) else None for name in BRANCHES}}


def parse_playbook(value):
    if not isinstance(value, dict) or value.keys() != {"window_s", "chains"}:
        raise ValueError("Playbook requires window_s and chains")
    window, chains = value["window_s"], value["chains"]
    if type(window) is not int or not 15 <= window <= 60 or not isinstance(chains, list) or len(chains) > 3:
        raise ValueError("Choose up to three chains and a window of 15 to 60 game seconds")
    parsed = {}
    used_bots = set()
    for chain in chains:
        if not isinstance(chain, dict) or chain.keys() != {"id", "wait_s", *BRANCHES}:
            raise ValueError("Invalid tactical chain fields")
        key, wait = chain["id"], chain["wait_s"]
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", key) or key in parsed:
            raise ValueError("Use a unique chain ID of up to 24 lowercase ASCII characters")
        if type(wait) is not int or not 2 <= wait <= 15:
            raise ValueError("Each step may wait 2 to 15 game seconds")
        steps = {branch: _parse_step(chain[branch]) for branch in BRANCHES}
        if steps["start"] is None:
            raise ValueError("A tactical chain needs a starting order")
        ids = steps["start"].bot_ids
        if used_bots.intersection(ids):
            raise ValueError("Starting groups must use distinct bots")
        used_bots.update(ids)
        parsed[key] = Chain(key, wait, **steps)
    return window, parsed


def _parse_step(value):
    if value is None:
        return None
    if not isinstance(value, dict) or value.keys() != {"action", "target", "bot_ids", "ttl_s", "reason", "opener"}:
        raise ValueError("Each step is a coaching order with an opener or null")
    fields = dict(value)
    opener = fields.pop("opener")
    command = parse_command({"session_id": "playbook", "clock_s": 0, **fields}, enabled_actions=CHAIN_ACTIONS)
    if command.action in TACTICS and len(command.bot_ids) < 2:
        raise ValueError("A fight needs at least two bots")
    if opener is not None:
        if (command.action not in TACTICS or not isinstance(opener, dict) or opener.keys() != {"bot_id", "ability"}
                or type(opener["bot_id"]) is not int or opener["bot_id"] not in command.bot_ids
                or opener["ability"] not in OPENERS):
            raise ValueError("Choose a supported opening ability and a caster in the fight group")
        opener = (opener["bot_id"], opener["ability"])
    return Step(command.action, command.target, command.bot_ids, command.ttl_s, command.reason, opener)


def playbook_schema():
    def integer(low, high):
        return {"type": "integer", "minimum": low, "maximum": high}

    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}

    opener = obj(bot_id=integer(0, 23), ability={"type": "string", "enum": list(OPENERS)})
    actions = catalog(enabled_actions=CHAIN_ACTIONS)["actions"]
    orders = [obj(action={"type": "string", "const": action}, target=spec["target"],
                  bot_ids={"type": "array", "items": integer(0, 23), "minItems": 1, "maxItems": 3},
                  ttl_s=spec["ttl_s"], reason={"type": "string", "minLength": 1, "maxLength": 140},
                  opener={"anyOf": [opener, {"type": "null"}]} if action in TACTICS else {"type": "null"})
              for action, spec in actions.items()]
    order = {"$ref": "#/$defs/playbook_order"}
    branch = {"anyOf": [order, {"type": "null"}]}
    chain = obj(id={"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,23}$"}, wait_s=integer(2, 15),
                start=order, on_success=branch, on_failure=branch)
    return {**obj(window_s=integer(15, 60), chains={"type": "array", "maxItems": 3, "items": chain}),
            "$defs": {"playbook_order": {"anyOf": orders}}}
