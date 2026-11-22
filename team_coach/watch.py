"""Read team observer events without sending commands to the match."""

from argparse import ArgumentParser
from collections import deque
from pathlib import Path
from time import sleep
from urllib.request import urlopen
import json


def _status(port):
    try:
        with urlopen(f"http://127.0.0.1:{port}/status", timeout=3) as response:
            status = json.load(response)
    except OSError as error:
        return {"phase": "unavailable", "error": str(error)}
    return {
        key: status[key] for key in
        ("phase", "mode", "team", "session_id", "observations", "last_observation_clock")
        if key in status
    }


def _event(line):
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    if record.get("event") in (
        "match_connected", "match_ended", "match_event", "player_call",
        "fight_planned", "fight_ready", "fight_phase", "fight_applied", "fight_ended",
        "plan_ended", "bot_applied", "pressure_replanned",
        "playbook_accepted", "playbook_step", "opener_applied",
    ):
        return record
    return None


def _print(record):
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), flush=True)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path)
    parser.add_argument("--port", type=int, default=32125)
    parser.add_argument("--tail", type=int, default=12, help="Recent events to show first")
    parser.add_argument("--follow", action="store_true", help="Wait for new events")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.tail < 0:
        parser.error("Choose a valid port and nonnegative event limit")
    try:
        with args.journal.open(encoding="utf-8") as source:
            status = _status(args.port)
            _print({"event": "status", **status})
            events = (
                event for line in source if (event := _event(line)) is not None
            )
            for event in deque(events, maxlen=args.tail):
                _print(event)
            if not args.follow:
                return

            phase = (status.get("phase"), status.get("session_id"))
            while True:
                offset = source.tell()
                line = source.readline()
                if line.endswith("\n"):
                    if event := _event(line):
                        _print(event)
                    continue
                source.seek(offset)
                sleep(0.5 if phase[0] == "active" else 2)
                status = _status(args.port)
                next_phase = (status.get("phase"), status.get("session_id"))
                if phase != next_phase:
                    phase = next_phase
                    _print({"event": "status", **status})
    except KeyboardInterrupt:
        pass
    except OSError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
