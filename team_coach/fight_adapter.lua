local fight = {}

local tactics = { pickoff = true, flank = true, bait = true, counterattack = true, gank = true }
local phases = { stage = true, probe = true, reset = true, commit = true }
local stageDesire = math.min(0.58, (BOT_MODE_DESIRE_VERYHIGH or 0.6) - 0.01)
local commitDesire = math.min(0.73, (BOT_MODE_DESIRE_ABSOLUTE or 0.7) - 0.01)
local towerSafety = 900
local openingSpells = {
    axe_berserkers_call = "area", beastmaster_primal_roar = "unit", crystal_maiden_frostbite = "unit",
    doom_bringer_doom = "unit", lion_voodoo = "unit", skeleton_king_hellfire_blast = "unit",
}
local controlUltimates = {
    npc_dota_hero_doom_bringer = "doom_bringer_doom",
    npc_dota_hero_beastmaster = "beastmaster_primal_roar",
}
local damageUltimates = {
    npc_dota_hero_lina = "lina_laguna_blade",
    npc_dota_hero_lion = "lion_finger_of_death",
    npc_dota_hero_sniper = "sniper_assassinate",
}
local enemyTowers = {
    TOWER_TOP_1, TOWER_TOP_2, TOWER_TOP_3, TOWER_MID_1, TOWER_MID_2,
    TOWER_MID_3, TOWER_BOT_1, TOWER_BOT_2, TOWER_BOT_3, TOWER_BASE_1, TOWER_BASE_2,
}
local rolesLoaded, heroRoles = pcall(function()
    return require(GetScriptDirectory() .. "/FuncLib/data/hero_roles_map").HeroRolesMap
end)
local leagueLoaded, league = pcall(require, GetScriptDirectory() .. "/team_coach_league")

local function optional(read, ...)
    local ok, value = pcall(read, ...)
    if ok then return value end
end

local function macroStrength(order)
    if order.source == "manual" or order.source == "player_call" then return 100 end
    local settings = leagueLoaded and optional(league.tuning)
    return settings and settings.macro or 100
end

local function validNumber(value, minimum, maximum)
    return type(value) == "number" and value >= minimum and value <= maximum
end

local function playerID(value)
    return validNumber(value, 0, 23) and value % 1 == 0
end

local function health(hero)
    return math.min(1, math.max(0, hero:GetHealth() / math.max(1, hero:GetMaxHealth())))
end

local function visibleHero(hero)
    return hero ~= nil and hero:IsAlive() and hero:CanBeSeen()
        and not hero:HasModifier("modifier_arc_warden_tempest_double")
        and playerID(hero:GetPlayerID()) and GetSelectedHeroName(hero:GetPlayerID()) == hero:GetUnitName()
end

local stock
local function targetableEnemy(bot, enemy)
    if not visibleHero(enemy) or enemy:GetTeam() == bot:GetTeam() or enemy:GetTeam() ~= GetOpposingTeam() then
        return false
    end
    local activeBot = optional(GetBot)
    if not activeBot or activeBot:GetTeam() ~= bot:GetTeam() then
        return false
    end
    if stock == nil then
        stock = optional(require, GetScriptDirectory() .. "/FuncLib/func_utils") or false
    end
    return stock and optional(stock.IsSuspiciousIllusion, enemy) == false
end

local function bkbActive(hero)
    return hero:HasModifier("modifier_black_king_bar_immune")
end

local function abilityName(ability)
    if ability and ability:IsUltimate() and not ability:IsPassive() and not ability:IsHidden()
        and ability:GetLevel() > 0 then
        local name = ability:GetName()
        if type(name) == "string" and #name <= 80 and name:match("^[%w_]+$") then
            return name
        end
    end
end

local function activeUltimate(hero)
    if hero:IsCastingAbility() or hero:IsChanneling() or hero:IsUsingAbility() then
        return abilityName(hero:GetCurrentActiveAbility())
    end
end

local function readyOpening(bot, name)
    local ability = bot:GetAbilityByName(name)
    if ability and not ability:IsHidden() and ability:GetLevel() > 0 and ability:IsFullyCastable() then
        return ability
    end
end

function fight.readyOpeners(bot)
    local result = {}
    for name in pairs(openingSpells) do
        if readyOpening(bot, name) then result[#result + 1] = name end
    end
    table.sort(result)
    return result
end

local function supportPrior(hero)
    local role = rolesLoaded and type(heroRoles) == "table" and heroRoles[hero:GetUnitName()]
    if role and type(role.support) == "number" and type(role.carry) == "number" then
        return role.support >= 2 and role.carry <= 1
    end
end

local function visibleEnemies(bot, teamVision)
    local byID, seen, ambiguous, nearby = {}, {}, {}, {}
    local enemies = teamVision and optional(GetUnitList, UNIT_LIST_ENEMY_HEROES)
        or bot:GetNearbyHeroes(1600, true, BOT_MODE_NONE)
    for _, enemy in pairs(enemies or {}) do
        if visibleHero(enemy) and enemy:GetTeam() ~= bot:GetTeam() then
            local id = enemy:GetPlayerID()
            if seen[id] then ambiguous[id] = true end
            seen[id] = true
            if targetableEnemy(bot, enemy) then
                byID[id] = enemy
            end
            nearby[#nearby + 1] = enemy
        end
    end
    for id in pairs(ambiguous) do byID[id] = nil end
    return byID, nearby
end

function fight.read(order, botID, clock)
    if type(order) ~= "table" or type(order.id) ~= "string" or #order.id > 64
        or not order.id:match("^[%w_%-]+$") or not tactics[order.tactic] or not phases[order.phase]
        or not playerID(order.target_id) or not playerID(order.anchor_id)
        or not validNumber(order.expires_clock_s, clock, clock + 45) or order.expires_clock_s == clock
        or type(order.bot_ids) ~= "table" or #order.bot_ids < 2 or #order.bot_ids > 5
        or (order.priority_target ~= nil and type(order.priority_target) ~= "boolean")
        or (order.source ~= nil and order.source ~= "local" and order.source ~= "manual"
            and order.source ~= "player_call" and order.source ~= "model") then
        return
    end
    local members, count = {}, 0
    for index, id in pairs(order.bot_ids) do
        if not validNumber(index, 1, #order.bot_ids) or index % 1 ~= 0
            or not playerID(id) or id == order.target_id or members[id] then
            return
        end
        members[id] = index
        count = count + 1
    end
    local opener = order.opener
    if opener ~= nil and (type(opener) ~= "table" or not playerID(opener.bot_id)
        or not members[opener.bot_id] or not openingSpells[opener.ability]) then return end
    if count == #order.bot_ids and members[botID] and members[order.anchor_id] then
        order.members = members
        return order
    end
end

local function distance(a, b)
    local x, y = a.x - b.x, a.y - b.y
    return math.sqrt(x * x + y * y)
end

local function fraction(from, to, scale)
    return Vector(from.x + (to.x - from.x) * scale, from.y + (to.y - from.y) * scale, to.z)
end

local function travelPoint(from, to)
    local length = distance(from, to)
    if length > 820 then return fraction(from, to, 820 / length) end
    return to
end

local function towerDanger(from, goal)
    local dx, dy = goal.x - from.x, goal.y - from.y
    local lengthSquared = dx * dx + dy * dy
    local enemyTeam = GetOpposingTeam()
    for _, index in pairs(enemyTowers) do
        local tower = GetTower(enemyTeam, index)
        if tower and tower:IsAlive() then
            local origin = tower:GetLocation()
            if distance(goal, origin) < towerSafety then return true end
            if lengthSquared > 0 then
                local projection = ((origin.x - from.x) * dx + (origin.y - from.y) * dy) / lengthSquared
                if projection >= 0 and projection <= 1
                    and distance(fraction(from, goal, projection), origin) < towerSafety then
                    return true
                end
            end
        end
    end
    return false
end

local function enemySafety(point)
    local ancient = GetAncient(GetOpposingTeam())
    return (ancient ~= nil and distance(point, ancient:GetLocation()) < 1900) or towerDanger(point, point)
end

function fight.observe(bot, pressure)
    local origin = bot:GetLocation()
    local enemies = {}
    for id, enemy in pairs(visibleEnemies(bot, pressure)) do
        if #enemies >= 5 then break end
        local location = enemy:GetLocation()
        local enemyHP = health(enemy)
        if validNumber(location.x, -10000, 10000) and validNumber(location.y, -10000, 10000)
            and validNumber(enemyHP, 0, 1) then
            local sighting = {
                id = id, x = math.floor(location.x), y = math.floor(location.y), hp = enemyHP,
                level = enemy:GetLevel(), bkb = bkbActive(enemy),
                support = supportPrior(enemy), ultimate = optional(activeUltimate, enemy),
            }
            if pressure then
                sighting.under_tower = optional(enemySafety, location)
                sighting.role = leagueLoaded and optional(league.position, enemy:GetTeam(), id) or nil
                sighting.kills = optional(GetHeroKills, id)
                sighting.deaths = optional(GetHeroDeaths, id)
            end
            enemies[#enemies + 1] = sighting
        end
    end
    if validNumber(origin.x, -10000, 10000) and validNumber(origin.y, -10000, 10000) then
        return {x = math.floor(origin.x), y = math.floor(origin.y), enemies = enemies}
    end
end

local function routeClear(from, goal, throughFog)
    local midpoint = fraction(from, goal, 0.5)
    return (throughFog or (IsLocationVisible(goal) and IsLocationVisible(midpoint)))
        and IsLocationPassable(goal) and IsLocationPassable(midpoint) and not towerDanger(from, goal)
end

local function supportNear(bot, order, destination)
    local count, seen = 0, {}
    for _, ally in pairs(bot:GetNearbyHeroes(1600, false, BOT_MODE_NONE) or {}) do
        local id = ally:GetPlayerID()
        if ally ~= bot and order.members[id] and id ~= bot:GetPlayerID() and not seen[id]
            and visibleHero(ally) and ally:GetTeam() == bot:GetTeam() and not ally:IsIllusion()
            and (order.tactic ~= "gank" or ally:GetActiveMode() ~= BOT_MODE_RETREAT)
            and health(ally) >= 0.55 and distance(ally:GetLocation(), destination) <= 1100 then
            seen[id] = true
            count = count + 1
        end
    end
    return count
end

local function opponentCount(enemies, destination, radius)
    local count = 0
    for _, enemy in pairs(enemies) do
        if distance(enemy:GetLocation(), destination) <= radius then count = count + 1 end
    end
    return count
end

local function movement(from, destination, throughFog)
    if distance(from, destination) <= 120 then return {kind = "wait"} end
    local point = travelPoint(from, destination)
    if routeClear(from, point, throughFog) then return {kind = "move", point = point} end
end

local function nearAncientSide(target, home, depth, lateral)
    local dx, dy = home.x - target.x, home.y - target.y
    local magnitude = math.sqrt(dx * dx + dy * dy)
    if magnitude < 100 then return end
    local x, y = dx / magnitude, dy / magnitude
    return Vector(target.x + depth * x - lateral * y, target.y + depth * y + lateral * x, target.z)
end

local function calculate(bot, order)
    local gank = order.tactic == "gank"
    local byID, enemies = visibleEnemies(bot, gank)
    local enemy = byID[order.target_id]
    if not enemy then return end
    local from, target = bot:GetLocation(), enemy:GetLocation()
    local distanceToTarget = distance(from, target)
    local ancient = GetAncient(bot:GetTeam())
    if not ancient or not IsLocationVisible(target) then return end
    local home = ancient:GetLocation()
    if order.phase == "reset" then
        if distanceToTarget >= 1050 then return {kind = "wait"} end
        local point = nearAncientSide(target, home, 1050, 0)
        if point and distance(point, target) > distanceToTarget then
            return movement(from, point)
        end
        return
    end

    local support = supportNear(bot, order, target)
    local nearbyOpponents = opponentCount(enemies, target, 950)
    if gank and (enemySafety(target) or enemy:IsInvulnerable() or bkbActive(enemy)
        or nearbyOpponents > #order.bot_ids - 1) then return end
    local isProbe = order.phase == "probe" and bot:GetPlayerID() == order.anchor_id
    if order.phase == "commit" or isProbe then
        local minimumHP = isProbe and 0.76 or 0.62
        local minimumSupport = gank and #order.bot_ids - 1 or 1
        if health(bot) < minimumHP or support < minimumSupport or nearbyOpponents > support + 1
            or distanceToTarget > (isProbe and 1100 or 1300) or enemy:IsInvulnerable() or bkbActive(enemy)
            or optional(activeUltimate, enemy) then
            return
        end
        if distanceToTarget <= bot:GetAttackRange() + 75 and not enemy:IsAttackImmune()
            and routeClear(from, target) then
            return {kind = "attack", target = enemy}
        end
        local depth = isProbe and math.max(450, math.min(760, bot:GetAttackRange())) or 260
        if gank then depth = math.min(260, bot:GetAttackRange() * 0.5) end
        local point = nearAncientSide(target, home, depth, 0)
        if point and distance(point, target) < distanceToTarget then
            local action = movement(from, point)
            if action then action.target = enemy end
            return action
        end
        if gank then return {kind = "wait", target = enemy} end
        return
    end

    if health(bot) < 0.68 or opponentCount(enemies, from, 550) > support then return end
    local player = bot:GetPlayerID()
    local index = order.members[player]
    if order.opener and order.phase == "stage" and order.tactic ~= "flank" and distanceToTarget <= 950
        and routeClear(from, target) then return {kind = "wait"} end
    if gank then
        if distanceToTarget <= 950 and routeClear(from, target) then return {kind = "wait"} end
        if bot:WasRecentlyDamagedByAnyHero(2) then return end
        local lateral = player == order.anchor_id and 0 or (index % 2 == 0 and 385 or -385)
        local stage = nearAncientSide(target, home, 780, lateral)
        if stage then
            return movement(from, stage, distanceToTarget > 1500) or movement(from, target)
        end
        return
    end
    local lateral = 0
    if order.tactic == "flank" and player ~= order.anchor_id then
        lateral = index % 2 == 0 and 640 or -640
    end
    local stage = nearAncientSide(target, home, order.tactic == "flank" and 720 or 850, lateral)
    if stage and opponentCount(enemies, stage, 550) == 0 then return movement(from, stage) end
end

local function candidate(bot, state)
    local order = state.fight
    if not order or macroStrength(order) == 0
        or order.expires_clock_s <= DotaTime() or GetGameState() ~= GAME_STATE_GAME_IN_PROGRESS
        or not bot:IsHero() or not bot:IsAlive()
        or bot:IsInvulnerable() or bot:IsIllusion() or bot:IsStunned() or bot:IsRooted()
        or bot:IsCastingAbility() or bot:IsUsingAbility() or bot:IsChanneling()
        or bot:GetActiveMode() == BOT_MODE_RETREAT or bot:GetActiveMode() == BOT_MODE_ROSHAN
        or bot:GetActiveMode() == BOT_MODE_DEFEND_TOWER_TOP
        or bot:GetActiveMode() == BOT_MODE_DEFEND_TOWER_MID
        or bot:GetActiveMode() == BOT_MODE_DEFEND_TOWER_BOT
        or (order.tactic ~= "gank" and bot:GetActiveMode() == BOT_MODE_ATTACK
            and bot:GetActiveModeDesire() >= BOT_MODE_DESIRE_HIGH)
        or health(bot) < 0.52
        or (order.phase == "stage" and health(bot) < 0.75 and bot:WasRecentlyDamagedByAnyHero(1.5))
        or (order.phase ~= "reset" and bot:WasRecentlyDamagedByTower(1.5)) then
        return
    end
    local now = GameTime()
    local cached = state.fightCandidate
    if cached and cached.order == order and now - cached.at < 0.2 + (100 - macroStrength(order)) * 0.012 then
        return cached.action
    end
    local action = calculate(bot, order)
    state.fightCandidate = {order = order, at = now, action = action}
    return action
end

function fight.desire(bot, state, baseline)
    local desire = state.fight and state.fight.phase == "commit" and commitDesire or stageDesire
    if baseline >= desire then return baseline end
    local action = candidate(bot, state)
    if not action then return baseline end
    if (state.fight.tactic == "gank" or state.fight.opener) and (
        (state.fight.phase == "stage" and action.kind == "wait")
        or (bot:GetActiveMode() == BOT_MODE_ATTACK and bot:GetActiveModeDesire() >= BOT_MODE_DESIRE_HIGH)) then
        return baseline
    end
    return baseline + (desire - baseline) * macroStrength(state.fight) / 100
end

function fight.ready(bot, state)
    local order = state.fight
    if not order or (order.phase ~= "stage" and order.phase ~= "reset") then return end
    local action = candidate(bot, state)
    if action and action.kind == "wait" then
        return {id = order.id, phase = order.phase}
    end
end

function fight.reserveUltimate(bot, state)
    local order = state.fight
    if not order or order.tactic ~= "bait" or (order.phase ~= "probe" and order.phase ~= "reset")
        or order.anchor_id == bot:GetPlayerID() or health(bot) < 0.72
        or bot:WasRecentlyDamagedByAnyHero(2) or bot:WasRecentlyDamagedByTower(2) then
        return false
    end
    return candidate(bot, state) ~= nil
end

-- Give the hero script the gank target only during its ability decision.
function fight.focus(bot, state)
    local order = state.fight
    if not order or (order.tactic ~= "gank" and not order.opener) or order.phase ~= "commit"
        or bot:GetActiveMode() ~= BOT_MODE_TEAM_ROAM then return end
    local action = candidate(bot, state)
    local enemy = action and action.target
    if enemy and targetableEnemy(bot, enemy) and not enemy:IsInvulnerable()
        and not bkbActive(enemy) and not enemySafety(enemy:GetLocation()) then return enemy end
end

function fight.openingSpell(bot, state, enemy)
    local order = state.fight
    local opener = order.opener
    if not opener or bot:GetPlayerID() ~= opener.bot_id or stock.CanNotUseAbility(bot)
        or (state.openerAttempt and state.openerAttempt.id == order.id)
        or not stock.CanCastOnNonMagicImmune(enemy) or stock.IsDisabled(enemy) then return end
    local ability = readyOpening(bot, opener.ability)
    if not ability then return end
    local mode = openingSpells[opener.ability]
    local range = mode == "area" and ability:GetSpecialValueInt("radius") or ability:GetCastRange()
    if range <= 0 or distance(bot:GetLocation(), enemy:GetLocation()) > range - 20 then return end
    if mode == "unit" and not stock.CanCastOnTargetAdvanced(enemy) then return end
    return ability, mode
end

function fight.markOpener(state, ability)
    state.openerAttempt = {id = state.fight.id, ability = ability, at = GameTime()}
end

function fight.appliedOpener(state, clock)
    local order, attempt = state.fight, state.openerAttempt
    if order and order.opener and attempt and attempt.id == order.id and order.expires_clock_s > clock
        and attempt.ability:GetCooldownTimeRemaining() > 0.25 then
        return {id = order.id, ability = order.opener.ability}
    end
end

function fight.gankUltimate(bot, state, enemy)
    if not state.fight.priority_target or stock.CanNotUseAbility(bot)
        or not stock.CanCastOnNonMagicImmune(enemy) or not stock.CanCastOnTargetAdvanced(enemy)
        or stock.IsHaveAegis(enemy) or enemy:HasModifier("modifier_oracle_false_promise_timer")
        or enemy:HasModifier("modifier_dazzle_shallow_grave") then return end
    local hero = bot:GetUnitName()
    local control = controlUltimates[hero]
    local name = control or damageUltimates[hero]
    local ability = name and bot:GetAbilityByName(name)
    if not abilityName(ability) or not ability:IsFullyCastable()
        or distance(bot:GetLocation(), enemy:GetLocation()) > ability:GetCastRange() then return end
    if control then
        if health(enemy) >= 0.25 and not stock.IsDisabled(enemy) and not enemy:HasModifier("modifier_doom_bringer_doom")
            and not enemy:HasModifier("modifier_beastmaster_primal_roar") then return ability end
        return
    end
    local damage = ability:GetSpecialValueInt("damage")
    if damage <= 0 then return end
    local incoming = enemy:GetActualIncomingDamage(damage, DAMAGE_TYPE_MAGICAL)
    local hp = enemy:GetHealth()
    local attack = enemy:GetActualIncomingDamage(bot:GetAttackDamage(), DAMAGE_TYPE_PHYSICAL)
    if incoming >= hp * 1.10 and (hp > attack * 2 or GetUnitToUnitDistance(bot, enemy) > bot:GetAttackRange()) then
        return ability
    end
end

function fight.think(bot, state)
    -- Keep the queued combo intact while the engine is in the roam mode.
    local order, now = state.fight, GameTime()
    if order and (order.tactic == "gank" or order.opener) and bot:NumQueuedActions() > 0 then return true end
    local opener = state.openerAttempt
    if order and opener and opener.id == order.id and now - opener.at < 0.35 then return true end
    local action = candidate(bot, state)
    if not action then return false end
    if action.kind == "wait" then return order.tactic ~= "gank" and not order.opener end
    local previous = state.fightAction
    local samePhase = previous and previous.id == order.id
        and previous.phase == order.phase and previous.targetID == order.target_id
    local interval = (action.kind == "attack" and 0.9 or 0.45) + (100 - macroStrength(order)) * 0.006
    if samePhase and now - previous.at < interval then return true end
    if action.kind == "attack" then
        local enemy = action.target
        if not targetableEnemy(bot, enemy) or enemy:IsInvulnerable() or enemy:IsAttackImmune() or bkbActive(enemy)
            or distance(bot:GetLocation(), enemy:GetLocation()) > bot:GetAttackRange() + 75 then
            return false
        end
        if order.tactic ~= "gank" or bot:GetAttackTarget() ~= enemy then bot:Action_AttackUnit(enemy, true) end
    else
        bot:Action_MoveToLocation(action.point)
    end
    state.fightAction = {id = order.id, phase = order.phase, targetID = order.target_id, at = now}
    return true
end

function fight.applied(state, clock)
    local order, effect = state.fight, state.fightAction
    if order and effect and order.id == effect.id and order.phase == effect.phase
        and order.target_id == effect.targetID
        and clock < order.expires_clock_s and GameTime() - effect.at <= 4 then
        return {id = effect.id, phase = effect.phase}
    end
end

return fight
