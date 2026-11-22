"""Parse the explicitly supported human team calls from the local match."""

from dataclasses import dataclass
from math import isfinite
from unicodedata import normalize


_FIELDS = {"id", "player_id", "source", "clock_s", "text"}
_LANES = {
    "top": "top", "топ": "top",
    "mid": "mid", "мид": "mid",
    "bot": "bot", "бот": "bot",
}


@dataclass(frozen=True, slots=True)
class PlayerCall:
    id: str
    player_id: int
    source: str
    clock_s: int | float
    action: str
    lane: str | None = None


def _valid_clock(value):
    return type(value) is int or (type(value) is float and isfinite(value))


def parse_call(value, *, clock_s):
    """Return a typed call for a complete command from the last eight game seconds."""
    if not isinstance(value, dict) or value.keys() != _FIELDS:
        raise ValueError("Player call must contain id, player_id, source, clock_s and text")
    call_id, player_id = value["id"], value["player_id"]
    source, observed, text = value["source"], value["clock_s"], value["text"]
    if (
        not isinstance(call_id, str) or not 1 <= len(call_id) <= 96
        or not call_id.isascii() or not call_id.isprintable() or not call_id.strip()
    ):
        raise ValueError("Invalid player call ID")
    if type(player_id) is not int or not 0 <= player_id <= 23:
        raise ValueError("Invalid player ID for player call")
    if not isinstance(source, str) or source not in ("chat", "ping"):
        raise ValueError("Player call source must be chat or ping")
    if not _valid_clock(observed) or not _valid_clock(clock_s) or not observed <= clock_s <= observed + 8:
        raise ValueError("Player call is outside the eight-second match window")
    if not isinstance(text, str):
        raise ValueError("Player call text must be a string")

    words = normalize("NFKC", text).casefold().split()
    if words and words[0] == "!coach":
        words = words[1:]
    elif words and words[0].startswith("!"):
        words[0] = words[0][1:]
    else:
        raise ValueError("Player call must start with !")
    if len(words) == 2 and words[0] in ("push", "пуш", "пушим") and words[1] in _LANES:
        return PlayerCall(call_id, player_id, source, observed, "push", _LANES[words[1]])
    if len(words) == 1:
        if words[0] in ("rosh", "roshan", "рош", "рошан"):
            return PlayerCall(call_id, player_id, source, observed, "roshan")
        if words[0] in ("cancel", "отмена", "отмени"):
            return PlayerCall(call_id, player_id, source, observed, "cancel")
    raise ValueError("Unsupported player call")
