"""Stage and install a local Open Hyper AI copy with the team coach adapter."""

from argparse import ArgumentParser
import os
from pathlib import Path
import re
from secrets import token_hex
import shutil
from stat import FILE_ATTRIBUTE_REPARSE_POINT
from tempfile import TemporaryDirectory

from .console_telemetry import MAILBOX_NAME
from .league_roles import patch_league_roles
from .workshop_compat import patch_workshop_compat


WORKSHOP_ID = "3246316298"
MARKER = ".team-coach-install"
MARKER_CONTENT = f"team-coach:open-hyper-ai:{WORKSHOP_ID}\n"
ADAPTER_NAME = "team_coach_adapter.lua"
FIGHT_ADAPTER_NAME = "team_coach_fight.lua"
CONFIG_NAME = "team_coach_config.lua"
USAGE_FILE = "ability_item_usage_generic.lua"
LANES = ("top", "mid", "bot")
OBJECTIVES = ("push", "defend")
LOAD_ADAPTER = 'local teamCoachOk, teamCoach = pcall(require, GetScriptDirectory() .. "/team_coach_adapter")\n'


def _linked(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _check_source_entries(directory, names):
    for name in names:
        if _linked(Path(directory, name)):
            raise ValueError(f"Workshop copy contains a link or reparse point in {directory}: {name}")
    return []


def _check_target(target):
    if not os.path.lexists(target):
        return
    if _linked(target) or not target.is_dir():
        raise ValueError(f"Destination is not a team coach directory: {target}")
    marker = target / MARKER
    if not marker.is_file() or _linked(marker) or marker.read_text(encoding="utf-8") != MARKER_CONTENT:
        raise ValueError(f"Destination already contains a different bot install: {target}")


def _replace_once(path, pattern, replacement):
    contents = path.read_bytes()
    result, replacements = re.subn(pattern, replacement, contents)
    if replacements != 1:
        raise ValueError(f"Expected one Open Hyper AI hook in {path.name}, found {replacements}")
    path.write_bytes(result)


def _patch_command_modes(stage):
    hooks = {
        "farm": rb"(?=function OnStart\(\))",
        "retreat": rb"(?=if SafeCall then)",
        "roshan": rb"(?=local function F\(\))",
    }
    for action, hook in hooks.items():
        path = stage / f"mode_{action}_generic.lua"
        wrapper = f"""
local teamCoachStockDesire = GetDesire
function GetDesire()
    local baseline = teamCoachStockDesire()
    if teamCoachOk then
        local ok, desire = pcall(teamCoach.biasCommand, GetBot(), "{action}", baseline)
        if ok then return desire end
    end
    return baseline
end
""".encode()
        _replace_once(path, hook, wrapper)
        path.write_bytes(LOAD_ADAPTER.encode() + path.read_bytes())


def _patch_ultimate_holds(stage):
    overrides = stage / "FuncLib" / "systems" / "global_overrides.lua"
    _replace_once(
        overrides,
        rb"(?=\breturn\s+\w+\s*\Z)",
        ("\n" + LOAD_ADAPTER).encode() + b"""
if teamCoachOk then pcall(teamCoach.installUltimateOrders, CDOTA_Bot_Script) end
""",
    )
    usage = stage / USAGE_FILE
    _replace_once(
        usage,
        rb"local (?P<ok>\w+)\s*,\s*(?P<err>\w+)\s*=\s*pcall\((?P<callback>\w+\.SkillsComplement)\)",
        lambda match: (
            b"local " + match["ok"] + b"," + match["err"] + b"\n"
            b"if teamCoachUsageOk then\n"
            + match["ok"] + b"," + match["err"] + b" = teamCoachUsage.runSkills(GetBot(), "
            + match["callback"] + b")\n"
            b"else\n"
            + match["ok"] + b"," + match["err"] + b" = pcall(" + match["callback"] + b")\n"
            b"end\n"
        ),
    )
    usage.write_bytes(
        b'local teamCoachUsageOk, teamCoachUsage = pcall(require, GetScriptDirectory() .. "/team_coach_adapter")\n'
        + usage.read_bytes()
    )

    # Let these existing decision hooks continue considering other spells.
    hooks = {
        "lina": ("lina_laguna_blade", ("ConsiderR",)),
        "enigma": ("enigma_black_hole", ("ConsiderBlinkPulseHole", "ConsiderBlinkHole", "ConsiderBlackHole")),
    }
    helper = b"""
local function teamCoachHolds(bot, ability)
    if not teamCoachOk then return false end
    local ok, held = pcall(teamCoach.holdsUltimate, bot, ability)
    return ok and held
end
"""
    for hero, (ability, decisions) in hooks.items():
        path = stage / "BotsLib" / f"hero_{hero}.lua"
        gate = f' and not teamCoachHolds(GetBot(), "{ability}") then'.encode()
        for decision in decisions:
            pattern = (
                rf"(?P<check>(?P<result>\w+)\s*,\s*\w+\s*=\s*\w+\.{decision}\(\)"
                r"\s*if\s+(?P=result)\s*>\s*0)\s+then"
            ).encode()
            _replace_once(path, pattern, lambda match: match["check"] + gate)
        path.write_bytes(LOAD_ADAPTER.encode() + helper + path.read_bytes())


def _patch_fight_mode(stage):
    path = stage / "mode_team_roam_generic.lua"
    wrapper = b"""
local teamCoachStockRoamDesire, teamCoachStockRoamThink = GetDesire, Think
function GetDesire()
    local baseline = teamCoachStockRoamDesire()
    if teamCoachOk then
        local ok, desire = pcall(teamCoach.fightDesire, GetBot(), baseline)
        if ok then return desire end
    end
    return baseline
end

function Think()
    if teamCoachOk then
        local ok, handled = pcall(teamCoach.fightThink, GetBot())
        if ok and handled then return end
    end
    return teamCoachStockRoamThink()
end
"""
    exports = rb"(?=\b(?P<mode>\w+)\.GetDesire\s*=\s*GetDesire\s*;?\s*(?P=mode)\.Think\s*=\s*Think)"
    _replace_once(path, exports, lambda _: wrapper)
    path.write_bytes(LOAD_ADAPTER.encode() + path.read_bytes())


def _patch_chat(stage):
    _replace_once(
        stage / USAGE_FILE,
        rb"InstallChatCallback\(function\((?P<event>\w+)\)(?P<stock>\w+\.SetReplyHumanTime)\((?P=event)\)end\)",
        lambda match: (
            b"local teamCoachChatBot = GetBot()\nInstallChatCallback(function(" + match["event"] + b")\n"
            b"    if teamCoachUsageOk and teamCoachUsage.onChat(teamCoachChatBot, " + match["event"]
            + b") then return end\n    " + match["stock"] + b"(" + match["event"] + b")\nend)"
        ),
    )


def _patch_scripts(stage):
    _replace_once(
        stage / USAGE_FILE,
        rb"function AbilityUsageThink\(\)\s*HandleIdleBotState\((?P<bot>\w+)\)",
        lambda match: (
            ("\n" + LOAD_ADAPTER).encode()
            + b"function AbilityUsageThink()\n"
            + b"    if teamCoachOk then pcall(teamCoach.tick, " + match["bot"] + b") end\n"
            + b"    HandleIdleBotState(" + match["bot"] + b")\n"
        ),
    )
    for objective in OBJECTIVES:
        for lane in LANES:
            path = stage / f"mode_{objective}_tower_{lane}_generic.lua"
            function = f"Get{objective.title()}Desire"
            lane_enum = f"LANE_{lane.upper()}"
            pattern = rf"return (?P<call>\w+\.{function}\((?P<bot>\w+),\s*{lane_enum}\))".encode()
            _replace_once(
                path, pattern,
                lambda match: (
                    b"\n    local baseline = " + match["call"] + b"\n"
                    + b"    if teamCoachOk then\n"
                    + b"        local ok, desire = pcall(teamCoach.bias, " + match["bot"]
                    + f', "{objective}", "{lane}", baseline)\n'.encode()
                    + b"        if ok then return desire end\n"
                    + b"    end\n    return baseline\n"
                ),
            )
            path.write_bytes(LOAD_ADAPTER.encode() + path.read_bytes())
    _patch_command_modes(stage)
    _patch_ultimate_holds(stage)
    _patch_fight_mode(stage)
    _patch_chat(stage)


def _steamapps():
    return Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam" / "steamapps"


def local_bots_path(game=None):
    game_root = Path(game) if game is not None else _steamapps() / "common/dota 2 beta"
    return game_root / "game/dota/scripts/vscripts/bots"


def install(
    team="radiant", port=32125, *, workshop=None, game=None, target=None,
    log_telemetry=False, file_commands=False, opponent_port=None, caller_id=None,
):
    """Install into Dota, or into an explicit target directory for staging."""
    if team not in {"radiant", "dire"} or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Choose a valid team and TCP port")
    if opponent_port is not None and (
        type(opponent_port) is not int or not 1 <= opponent_port <= 65535 or opponent_port == port
    ):
        raise ValueError("Choose a separate valid opponent TCP port")
    if caller_id is not None and (type(caller_id) is not int or not 0 <= caller_id <= 23):
        raise ValueError("Choose a valid player ID in 0..23")
    if game is not None and target is not None:
        raise ValueError("Choose either a Dota installation or an explicit destination")
    if file_commands and not log_telemetry:
        raise ValueError("File commands require console telemetry")
    source = Path(workshop) if workshop is not None else _steamapps() / "workshop/content/570" / WORKSHOP_ID
    destination = Path(target) if target is not None else local_bots_path(game)
    if not source.is_dir() or _linked(source) or not (source / USAGE_FILE).is_file():
        raise ValueError(f"Open Hyper AI bot scripts were not found in {source}")
    source = source.resolve()
    destination = destination.parent.resolve(strict=True) / destination.name
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("The Workshop source and destination must be separate directories")
    _check_target(destination)
    dual_mailboxes = ("team_coach_replies_radiant.lua", "team_coach_replies_dire.lua")

    with TemporaryDirectory(prefix=".team-coach-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "bots"
        shutil.copytree(source, stage, ignore=_check_source_entries)
        for name in (ADAPTER_NAME, FIGHT_ADAPTER_NAME, CONFIG_NAME, MAILBOX_NAME, *dual_mailboxes, MARKER):
            if (stage / name).exists():
                raise ValueError(f"Workshop source already contains {name}")
        _patch_scripts(stage)
        patch_league_roles(stage)
        patch_workshop_compat(stage)
        shutil.copyfile(Path(__file__).with_name("adapter.lua"), stage / ADAPTER_NAME)
        shutil.copyfile(Path(__file__).with_name("fight_adapter.lua"), stage / FIGHT_ADAPTER_NAME)
        options = [f'team = "{team}"', f"port = {port}"]
        if opponent_port is not None:
            options.append(f"opponent_port = {opponent_port}")
        if caller_id is not None:
            options.append(f"caller_id = {caller_id}")
        if log_telemetry:
            options.append("log_telemetry = true")
        if file_commands:
            options.append("file_commands = true")
            for name in dual_mailboxes if opponent_port is not None else (MAILBOX_NAME,):
                (stage / name).write_text("return {}\n", encoding="ascii")
        (stage / CONFIG_NAME).write_text(
            "return { " + ", ".join(options) + " }\n", encoding="utf-8",
        )
        (stage / MARKER).write_text(MARKER_CONTENT, encoding="utf-8")
        _check_target(destination)
        backup = None
        if destination.exists():
            backup = destination.with_name(f"{destination.name}.team-coach-backup-{token_hex(4)}")
            destination.rename(backup)
        try:
            stage.rename(destination)
        except OSError:
            if backup is not None and not os.path.lexists(destination):
                backup.rename(destination)
            raise
    return destination


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--team", choices=("radiant", "dire"), default="radiant")
    parser.add_argument("--port", type=int, default=32125)
    parser.add_argument("--opponent-port", type=int, help="Enable coaching for the other side on this TCP port")
    parser.add_argument("--caller-id", type=int, help="Restrict commands to a specific allied player ID")
    parser.add_argument("--log-telemetry", action="store_true", help="Write snapshots to the Dota console log")
    parser.add_argument("--file-commands", action="store_true", help="Read local replies along with --log-telemetry")
    parser.add_argument("--workshop", type=Path, help="Path to the Open Hyper AI Workshop bot scripts")
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--game", type=Path, help="Path to the Dota 2 installation")
    destination.add_argument("--target", type=Path, help="Explicit bots destination for staging")
    args = parser.parse_args()
    try:
        print(install(**vars(args)))
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
