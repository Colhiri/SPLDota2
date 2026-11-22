"""Run independent coaches for a local Open Hyper AI match."""

from argparse import ArgumentParser
from contextlib import ExitStack
from datetime import datetime
from functools import partial
from pathlib import Path
from threading import Event, Thread
import json

from .console_telemetry import ConsoleTelemetry, LuaMailbox, MAILBOX_NAME, team_mailbox_name
from .install import install, local_bots_path
from .service import TEAMS, CoachService, make_server
from .strategist import ModelCoach


def _journal(output, team):
    def write(event):
        row = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "team": team, **event}
        output.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        if event["event"] not in ("coach_requested", "review", "observation"):
            print(json.dumps({"team": team, **event}, ensure_ascii=False), flush=True)
    return write


def _watch(services, servers, primary, telemetry, mailboxes, args):
    stop = Event()

    def watch_match(name, action, assigned):
        while not stop.wait(0.5):
            for service in assigned:
                if hasattr(service, "worker_update"):
                    service.worker_update(name, starting=True)
            try:
                action()
            except Exception as error:
                for service in assigned:
                    if hasattr(service, "worker_update"):
                        service.worker_update(name, error=str(error))
            else:
                for service in assigned:
                    if hasattr(service, "worker_update"):
                        service.worker_update(name)

    def observe_match():
        if telemetry is not None:
            telemetry.ingest_sides(services, mailboxes)
        for service in services.values():
            service.observe()

    def consult_session(service):
        coach = getattr(service, "coach", None)
        if hasattr(coach, "maintain"):
            coach.maintain()
        service.step()

    actions = [("telemetry", observe_match, list(services.values()))]
    for service in services.values():
        if service.mode in ("rules", "model") and not service.model_primary:
            actions.append(("rules", service.review, [service]))
    strategy_services = [service for service in services.values() if service.mode == "model"]
    if strategy_services and args.max_calls:
        # Separate persistent sessions consult concurrently; neither team waits for the other.
        actions.extend(("coach", partial(consult_session, service), [service]) for service in strategy_services)

    workers = [Thread(target=watch_match, args=action, daemon=True) for action in actions]
    other_servers = [server for side, server in servers.items() if side != primary]
    http_workers = [Thread(target=server.serve_forever, daemon=True) for server in other_servers]
    for worker in [*workers, *http_workers]:
        worker.start()
    try:
        servers[primary].serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for server in other_servers:
            server.shutdown()
        for worker in workers:
            worker.join(timeout=args.model_timeout + 2)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Install the adapter into the local Dota scripts")
    parser.add_argument("--game", type=Path, help="Dota installation path for --install and console-log replies")
    parser.add_argument("--workshop", type=Path, help="Open Hyper AI Workshop folder")
    parser.add_argument("--team", choices=TEAMS, default="radiant", help="Player's side in dual mode")
    parser.add_argument("--port", type=int, default=32125)
    modes = ("model", "observe", "rules", "shadow")
    parser.add_argument("--mode", choices=modes, help="Default: model")
    parser.add_argument("--both", action="store_true", help="Run separate coaches for both teams")
    parser.add_argument("--opponent-port", type=int, help="Opponent port, defaults to --port + 1")
    parser.add_argument("--opponent-mode", choices=modes, help="Opponent mode, defaults to model")
    parser.add_argument("--caller-id", type=int, help="Allied human player ID. Dual mode can select a sole human")
    parser.add_argument("--decision-interval", type=int, default=2, help="Game seconds between local reviews at macro 100 (2–20)")
    parser.add_argument("--max-calls", type=int, default=4096, help="Session turns per team and match at coach 100")
    parser.add_argument("--cooldown", type=int, default=15, help="Game seconds between turns; important events trigger earlier")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--effort", choices=("low", "medium", "high"), default="high")
    parser.add_argument("--model-timeout", type=int, default=30)
    parser.add_argument("--log", type=Path, help="Append player-side events to this JSON Lines file")
    parser.add_argument("--dota-log", type=Path, help="Read telemetry from this Dota console log")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.max_calls < 0 or args.cooldown < 0:
        parser.error("Choose a valid port and nonnegative request budget")
    if not 1 <= args.model_timeout <= 30:
        parser.error("--model-timeout must be within 1..30 seconds")
    if not 2 <= args.decision_interval <= 20:
        parser.error("--decision-interval must be within 2..20 seconds")
    if args.caller_id is not None and not 0 <= args.caller_id <= 23:
        parser.error("--caller-id must be within 0..23")
    if not args.both and (args.opponent_port is not None or args.opponent_mode is not None):
        parser.error("Opponent settings require --both")

    date = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    log = args.log or Path("runs", f"team-coach-{date}", "events.jsonl")
    ports = {args.team: args.port}
    selected_modes = {args.team: args.mode or "model"}
    journals = {args.team: log}
    opponent_port = None
    if args.both:
        opponent = next(team for team in TEAMS if team != args.team)
        opponent_port = args.opponent_port if args.opponent_port is not None else args.port + 1
        if not 1 <= opponent_port <= 65535 or opponent_port == args.port:
            parser.error("Choose distinct valid TCP ports for both coaches")
        ports[opponent] = opponent_port
        selected_modes[opponent] = args.opponent_mode or "model"
        journals[opponent] = log.with_name(f"{opponent}-{log.name}")

    try:
        mailboxes = {}
        if args.dota_log is not None:
            for side, mode in selected_modes.items():
                if mode != "shadow":
                    name = team_mailbox_name(side) if args.both else MAILBOX_NAME
                    mailboxes[side] = LuaMailbox(local_bots_path(args.game) / name)
        paths = [*journals.values(), *(box.path for box in mailboxes.values())]
        if args.dota_log is not None:
            paths.append(args.dota_log)
        if len({path.resolve() for path in paths}) != len(paths):
            parser.error("Console input, team journals and team reply files must use distinct paths")
        telemetry = ConsoleTelemetry(args.dota_log) if args.dota_log is not None else None

        with ExitStack() as stack:
            services, servers = {}, {}
            for side, mode in selected_modes.items():
                journal_path = journals[side]
                journal_path.parent.mkdir(parents=True, exist_ok=True)
                output = stack.enter_context(journal_path.open("a", encoding="utf-8", buffering=1))
                coach = (
                    ModelCoach(model=args.model, effort=args.effort, timeout=args.model_timeout,
                               directory=journal_path.parent / f"session-{side}", team=side, opponent=side != args.team)
                    if mode == "model" else None
                )
                if coach is not None:
                    stack.callback(coach.close)
                    prepared = coach.prepare()
                    print(f"{side} session ready: {prepared['id']}", flush=True)
                player_side = side == args.team
                service = CoachService(
                    team=side, mode=mode, cooldown_s=args.cooldown, max_calls=args.max_calls,
                    coach=coach, journal=_journal(output, side), decision_interval_s=args.decision_interval,
                    accept_player_calls=player_side and (args.both or args.caller_id is not None),
                    caller_id=args.caller_id if player_side else None,
                    opponent_pressure=not player_side,
                )
                services[side] = service
                servers[side] = stack.enter_context(make_server(service, ports[side]))

            if args.install:
                installed = install(
                    args.team, args.port, workshop=args.workshop, game=args.game,
                    log_telemetry=telemetry is not None, file_commands=bool(mailboxes),
                    opponent_port=opponent_port, caller_id=args.caller_id,
                )
                print(f"Installed Open Hyper AI adapter in {installed}", flush=True)
            for mailbox in mailboxes.values():
                if not mailbox.path.is_file():
                    parser.error("Install the file-command adapter with --install before using this mode")
            for side, service in services.items():
                print(f"{side} coach ready at http://127.0.0.1:{ports[side]} ({service.mode})", flush=True)
                print(f"{side} journal: {journals[side].resolve()}", flush=True)
            if telemetry is not None:
                print(f"Dota console telemetry: {telemetry.path.resolve()}", flush=True)
            _watch(services, servers, args.team, telemetry, mailboxes, args)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
