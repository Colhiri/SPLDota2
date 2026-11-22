"""Use the Local League role roster in the staged Open Hyper AI scripts."""

from pathlib import Path
import re


LEAGUE_ROLES_NAME = "team_coach_league_roles.lua"
_HELPER_NAME = "team_coach_league.lua"
_LOAD = b'local leagueOk, league = pcall(require, GetScriptDirectory() .. "/team_coach_league")\n'
_CONFIGURED = b"""
local function leagueConfigured()
    if not leagueOk then return false end
    local ok, configured = pcall(league.configured)
    return ok and configured
end
"""


def _hook(contents, name, pattern, replacement):
    source, count = re.subn(pattern, replacement, contents)
    if count != 1:
        raise ValueError(f"Expected one league role hook in {name}, found {count}")
    return source


def _selection(path, source):
    role = re.search(rb"local (?P<module>\w+)\s*=\s*require\(GetScriptDirectory\(\)\s*\.\.\s*['\"]/FuncLib/systems/role['\"]\)", source)
    if not role:
        raise ValueError(f"Expected the Open Hyper AI role module in {path.name}")
    assignment = role["module"] + b".RoleAssignment"
    source = _hook(
        source, path.name, rb"\bfunction UpdateLaneAssignments\(\)",
        b"""function UpdateLaneAssignments()
    if leagueOk then
        local ok, lanes = pcall(league.lanes, GetTeam(), """ + assignment + b""")
        if ok and lanes then return lanes end
    end
""",
    )
    source = _hook(
        source, path.name, rb"\bfunction Think\(\)",
        b"""function Think()
    if leagueOk then
        pcall(league.lanes, GetTeam(), """ + assignment + b""")
        local ok, picked = pcall(league.pick, GetTeam())
        if ok and picked then return end
    end
""",
    )
    return _hook(
        source, path.name, rb"\band not IsLanMode\(\)then(?=\s+\w+\.ShufflePickOrder\()",
        b"and not IsLanMode() and not leagueConfigured() then",
    )


def _positions(path, source):
    for name in (b"GetRoleFromId", b"GetPosition"):
        source = _hook(
            source, path.name, rb"\b" + name + rb"\s*=\s*function\((?P<bot>\w+)\)",
            lambda match: match[0] + f"""
    local unit = {match['bot'].decode('ascii')}
    if leagueOk and unit and unit:GetTeam() == GetTeam() then
        local ok, role = pcall(league.position, GetTeam(), unit:GetPlayerID())
        if ok and role then
            unit.assignedRole = role
            return role
        end
    end
""".encode("ascii"),
        )
    return source


def _assigned_lane(path, source):
    return _hook(
        source, path.name,
        rb"(?P<start>function CDOTA_Bot_Script:GetAssignedLane\(\).*?return LANE_NONE\s+end;)\s*(?=return \w+\(self\)end;)",
        lambda match: match["start"] + b"""
    if leagueOk then
        local ok, lane = pcall(league.lane, self)
        if ok and lane then return lane end
    end
""",
    )


def _human_position(path, source):
    return _hook(
        source, path.name, rb"\bfunction \w+\.IsPosxHuman\((?P<role>\w+)\)",
        lambda match: match[0] + b"""
    if leagueOk then
        local ok, human = pcall(league.isHumanPosition, GetTeam(), """ + match["role"] + b""")
        if ok and human ~= nil then return human end
    end
""",
    )


def _laning(path, source):
    return _hook(
        source, path.name,
        rb"\bif (?P<condition>\w+ or \w+\.GetPosition\(\w+\)==1 and \w+\.IsPosxHuman\(5\))then\s+function Think\(\)",
        lambda match: b"if leagueConfigured() or " + match["condition"] + b" then function Think()\n"
        b"    if leagueOk then\n"
        b'        local ok, ready = pcall(league.microReady, GetBot(), "lane")\n'
        b"        if ok and not ready then return end\n"
        b"    end\n",
    )


def _micro_usage(path, source):
    # This Workshop build runs hero spell decisions from ItemUsageThink as well.
    return _hook(
        source, path.name, rb"\bfunction ItemUsageThink\(\)",
        b"""function ItemUsageThink()
    if leagueOk then
        local ok, ready = pcall(league.microReady, GetBot(), "items_and_spells")
        if ok and not ready then return end
    end
""",
    )


def patch_league_roles(stage):
    helper = stage / _HELPER_NAME
    data = stage / LEAGUE_ROLES_NAME
    if helper.exists() or data.exists():
        raise ValueError("The Workshop source already contains Local League role files")
    modules = (
        ("hero_selection.lua", _selection, _CONFIGURED),
        ("FuncLib/systems/role.lua", _positions, b""),
        ("FuncLib/systems/global_overrides.lua", _assigned_lane, b""),
        ("FuncLib/general_utils/hero_info.lua", _human_position, b""),
        ("mode_laning_generic.lua", _laning, _CONFIGURED),
        ("ability_item_usage_generic.lua", _micro_usage, b""),
    )
    for name, patch, prelude in modules:
        path = stage / name
        path.write_bytes(_LOAD + prelude + patch(path, path.read_bytes()))
    helper.write_bytes(Path(__file__).with_name("league_roles.lua").read_bytes())
    data.write_text("return nil\n", encoding="ascii")
