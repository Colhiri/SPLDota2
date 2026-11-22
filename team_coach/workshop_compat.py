"""Repair known Lua API mismatches in the staged Open Hyper AI scripts."""

import re
from pathlib import Path


_DISPEL_UTILS = b'local dispelUtils = require(GetScriptDirectory() .. "/FuncLib/func_utils")\n'

_DISPEL_FIXES = (
    (
        b"M=z.GetModifierRemainingDuration(J)",
        b"M=dispelUtils.GetModifierTime(z,J)",
    ),
    (
        b"R and R:IsTrained()and not R.IsPassive()and R.IsCooldownReady()",
        b"R and R:IsTrained()and not R:IsPassive()and R:IsCooldownReady()",
    ),
    (
        b"if not P.IsBot()or P.GetPosition and P.GetPosition()<=2 then return true end;"
        b"local T=P.GetHealth()/P.GetMaxHealth()",
        b"if not P:IsBot()or dispelUtils.GetPosition(P)<=2 then return true end;"
        b"local T=P:GetHealth()/P:GetMaxHealth()",
    ),
    (b"if a1 and a1.IsFullyCastable()then", b"if a1 and a1:IsFullyCastable()then"),
    (b"if not a3 or not a3.IsFullyCastable()then", b"if not a3 or not a3:IsFullyCastable()then"),
    (
        b"if a9.IsFacingLocation(a6:GetLocation(),30)then",
        b"if a9:IsFacingLocation(a6:GetLocation(),30)then",
    ),
    (
        b"if not a1 or not a1.IsFullyCastable()then return-1 end;local ac=P.GetIncomingTrackingProjectiles()",
        b"if not a1 or not a1:IsFullyCastable()then return-1 end;local ac=P:GetIncomingTrackingProjectiles()",
    ),
)

# Last-seen enemies are player IDs. Target inspection needs visible hero handles.
_DEFEND_FIXES = (
    (
        b"for aq,d7 in ipairs(cN)do if Z.IsValidHero(d7)and Z.GetHP(d7)<0.8",
        b"for aq,d7 in ipairs(Z.GetEnemiesNearLoc(cM,2500))do if Z.IsValidHero(d7)and Z.GetHP(d7)<0.8",
    ),
    (
        b"for aq,bV in ipairs(Z.GetLastSeenEnemiesNearLoc(ds,du))do do local dv;repeat if not Z.IsValidHero(bV)",
        b"for aq,bV in ipairs(Z.GetEnemiesNearLoc(ds,du))do do local dv;repeat if not Z.IsValidHero(bV)",
    ),
)

_LANING_FIXES = (
    (
        b"local X=0;local function Y(Z)",
        b"""local laneAttackTarget, laneAttackDeadline, laneLastAttackTime
local function laneAttackPending(bot)
    if laneAttackTarget and GameTime() < laneAttackDeadline and b.IsValid(laneAttackTarget)
        and not bot:IsDisarmed()
        and bot:GetLastAttackTime() == laneLastAttackTime then
        return true
    end
    laneAttackTarget = nil
    return false
end

local function laneAttackOnce(bot, creep)
    local now = GameTime()
    local swing = bot:GetAttackPoint() / math.max(bot:GetAttackSpeed(), 0.01)
    local lastAttack = bot:GetLastAttackTime()
    local recovery = math.max(0, bot:GetSecondsPerAttack() - swing - (now - lastAttack))
    laneAttackTarget, laneAttackDeadline, laneLastAttackTime = creep, now + recovery + swing + 0.2, lastAttack
    bot:SetTarget(creep)
    bot:Action_AttackUnit(creep, true)
end

local X=0;local function Y(Z)""",
    ),
    (
        b"local _=e:GetNearbyTowers(1200,true)if Y(h)then",
        b"if b.CanNotUseAction(e) then return end;local _=e:GetNearbyTowers(1200,true)if Y(h)then",
    ),
    (
        b"local C=GetBestLastHitCreep(i)if b.IsValid(C)then if b.IsValidBuilding(_[1])",
        b"if laneAttackPending(e) then return end;"
        b"local C=not e:IsDisarmed() and GetBestLastHitCreep(i) or nil;"
        b"if b.IsValid(C)then if b.IsValidBuilding(_[1])",
    ),
    (
        b"local V=N(O)if b.WillKillTarget(O,R,DAMAGE_TYPE_PHYSICAL,U)",
        b"local V=N(O)+(b.IsInRange(e,O,m) and 3 or 0);if b.WillKillTarget(O,R,DAMAGE_TYPE_PHYSICAL,U)",
    ),
    (b"e:SetTarget(C)e:Action_AttackUnit(C,false)", b"laneAttackOnce(e,C)"),
    (
        b"O:GetHealth()<=n and b.IsInRange(e,O,m+150)",
        b"b.IsInRange(e,O,m) and b.WillKillTarget(O,e:GetAttackDamage()-3,DAMAGE_TYPE_PHYSICAL,"
        b"b.GetAttackProDelayTime(e,O))",
    ),
    (
        b"local aa=GetBestDenyCreep(h)if b.IsValid(aa)then e:SetTarget(aa)e:Action_AttackUnit(aa,true)",
        b"local aa=not e:IsDisarmed() and GetBestDenyCreep(h) or nil;"
        b"if b.IsValid(aa)then laneAttackOnce(e,aa)",
    ),
)

_WINDRANGER_FIXES = (
    (
        b"local J;function a.SkillsComplement()",
        b"function a.SkillsComplement()",
    ),
    (
        b"if#P>=2 and a.IsBeingAttackedByRealHero(b)and J<0.7",
        b"if#P>=2 and a.IsBeingAttackedByRealHero(b)and c.GetHP(b)<0.7",
    ),
)


_SKILL_POINTS_CHECK = re.compile(
    rb"\bif\s*#(?P<skills>\w+)\s*>=\s*1\s+and\s+"
    rb"(?P<bot>\w+):GetAbilityPoints\(\)\s*>\s*0\s+then\b"
)


def _patch_talent_progression(stage: Path) -> None:
    usage = stage / "ability_item_usage_generic.lua"
    if not usage.is_file():
        return
    original = usage.read_bytes()
    checks = list(_SKILL_POINTS_CHECK.finditer(original))
    if len(checks) != 1:
        return
    check = checks[0]
    hook = b"\nleagueSkillProgression.learn(" + check["bot"] + b", " + check["skills"] + b")\n"
    load = b'local leagueSkillProgression = require(GetScriptDirectory() .. "/team_coach_skill_progression")\n'
    module = Path(__file__).with_name("skill_progression.lua")
    (stage / "team_coach_skill_progression.lua").write_bytes(module.read_bytes())
    usage.write_bytes(load + original[:check.start()] + hook + original[check.start():])


def patch_workshop_compat(stage: Path) -> None:
    """Apply unambiguous matches and leave other Workshop versions alone."""
    _patch_talent_progression(stage)
    for relative, fixes, require_all in (
        ("FuncLib/systems/dispel.lua", _DISPEL_FIXES, False),
        ("FuncLib/systems/defend.lua", _DEFEND_FIXES, False),
        ("mode_laning_generic.lua", _LANING_FIXES, True),
        ("BotsLib/hero_windrunner.lua", _WINDRANGER_FIXES, True),
        ("Customize/general.lua", ((b"Customize.ThinkLess = 1;", b"Customize.ThinkLess = 0;"),), True),
    ):
        path = stage / relative
        if not path.is_file():
            continue
        original = path.read_bytes()
        if require_all and any(original.count(old) != 1 for old, _ in fixes):
            continue
        updated = original
        for old, new in fixes:
            if updated.count(old) == 1:
                updated = updated.replace(old, new)
        if updated != original:
            if path.name == "dispel.lua" and b"dispelUtils." in updated and _DISPEL_UTILS not in updated:
                updated = _DISPEL_UTILS + updated
            path.write_bytes(updated)
