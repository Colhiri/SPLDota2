"""Inspect and control a local team coach from the command line."""

from argparse import ArgumentParser
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from pathlib import Path
import json
import sys

from .commands import ACTION_REGISTRY, catalog, parse_command


def _request(port, method, path, payload=None):
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as connection:
        connection.request(method, path, body, headers)
        with connection.getresponse() as response:
            try:
                result = json.load(response)
            except ValueError as error:
                raise ValueError(f"{method} {path}: invalid JSON in HTTP {response.status} response") from error
            if not isinstance(result, dict):
                raise ValueError(f"{method} {path}: expected a JSON object")
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}: {result.get('error') or response.reason}")
            return result


def _send(args, parser):
    spec = ACTION_REGISTRY[args.action]
    target = {"kind": spec.target_kind}
    if spec.target_field is not None:
        if spec.target_choices and args.target not in spec.target_choices:
            parser.error(f"{args.action} requires --target with one of: {', '.join(spec.target_choices)}")
        if args.target is None:
            parser.error(f"{args.action} requires --target")
        if spec.target_kind == "enemy":
            try:
                target[spec.target_field] = int(args.target)
            except ValueError:
                parser.error(f"{args.action} requires a numeric enemy ID")
        else:
            target[spec.target_field] = args.target
    elif args.target is not None:
        parser.error(f"{args.action} does not accept --target")

    match = _request(args.port, "GET", "/status")
    context = match.get("context")
    if match.get("phase") != "active" or not match.get("session_id") or not isinstance(context, dict):
        raise ValueError("The coach has no active match snapshot")
    command = parse_command({
        "session_id": match["session_id"],
        "clock_s": context.get("clock_s"),
        "action": args.action,
        "target": target,
        "bot_ids": args.bots,
        "ttl_s": args.ttl,
        "reason": args.reason,
    })
    return _request(args.port, "POST", "/commands", command.wire())


def _cancel(args):
    match = _request(args.port, "GET", "/status")
    session_id = match.get("session_id")
    if not session_id:
        raise ValueError("The coach has no match session")
    return _request(args.port, "DELETE", "/commands", {
        "session_id": session_id,
        "command_id": args.command_id,
    })


def _playbook(args):
    if args.file is None:
        return _request(args.port, "GET", "/playbook")
    contents = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8-sig")
    playbook = json.loads(contents)
    match = _request(args.port, "GET", "/status")
    if match.get("phase") != "active" or not match.get("session_id"):
        raise ValueError("The coach has no active match snapshot")
    return _request(args.port, "POST", "/playbook", {
        "session_id": match["session_id"], "clock_s": match["context"]["clock_s"], "playbook": playbook,
    })


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=32125, help="Loopback port of the coach (default: 32125)")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    commands = subparsers.add_parser("list", help="Print the command catalog")
    commands.add_argument("--active", action="store_true", help="Get the server catalog and active commands")
    subparsers.add_parser("status", help="Print the current match and bot status")
    send = subparsers.add_parser("send", help="Submit an action using the current match snapshot")
    send.add_argument("action", choices=ACTION_REGISTRY)
    send.add_argument("--target", help="Lane, enemy ID, or ability name")
    send.add_argument("--bots", type=int, nargs="+", required=True, metavar="ID", help="One to three bot IDs")
    send.add_argument("--ttl", type=int, required=True, metavar="SECONDS", help="Lifetime in game seconds")
    send.add_argument("--reason", required=True, help="Short reason for the match journal")
    cancel = subparsers.add_parser("cancel", help="Cancel a command or lane plan by ID")
    cancel.add_argument("command_id")
    playbook = subparsers.add_parser("playbook", help="Read the chain catalog or submit a JSON playbook")
    playbook.add_argument("file", nargs="?", help="JSON file, or - for standard input")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be within 1..65535")

    try:
        if args.operation == "list":
            result = _request(args.port, "GET", "/commands") if args.active else {"catalog": catalog()}
        elif args.operation == "status":
            result = _request(args.port, "GET", "/status")
        elif args.operation == "send":
            result = _send(args, send)
        elif args.operation == "playbook":
            result = _playbook(args)
        else:
            result = _cancel(args)
    except (OSError, HTTPException, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
