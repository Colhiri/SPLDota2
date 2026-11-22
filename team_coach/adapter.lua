local scriptDir = GetScriptDirectory()
local config = require(scriptDir .. "/team_coach_config")
local json = require(scriptDir .. "/ts_libs/utils/json")
local fightLoaded, fight = pcall(require, scriptDir .. "/team_coach_fight")
local leagueLoaded, league = pcall(require, scriptDir .. "/team_coach_league")

local coach = {}
local selectedTeam = config.team == "dire" and TEAM_DIRE or TEAM_RADIANT
local teamNames = { [TEAM_RADIANT] = "radiant", [TEAM_DIRE] = "dire" }
local dualTeam = type(config.opponent_port) == "number"
local logTelemetry = config.log_telemetry == true
local fileCommands = logTelemetry and config.file_commands == true
local observeOnly = logTelemetry and not fileCommands
local interval = 2
local requestTimeout = 6
local maxPlanSeconds = 60
local maxBoostedDesire = math.min(0.72, BOT_MODE_DESIRE_HIGH - 0.01)
local maxRetreatDesire = math.min(0.86, (BOT_MODE_DESIRE_VERYHIGH or 0.9) - 0.01)
local commandOrder = { "retreat", "roshan", "farm", "hold_ultimate" }
local commandModes = { farm = BOT_MODE_FARM, retreat = BOT_MODE_RETREAT, roshan = BOT_MODE_ROSHAN }
local commandMaxSeconds = { farm = 30, retreat = 15, roshan = 30, hold_ultimate = 10 }
local commandFields = { id = true, action = true, target = true, bot_ids = true, expires_clock_s = true, source = true }
local orderSources = { ["local"] = true, model = true, manual = true, player_call = true }
local lanes = {
    top = { index = LANE_TOP, towers = { TOWER_TOP_1, TOWER_TOP_2, TOWER_TOP_3 },
        push = BOT_MODE_PUSH_TOWER_TOP, defend = BOT_MODE_DEFEND_TOWER_TOP },
    mid = { index = LANE_MID, towers = { TOWER_MID_1, TOWER_MID_2, TOWER_MID_3 },
        push = BOT_MODE_PUSH_TOWER_MID, defend = BOT_MODE_DEFEND_TOWER_MID },
    bot = { index = LANE_BOT, towers = { TOWER_BOT_1, TOWER_BOT_2, TOWER_BOT_3 },
        push = BOT_MODE_PUSH_TOWER_BOT, defend = BOT_MODE_DEFEND_TOWER_BOT },
}
local modes = {
    [BOT_MODE_NONE] = "idle", [BOT_MODE_LANING] = "lane", [BOT_MODE_FARM] = "farm",
    [BOT_MODE_RETREAT] = "retreat", [BOT_MODE_ATTACK] = "attack", [BOT_MODE_ROSHAN] = "roshan",
    [BOT_MODE_TEAM_ROAM] = "roam",
}
local function teamPort(team)
    if team == selectedTeam then
        return config.port
    end
    if dualTeam and teamNames[team] then
        return config.opponent_port
    end
end

for lane, mode in pairs(lanes) do
    modes[mode.push] = "push_" .. lane
    modes[mode.defend] = "defend_" .. lane
end

local function clampFraction(value)
    return math.max(0, math.min(1, value))
end

local function health(unit)
    return clampFraction(unit:GetHealth() / math.max(1, unit:GetMaxHealth()))
end

local function zone(location)
    local closest = "other"
    local distance = 1800
    for name, lane in pairs(lanes) do
        local projection = GetAmountAlongLane(lane.index, location)
        if projection and projection.distance < distance then
            closest, distance = name, projection.distance
        end
    end
    return closest
end

local function townPortalReady(bot)
    local item = bot:GetItemInSlot(15)
    return item ~= nil and item:IsFullyCastable()
end

local function optional(read, ...)
    local ok, value = pcall(read, ...)
    if ok then
        return value
    end
end

local function tuning()
    local value = leagueLoaded and optional(league.tuning)
    return value or {bot_difficulty = 4, micro = 100, macro = 100, coach = 100}
end

local function ownsMap(botId, team, alive)
    local known = { [botId] = true }
    local function preferred(member)
        if not member or member:GetTeam() ~= team or not member:IsHero() or not member:IsBot()
            or member:IsIllusion() then
            return false
        end
        local id = member:GetPlayerID()
        if type(id) ~= "number" or id < 0 or id > 23 or id % 1 ~= 0 or known[id]
            or member:GetUnitName() ~= GetSelectedHeroName(id) then
            return false
        end
        local otherAlive = member:IsAlive()
        if type(otherAlive) ~= "boolean" then
            return false
        end
        known[id] = true
        return (otherAlive and not alive) or (otherAlive == alive and id < botId)
    end

    for slot = 1, 5 do
        local member = optional(GetTeamMember, slot)
        if member and optional(preferred, member) then
            return false
        end
    end
    if UNIT_LIST_ALLIED_HEROES ~= nil then
        for _, member in pairs(optional(GetUnitList, UNIT_LIST_ALLIED_HEROES) or {}) do
            if optional(preferred, member) then
                return false
            end
        end
    end
    for _, playerId in pairs(optional(GetTeamPlayers, team) or {}) do
        if not known[playerId] and optional(IsPlayerBot, playerId) then
            local otherAlive = optional(IsHeroAlive, playerId)
            if type(otherAlive) == "boolean"
                and ((otherAlive and not alive) or (otherAlive == alive and playerId < botId)) then
                return false
            end
        end
    end
    return true
end

local function firstTower(team, lane)
    for tier, index in ipairs(lane.towers) do
        local tower = GetTower(team, index)
        if tower ~= nil and tower:IsAlive() then
            return tier, tower
        end
    end
    return 0, nil
end

local function enemiesOnMap(enemyTeam)
    local enemies, locations = {}, {}
    for _, id in pairs(GetTeamPlayers(enemyTeam)) do
        local hero = GetSelectedHeroName(id)
        if hero ~= nil and hero ~= "" then
            local alive = IsHeroAlive(id)
            local sightings = alive and GetHeroLastSeenInfo(id)
            local seen = sightings and sightings[1]
            local report = { id = id, hero = hero, alive = alive, zone = "other" }
            if seen and seen.location and type(seen.time_since_seen) == "number" then
                report.seen_s = math.floor(math.max(0, seen.time_since_seen) * 10) / 10
                if report.seen_s <= 15 then
                    report.zone = zone(seen.location)
                end
                if report.seen_s <= 5 then
                    locations[#locations + 1] = seen.location
                end
            end
            enemies[#enemies + 1] = report
        end
    end
    return enemies, locations
end

local function countNearby(locations, tower)
    if tower == nil then
        return 0
    end
    local origin = tower:GetLocation()
    local count = 0
    for _, location in ipairs(locations) do
        local dx, dy = origin.x - location.x, origin.y - location.y
        if dx * dx + dy * dy <= 1400 * 1400 then
            count = count + 1
        end
    end
    return count
end

local baseChecks = {}
local function ancientThreat(team)
    local now = GameTime()
    local cached = baseChecks[team]
    if cached and now >= cached.at and now - cached.at < 0.35 then
        return cached.ancient
    end
    local ancient = GetAncient(team)
    local threat
    if ancient and ancient:IsAlive() then
        local home = ancient:GetLocation()
        for _, id in pairs(GetTeamPlayers(GetOpposingTeam())) do
            local latest = IsHeroAlive(id) and GetHeroLastSeenInfo(id)
            local seen = latest and latest[1]
            if seen and seen.time_since_seen <= 2.5 and seen.location then
                local dx, dy = seen.location.x - home.x, seen.location.y - home.y
                if dx * dx + dy * dy <= 2450 * 2450 then
                    threat = ancient
                    break
                end
            end
        end
    end
    local since = threat and cached and cached.ancient == threat and cached.since or now
    baseChecks[team] = {at = now, ancient = threat, since = since}
    return threat
end

local function laneCreepLocations(list)
    local locations = {}
    for _, creep in pairs(GetUnitList(list)) do
        local name = creep:GetUnitName()
        if creep:IsAlive() and creep:CanBeSeen() and (name:find("^npc_dota_creep_goodguys_")
            or name:find("^npc_dota_creep_badguys_") or name == "npc_dota_goodguys_siege"
            or name == "npc_dota_badguys_siege") then
            locations[#locations + 1] = creep:GetLocation()
        end
    end
    return locations
end

local function livingAllyLocation(ally)
    if ally:IsAlive() then
        return ally:GetLocation()
    end
end

local function towerPosition(tower)
    local location = tower:GetLocation()
    return {x = math.floor(location.x), y = math.floor(location.y)}
end

local function mapSnapshot(team, enemyTeam)
    local enemies, enemyLocations = enemiesOnMap(enemyTeam)
    local allyCreeps = laneCreepLocations(UNIT_LIST_ALLIED_CREEPS)
    local enemyCreeps = laneCreepLocations(UNIT_LIST_ENEMY_CREEPS)
    local allyLocations = {}
    for member = 1, 5 do
        local ally = optional(GetTeamMember, member)
        local location = ally and optional(livingAllyLocation, ally)
        if location then
            allyLocations[#allyLocations + 1] = location
        end
    end
    local front = {}
    for name, lane in pairs(lanes) do
        local ownTier, ownTower = firstTower(team, lane)
        local enemyTier, enemyTower = firstTower(enemyTeam, lane)
        front[name] = {
            ally_front = clampFraction(GetLaneFrontAmount(team, lane.index, false)),
            enemy_front = clampFraction(GetLaneFrontAmount(enemyTeam, lane.index, false)),
            own_tower = ownTier, enemy_tower = enemyTier,
            own_tower_hp = ownTower and health(ownTower),
            enemy_tower_hp = enemyTower and enemyTower:CanBeSeen() and health(enemyTower) or nil,
            own_tower_pos = ownTower and towerPosition(ownTower),
            enemy_tower_pos = enemyTower and towerPosition(enemyTower),
            enemy_near_own = countNearby(enemyLocations, ownTower),
            ally_near_enemy = countNearby(allyLocations, enemyTower),
            enemy_near_enemy = countNearby(enemyLocations, enemyTower),
            ally_wave_near_enemy = math.min(30, countNearby(allyCreeps, enemyTower)),
            enemy_wave_near_own = math.min(30, countNearby(enemyCreeps, ownTower)),
        }
    end
    return {lanes = front, enemies = enemies, ancient_threat = ancientThreat(team) ~= nil, tuning = tuning()}
end

local function recentDesires(state)
    local scores
    for key, sample in pairs(state.desires) do
        if state.lastTick - sample.at <= interval * 2 then
            scores = scores or {}
            scores[key] = sample.value
        end
    end
    return scores
end

local function recordDesire(state, key, value)
    if state == nil or state.desires == nil then
        return
    end
    if type(value) == "number" and value >= 0 and value <= 1 then
        local sample = state.desires[key] or {}
        sample.value = value
        sample.at = state.lastTick
        state.desires[key] = sample
    else
        state.desires[key] = nil
    end
end

local function activeCommand(state, action, clock)
    local command = state and state.commands and state.commands[action]
    if command and command.expires_clock_s > clock then
        return command
    end
end

local function recordEffect(state, command, clock)
    state.commandEffects = state.commandEffects or {}
    state.commandEffects[command.action] = { id = command.id, clock = clock }
end

local function appliedCommands(state, activeMode, clock)
    local ids
    for _, action in ipairs(commandOrder) do
        local effect = state.commandEffects and state.commandEffects[action]
        local command = effect and activeCommand(state, action, clock)
        if command and effect.id == command.id and clock - effect.clock <= interval * 2
            and (action == "hold_ultimate" or activeMode == commandModes[action]) then
            ids = ids or {}
            ids[#ids + 1] = command.id
        end
    end
    return ids
end

local function humanIds(team)
    local ids = {}
    for _, id in pairs(GetTeamPlayers(team)) do
        if not IsPlayerBot(id) then
            ids[#ids + 1] = id
        end
    end
    table.sort(ids)
    return ids
end

local function authorizedHuman(team)
    if team ~= selectedTeam then
        return nil
    end
    local ids = humanIds(team)
    local id = config.caller_id or (#ids == 1 and ids[1])
    if type(id) ~= "number" or GetTeamForPlayer(id) ~= team then
        return nil
    end
    for _, memberId in ipairs(ids) do
        if memberId == id then
            for slot = 1, 5 do
                local member = GetTeamMember(slot)
                if member and member:GetPlayerID() == id and not member:IsBot() then
                    return member
                end
            end
        end
    end
end

local callWords = {
    ["!пуш"] = true, ["!рош"] = true, ["!отмена"] = true, ["!coach"] = true,
    ["!push"] = true, ["!rosh"] = true, ["!cancel"] = true,
}

local function recordChat(bot, event, text)
    local teamOnly = event.team_only
    if (teamOnly ~= true and teamOnly ~= 1) or #text > 160
        or GetGameState() ~= GAME_STATE_GAME_IN_PROGRESS or bot:GetTeam() ~= selectedTeam then
        return
    end
    local id = event.player_id or event.playerid
    if type(id) ~= "number" or id < 0 or id > 23 or id % 1 ~= 0 then
        return
    end
    local clock = DotaTime()
    local hash = 0
    for index = 1, #text do
        hash = (hash * 131 + string.byte(text, index)) % 2147483647
    end
    local eventId = ("chat-%d-%d-%x"):format(id, math.floor(clock * 100 + 0.5), hash)
    local calls = bot.teamCoachCalls or {}
    for _, call in ipairs(calls) do
        if call.id == eventId then
            return
        end
    end
    if #calls >= 4 then
        table.remove(calls, 1)
    end
    calls[#calls + 1] = { id = eventId, player_id = id, source = "chat", clock_s = clock, text = text }
    bot.teamCoachCalls = calls
end

function coach.onChat(bot, event)
    local text = type(event) == "table" and (event.string or event.text)
    local word = type(text) == "string" and text:match("^%s*(%S+)")
    if not word or not callWords[word:gsub("[A-Z]", string.lower)] then
        return false
    end
    pcall(recordChat, bot, event, text)
    return true
end

local nativeUtilities
local function stockUtilities()
    if nativeUtilities == nil then
        nativeUtilities = optional(require, scriptDir .. "/FuncLib/func_utils") or false
    end
    if nativeUtilities then
        return nativeUtilities
    end
end

local function roshanObservation(bot)
    local stock = stockUtilities()
    if not stock then
        return nil
    end
    local units
    if UNIT_LIST_NEUTRAL_CREEPS ~= nil then
        units = GetUnitList(UNIT_LIST_NEUTRAL_CREEPS)
    else
        units = bot:GetNearbyNeutralCreeps(1600)
    end
    local visible = false
    for _, unit in pairs(units or {}) do
        if unit:GetUnitName() == "npc_dota_roshan" and unit:CanBeSeen() and unit:IsAlive() then
            visible = true
            break
        end
    end
    return { visible = visible, estimated_alive = visible or stock.IsRoshanAlive() == true }
end

local function pingCall(team, clock)
    local human = authorizedHuman(team)
    local ping = human and human:GetMostRecentPing()
    if not ping or not ping.location or type(ping.time) ~= "number" or ping.time <= 0 then
        return nil
    end
    local age = GameTime() - ping.time
    local stock = stockUtilities()
    if not stock or age < 0 or age > 3 then
        return nil
    end
    local text
    if ping.normal_ping == true then
        local pit = stock.GetCurrentRoshanLocation()
        if pit and stock.GetDistance(ping.location, pit) < 600 then
            text = "!rosh"
        end
    elseif ping.normal_ping == false then
        local found, lane = stock.IsPingCloseToValidTower(GetOpposingTeam(), ping, 700, 5)
        if found then
            for name, info in pairs(lanes) do
                if info.index == lane then
                    text = "!push " .. name
                    break
                end
            end
        end
    end
    if text ~= nil then
        local id = human:GetPlayerID()
        return {
            id = ("ping-%d-%d"):format(id, math.floor(ping.time * 1000 + 0.5)),
            player_id = id, source = "ping", clock_s = clock - age, text = text,
        }
    end
end

local function playerCalls(bot, clock, ownsBoard)
    local calls = {}
    local fresh = {}
    local pending = bot.teamCoachCalls or {}
    local human = #pending > 0 and optional(authorizedHuman, bot:GetTeam())
    local id = human and human:GetPlayerID()
    for _, call in ipairs(pending) do
        if clock >= call.clock_s and clock - call.clock_s <= 6 then
            if call.player_id == id then
                calls[#calls + 1] = call
            end
            fresh[#fresh + 1] = call
        end
    end
    bot.teamCoachCalls = fresh
    local ping = ownsBoard and optional(pingCall, bot:GetTeam(), clock)
    if ping then
        calls[#calls + 1] = ping
    end
    if #calls > 0 then
        return calls
    end
end

local function manaFraction(bot)
    return clampFraction(bot:GetMana() / math.max(1, bot:GetMaxMana()))
end

local function nearbyHeroes(bot, enemy)
    local count = 0
    for _, hero in pairs(bot:GetNearbyHeroes(1300, enemy, BOT_MODE_NONE) or {}) do
        if hero ~= bot and hero:CanBeSeen() and hero:IsAlive() then
            count = count + 1
        end
    end
    return math.min(24, count)
end

local function recentHeroDamage(bot)
    return bot:WasRecentlyDamagedByAnyHero(2)
end

local function activeModeDesire(bot)
    return clampFraction(bot:GetActiveModeDesire())
end

local function learnedUltimate(ability)
    return ability ~= nil and ability:IsUltimate() and not ability:IsPassive()
        and not ability:IsHidden() and ability:GetLevel() > 0
end

local function activeToggle(ability)
    return type(ability.IsToggle) == "function" and ability:IsToggle()
        and type(ability.GetToggleState) == "function" and ability:GetToggleState() == true
end

local function ultimateObservation(bot, slot, activeAbility)
    local ability = bot:GetAbilityInSlot(slot)
    if not learnedUltimate(ability) then
        return nil
    end
    local name, level = ability:GetName(), ability:GetLevel()
    local cooldown = ability:GetCooldownTimeRemaining()
    if type(name) ~= "string" or name == "" or type(cooldown) ~= "number"
        or not (cooldown >= 0 and cooldown < math.huge) then
        return nil
    end
    return {
        name = name, level = level, cooldown_s = math.floor(cooldown * 10) / 10,
        active = activeAbility == ability or activeToggle(ability),
    }
end

local function ultimateObservations(bot)
    if type(bot.GetAbilityInSlot) ~= "function" then
        return nil
    end
    local activeAbility
    if bot:IsCastingAbility() or bot:IsUsingAbility() or bot:IsChanneling() then
        activeAbility = bot:GetCurrentActiveAbility()
    end
    local observations = {}
    local seen = {}
    for slot = 0, 23 do
        local observation = optional(ultimateObservation, bot, slot, activeAbility)
        if observation and not seen[observation.name] then
            seen[observation.name] = true
            observations[#observations + 1] = observation
        end
    end
    return observations
end

local function snapshot(bot, state, clock)
    local team = bot:GetTeam()
    local id = bot:GetPlayerID()
    local activeMode = bot:GetActiveMode()
    local alive = bot:IsAlive()
    local role = bot.assignedRole
    if type(role) ~= "number" or role < 1 or role > 5 or role % 1 ~= 0 then
        role = 0
    end
    local data = {
        team = teamNames[team], clock_s = clock,
        reply_team = dualTeam and true or nil,
        bot = {
            id = id, hero = bot:GetUnitName(), role = role, alive = alive, hp = health(bot),
            difficulty = optional(function() return bot:GetDifficulty() end),
            level = optional(function() return bot:GetLevel() end),
            zone = alive and zone(bot:GetLocation()) or "other",
            mode = modes[activeMode] or "other",
            tp_ready = alive and townPortalReady(bot),
            desires = recentDesires(state),
            nearby_allies = alive and optional(nearbyHeroes, bot, false) or nil,
            nearby_enemies = alive and optional(nearbyHeroes, bot, true) or nil,
            under_fire = optional(recentHeroDamage, bot),
            mana = optional(manaFraction, bot),
            mode_desire = optional(activeModeDesire, bot),
            ultimates = optional(ultimateObservations, bot),
        },
    }
    local ownsBoard = ownsMap(id, team, alive)
    if ownsBoard then
        data.map = mapSnapshot(team, GetOpposingTeam())
        data.map.human_ids = optional(humanIds, team)
        data.map.roshan = optional(roshanObservation, bot)
    end
    data.calls = playerCalls(bot, clock, ownsBoard)
    if fightLoaded and alive then
        data.bot.fight = optional(fight.observe, bot, dualTeam and team ~= selectedTeam)
        data.bot.tactical_openers = optional(fight.readyOpeners, bot)
        data.applied_fight = fight.applied(state, clock)
        data.applied_opener = optional(fight.appliedOpener, state, clock)
        if not activeCommand(state, "retreat", clock) then
            data.fight_ready = optional(fight.ready, bot, state)
        end
    end
    local plan = state.plan
    local rotation = state.defenseRotation
    if alive and plan and plan.expires_clock_s > clock and rotation and rotation.id == plan.id
        and activeMode == BOT_MODE_TEAM_ROAM and clock - rotation.at <= interval * 2 then
        data.applied = {plan_id = plan.id, objective = plan.objective, lane = plan.lane, rotation = true}
    elseif alive and plan and plan.expires_clock_s > clock and state.biasedPlan == plan.id
        and clock - state.biasedAt <= interval * 2
        and activeMode == lanes[plan.lane][plan.objective] then
        data.applied = { plan_id = plan.id, objective = plan.objective, lane = plan.lane }
    end
    data.applied_commands = alive and appliedCommands(state, activeMode, clock) or nil
    return data
end

local function knownFields(value, fields)
    if type(value) ~= "table" then
        return false
    end
    for key in pairs(value) do
        if not fields[key] then
            return false
        end
    end
    return true
end

local function arraySize(value, limit)
    if type(value) ~= "table" or #value > limit then
        return nil
    end
    local count = 0
    for key in pairs(value) do
        if type(key) ~= "number" or key % 1 ~= 0 or key < 1 or key > #value then
            return nil
        end
        count = count + 1
    end
    if count == #value then
        return count
    end
end

local function assignedBot(ids, botId)
    if not arraySize(ids, 3) then
        return false
    end
    local seen = {}
    for _, id in ipairs(ids) do
        if type(id) ~= "number" or id < 0 or id > 23 or id % 1 ~= 0 or seen[id] then
            return false
        end
        seen[id] = true
    end
    return seen[botId] == true
end

local function commandTarget(command, bot)
    local target = command.target
    if command.action == "roshan" then
        return knownFields(target, { kind = true }) and target.kind == "roshan"
    end
    if commandModes[command.action] then
        return knownFields(target, { kind = true }) and target.kind == "self"
    end
    if command.action ~= "hold_ultimate" or not knownFields(target, { kind = true, name = true })
        or target.kind ~= "ability" or type(target.name) ~= "string" or #target.name > 80
        or not target.name:match("^[a-z][a-z0-9_]*$") then
        return false
    end
    local ability = bot:GetAbilityByName(target.name)
    return learnedUltimate(ability) and not activeToggle(ability)
end

local function readCommands(commands, bot, clock)
    local accepted, seen = {}, {}
    if not arraySize(commands, 8) then
        return accepted
    end
    local botId = bot:GetPlayerID()
    for _, command in ipairs(commands) do
        if knownFields(command, commandFields) and type(command.id) == "string"
            and #command.id <= 64 and command.id:match("^[%w_%-]+$") and not seen[command.id]
            and type(command.action) == "string" and commandMaxSeconds[command.action] and not accepted[command.action]
            and type(command.expires_clock_s) == "number"
            and command.expires_clock_s > clock and command.expires_clock_s <= clock + commandMaxSeconds[command.action]
            and (command.source == nil or orderSources[command.source])
            and assignedBot(command.bot_ids, botId) and commandTarget(command, bot) then
            seen[command.id] = true
            accepted[command.action] = command
        end
    end
    return accepted
end

local function receive(bot, state, response)
    local body = response
    if type(response) == "table" then
        if response.StatusCode and tonumber(response.StatusCode) ~= 200 then
            return
        end
        body = response.Body or response.body
    end
    if type(body) ~= "string" or #body > 4096 then
        return
    end
    local reply = json.decode(body)
    if type(reply) ~= "table" or type(reply.session_id) ~= "string" then
        return
    end
    if dualTeam and reply.team ~= teamNames[bot:GetTeam()] then
        return
    end
    if fileCommands then
        state.lastFileReply = state.lastTick
    end
    if state.session ~= reply.session_id then
        state.session = reply.session_id
        state.biasedPlan = nil
        state.commandEffects = nil
        state.fightAction = nil
    end
    state.plan = nil
    if not state.connected then
        state.connected = true
        local transport = fileCommands and "file exchange" or "localhost exchange"
        print("[team-coach] " .. transport .. " connected, player " .. bot:GetPlayerID())
    end
    local clock = DotaTime()
    state.commands = readCommands(reply.commands, bot, clock)
    state.fight = fightLoaded and not observeOnly and fight.read(reply.fight, bot:GetPlayerID(), clock) or nil
    local plan = reply.plan
    if type(plan) ~= "table" or type(plan.id) ~= "string"
        or (plan.objective ~= "push" and plan.objective ~= "defend")
        or not lanes[plan.lane] or type(plan.bot_ids) ~= "table"
        or type(plan.expires_clock_s) ~= "number"
        or (plan.source ~= nil and not orderSources[plan.source]) then
        return
    end
    if plan.expires_clock_s <= clock or plan.expires_clock_s > clock + maxPlanSeconds then
        return
    end
    if assignedBot(plan.bot_ids, bot:GetPlayerID()) then
        state.plan = plan
    end
end

local function pollFileReply(bot, state, now)
    if state.lastFileReply and now - state.lastFileReply >= requestTimeout then
        state.plan, state.commands, state.lastFileReply = nil, nil, nil
        state.fight = nil
    end
    if not state.fileRequest or now < state.nextFilePoll then
        return
    end
    if now > state.nextExchange then
        state.fileRequest = nil
        return
    end
    state.nextFilePoll = now + 0.4
    local mailbox = dualTeam and ("team_coach_replies_" .. teamNames[bot:GetTeam()]) or "team_coach_replies"
    local ok, replies = pcall(dofile, scriptDir .. "/" .. mailbox)
    local reply = ok and type(replies) == "table" and replies[bot:GetPlayerID()]
    if type(reply) == "table" and reply[1] == state.fileRequest and type(reply[2]) == "string" then
        state.fileRequest = nil
        pcall(receive, bot, state, reply[2])
    end
end

local function exchange(bot, state, clock)
    if logTelemetry then
        local packet = snapshot(bot, state, clock)
        if fileCommands then
            state.fileRequest = ("%d-%d"):format(RandomInt(0, 1000000000), state.requestId)
            state.nextFilePoll = state.lastTick + 0.4
            packet.request_id = state.fileRequest
        end
        local payload = json.encode(packet)
        local chunkSize = 600
        local count = math.ceil(#payload / chunkSize)
        for index = 1, count do
            local chunk = payload:sub((index - 1) * chunkSize + 1, index * chunkSize)
            print(("[team-coach-snapshot] %d/%d %s"):format(index, count, chunk))
        end
        return
    end
    if type(CreateHTTPRequest) ~= "function" then
        error("CreateHTTPRequest is unavailable in bot Lua")
    end
    local request = CreateHTTPRequest("http://127.0.0.1:" .. teamPort(bot:GetTeam()) .. "/exchange")
    if request == nil then
        error("CreateHTTPRequest returned no handle")
    end
    if request.SetHTTPRequestHeaderValue then
        request:SetHTTPRequestHeaderValue("Content-Type", "application/json")
    end
    request:SetHTTPRequestRawPostBody("application/json", json.encode(snapshot(bot, state, clock)))
    local requestId = state.requestId
    request:Send(function(response)
        if bot.teamCoachState ~= state or requestId ~= state.requestId then
            return
        end
        state.inFlightUntil = nil
        pcall(receive, bot, state, response)
    end)
end

function coach.tick(bot)
    if bot == nil or GetGameState() ~= GAME_STATE_GAME_IN_PROGRESS or not teamPort(bot:GetTeam())
        or not bot:IsHero() or bot:IsIllusion() or not IsPlayerBot(bot:GetPlayerID())
        or GetSelectedHeroName(bot:GetPlayerID()) ~= bot:GetUnitName() then
        return
    end
    local now = GameTime()
    local state = bot.teamCoachState
    if state == nil or now < state.lastTick then
        state = { nextExchange = now + (bot:GetPlayerID() % 5) * 0.2, requestId = 0 }
        bot.teamCoachState = state
    end
    state.desires = state.desires or {}
    state.lastTick = now
    if fileCommands then
        pollFileReply(bot, state, now)
    end
    if now < state.nextExchange or (state.inFlightUntil and now < state.inFlightUntil) then
        return
    end
    state.nextExchange = now + (state.fight and state.fight.opener and 1 or interval)
    if not logTelemetry then
        state.inFlightUntil = now + requestTimeout
    end
    state.requestId = state.requestId + 1
    local ok, err = pcall(exchange, bot, state, DotaTime())
    if not ok then
        state.inFlightUntil = nil
        if not state.warned then
            state.warned = true
            local transport = logTelemetry and "snapshot log" or "localhost exchange"
            print("[team-coach] " .. transport .. " failed: " .. tostring(err))
        end
    end
end

local function canApplyCommand(bot)
    return bot:IsAlive() and bot:IsHero() and not bot:IsIllusion() and teamPort(bot:GetTeam()) ~= nil
        and GetGameState() == GAME_STATE_GAME_IN_PROGRESS
        and not (bot:IsCastingAbility() or bot:IsUsingAbility() or bot:IsChanneling())
end

local function retreatNeeded(bot)
    return bot:GetActiveMode() == BOT_MODE_RETREAT or health(bot) <= 0.60
        or recentHeroDamage(bot) or bot:WasRecentlyDamagedByTower(2)
end

local function macroMovementSafe(bot, state, clock)
    if retreatNeeded(bot) or health(bot) < 0.65 or nearbyHeroes(bot, true) > 0
        or bot:GetActiveModeDesire() >= BOT_MODE_DESIRE_HIGH
        or (state.plan and state.plan.expires_clock_s > clock) then
        return false
    end
    local current = bot:GetActiveMode()
    for _, lane in pairs(lanes) do
        if current == lane.defend or current == lane.push then
            return false
        end
    end
    return true
end

function coach.biasCommand(bot, action, baseline)
    if logTelemetry then
        optional(coach.tick, bot)
    end
    local state = bot.teamCoachState
    recordDesire(state, action, baseline)
    if observeOnly or not state or type(baseline) ~= "number" or not (baseline > 0 and baseline < 1) then
        return baseline
    end
    local clock = DotaTime()
    local command = activeCommand(state, action, clock)
    if not command or not canApplyCommand(bot) then
        return baseline
    end
    local strength = (command.source == "local" or command.source == "model") and tuning().macro or 100
    if strength == 0 then return baseline end
    local cap
    if action == "retreat" and retreatNeeded(bot) then
        cap = maxRetreatDesire
    elseif not ancientThreat(bot:GetTeam()) and not activeCommand(state, "retreat", clock)
        and macroMovementSafe(bot, state, clock) then
        if action == "roshan" then
            cap = maxBoostedDesire
        elseif action == "farm" and not activeCommand(state, "roshan", clock)
            and bot:GetActiveMode() ~= BOT_MODE_ROSHAN then
            cap = maxBoostedDesire
        end
    end
    if not cap or baseline >= cap then
        return baseline
    end
    recordEffect(state, command, clock)
    if action == "roshan" then return baseline + (cap - baseline) * strength / 100 end
    return math.min(baseline + 0.14 * strength / 100, cap)
end

local function pendingUltimateHold(state)
    if not observeOnly and state and state.commands and state.commands.hold_ultimate then
        local command = activeCommand(state, "hold_ultimate", DotaTime())
        if command and (command.source ~= "local" and command.source ~= "model" or tuning().macro > 0) then
            return command
        end
    end
end

local function fightReservesUltimate(bot, state)
    return not observeOnly and fightLoaded and state and state.fight
        and not optional(ancientThreat, bot:GetTeam())
        and optional(fight.reserveUltimate, bot, state)
end

function coach.holdsUltimate(bot, ability)
    local state = bot and bot.teamCoachState
    local command = pendingUltimateHold(state)
    if observeOnly or not state or (not command and not state.fight) or ability == nil then
        return false
    end
    local name = type(ability) == "string" and ability or ability:GetName()
    local manual = command and name == command.target.name
    if not manual and not fightReservesUltimate(bot, state) then
        return false
    end
    if type(ability) == "string" then
        ability = bot:GetAbilityByName(name)
    end
    if not canApplyCommand(bot) or not IsPlayerBot(bot:GetPlayerID())
        or GetSelectedHeroName(bot:GetPlayerID()) ~= bot:GetUnitName()
        or bot:GetAbilityByName(name) ~= ability
        or not learnedUltimate(ability) or activeToggle(ability) then
        return false
    end
    if manual then
        recordEffect(state, command, DotaTime())
    end
    return true
end

-- Collect combat orders from one hero ability decision before committing its combo.
-- If holding the requested ultimate, avoid committing its Blink or BKB alone.
local function runStockSkills(bot, skills)
    local state = bot and bot.teamCoachState
    if not state or state.abilityOrders
        or (not pendingUltimateHold(state) and not fightReservesUltimate(bot, state)) then
        return pcall(skills)
    end
    local orders = {}
    state.abilityOrders = orders
    local ok, result = pcall(skills)
    state.abilityOrders = nil
    if not ok or orders.held then
        return ok, result
    end
    for _, action in ipairs(orders) do
        local sent, errorMessage = pcall(action[1], bot, unpack(action, 2, action.count))
        if not sent then
            return false, errorMessage
        end
    end
    return ok, result
end

function coach.runSkills(bot, skills)
    local state = bot and bot.teamCoachState
    local enemy = fightLoaded and not observeOnly and state and state.fight
        and not activeCommand(state, "retreat", DotaTime()) and not optional(ancientThreat, bot:GetTeam())
        and optional(fight.focus, bot, state)
    if not enemy then return runStockSkills(bot, skills) end
    local spell, mode
    local ok, first, kind = pcall(fight.openingSpell, bot, state, enemy)
    if ok then spell, mode = first, kind end
    if spell and not optional(coach.holdsUltimate, bot, spell) then
        return pcall(function()
            if mode == "area" then bot:Action_UseAbility(spell)
            else bot:Action_UseAbilityOnEntity(spell, enemy) end
            fight.markOpener(state, spell)
        end)
    end
    local ultimate = not state.fight.opener and optional(fight.gankUltimate, bot, state, enemy)
    if ultimate and not optional(coach.holdsUltimate, bot, ultimate) then
        return pcall(function() bot:Action_UseAbilityOnEntity(ultimate, enemy) end)
    end
    local previous = bot:GetTarget()
    bot:SetTarget(enemy)
    local ok, result = runStockSkills(bot, skills)
    if bot:GetTarget() == enemy then bot:SetTarget(previous) end
    return ok, result
end

function coach.installUltimateOrders(botClass)
    if observeOnly or botClass == nil then
        return
    end
    local function wrap(name, castsAbility)
        local original = botClass[name]
        if type(original) ~= "function" then
            return
        end
        botClass[name] = function(bot, ...)
            local state = bot and bot.teamCoachState
            local orders = state and state.abilityOrders
            if castsAbility and state and (state.fight or (state.commands and state.commands.hold_ultimate))
                and optional(coach.holdsUltimate, bot, ...) then
                if orders then
                    orders.held = true
                end
                return
            end
            if orders then
                orders[#orders + 1] = { original, count = select("#", ...) + 1, ... }
                return
            end
            return original(bot, ...)
        end
    end
    for _, prefix in ipairs({ "Action_", "ActionQueue_", "ActionPush_" }) do
        for _, suffix in ipairs({ "", "OnEntity", "OnLocation", "OnTree" }) do
            wrap(prefix .. "UseAbility" .. suffix, true)
        end
    end
    for _, name in ipairs({
        "Action_ClearActions", "Action_MoveToLocation", "Action_AttackUnit", "Action_AttackMove",
        "ActionQueue_Delay", "ActionQueue_AttackUnit",
    }) do
        wrap(name, false)
    end
end

function coach.bias(bot, objective, lane, baseline)
    if logTelemetry then
        optional(coach.tick, bot)
    end
    local state = bot.teamCoachState
    recordDesire(state, objective .. "_" .. lane, baseline)
    if objective == "push" and not observeOnly and teamPort(bot:GetTeam()) and ancientThreat(bot:GetTeam()) then
        return 0
    end
    local plan = state and state.plan
    if observeOnly or not plan or plan.objective ~= objective or plan.lane ~= lane
        or plan.expires_clock_s <= DotaTime() or baseline <= 0 or baseline >= maxBoostedDesire
        or not bot:IsAlive() or bot:IsChanneling() or health(bot) < 0.65
        or bot:WasRecentlyDamagedByAnyHero(2) or bot:WasRecentlyDamagedByTower(2) then
        return baseline
    end
    local strength = (plan.source == "manual" or plan.source == "player_call") and 100 or tuning().macro
    if strength == 0 then return baseline end
    local active = bot:GetActiveMode()
    if activeCommand(state, "retreat", DotaTime()) and retreatNeeded(bot) then
        return baseline
    end
    if active == BOT_MODE_RETREAT or bot:GetActiveModeDesire() >= BOT_MODE_DESIRE_HIGH then
        return baseline
    end
    local enemies = bot:GetNearbyHeroes(1100, true, BOT_MODE_NONE) or {}
    local allies = bot:GetNearbyHeroes(1100, false, BOT_MODE_NONE) or {}
    if #enemies > #allies + 1 then
        return baseline
    end
    state.biasedPlan = plan.id
    state.biasedAt = DotaTime()
    return math.min(baseline + 0.14 * strength / 100, maxBoostedDesire)
end

local function tacticalState(bot)
    if fightLoaded and not observeOnly and bot and teamPort(bot:GetTeam()) then
        local state = bot.teamCoachState
        if state and state.fight and not activeCommand(state, "retreat", DotaTime()) then
            return state
        end
    end
end

local function defendAncient(bot)
    if observeOnly or not bot or not canApplyCommand(bot)
        or bot:IsStunned() or bot:IsRooted() or bot:IsInvulnerable() or health(bot) < 0.38 then
        return nil
    end
    if bot:GetActiveMode() == BOT_MODE_RETREAT and (health(bot) < 0.55 or recentHeroDamage(bot)) then
        return nil
    end
    local strength = tuning().macro
    if strength == 0 then return nil end
    local team = bot:GetTeam()
    local ancient = ancientThreat(team)
    local check = baseChecks[team]
    if ancient and check and GameTime() - check.since >= (100 - strength) * 0.03 then
        return ancient
    end
end

local defenseSightings = {}

local function defenseApproach(bot, baseline)
    if not bot then return end
    local state = bot.teamCoachState
    local plan = state and state.plan
    local clock = DotaTime()
    local active = bot:GetActiveMode()
    if observeOnly or not dualTeam or bot:GetTeam() == selectedTeam or not plan or plan.objective ~= "defend"
        or plan.expires_clock_s <= clock or not canApplyCommand(bot) or tuning().macro == 0
        or health(bot) < 0.65 or bot:IsStunned() or bot:IsRooted() or bot:IsInvulnerable()
        or active == BOT_MODE_RETREAT or active == BOT_MODE_ROSHAN or activeCommand(state, "retreat", clock)
        or (active == BOT_MODE_ATTACK and bot:GetActiveModeDesire() >= BOT_MODE_DESIRE_HIGH)
        or bot:WasRecentlyDamagedByTower(2) then return end
    local desire = 0.67 * tuning().macro / 100
    if baseline and baseline >= desire then return end
    local tier, tower = firstTower(bot:GetTeam(), lanes[plan.lane])
    if tier ~= plan.tower or not tower then return end
    local ancient = GetAncient(bot:GetTeam())
    if not ancient then return end
    local from, home = tower:GetLocation(), ancient:GetLocation()
    local dx, dy = home.x - from.x, home.y - from.y
    local length = math.sqrt(dx * dx + dy * dy)
    if length < 100 then return end
    local shelter = Vector(from.x + dx * 520 / length, from.y + dy * 520 / length, from.z)
    if not IsLocationPassable(shelter) then return end
    local team = bot:GetTeam()
    local sight = defenseSightings[team]
    if not sight or clock < sight.at or clock - sight.at >= 0.4 then
        local _, locations = enemiesOnMap(GetOpposingTeam())
        sight = {at = clock, locations = locations}
        defenseSightings[team] = sight
    end
    local hostileLocations = sight.locations
    local attackers = countNearby(hostileLocations, tower)
    if attackers == 0 then return end
    local defenders = 0
    for member = 1, 5 do
        local ally = GetTeamMember(member)
        if ally and ally:IsAlive() and health(ally) >= 0.6 and GetUnitToLocationDistance(ally, from) <= 1200 then
            defenders = defenders + 1
        end
    end
    local distance = GetUnitToLocationDistance(bot, shelter)
    if distance <= 750 and attackers <= defenders and active == lanes[plan.lane].defend then
        return
    end
    local enemies = bot:GetNearbyHeroes(1000, true, BOT_MODE_NONE) or {}
    if #enemies > 0 or recentHeroDamage(bot) then return end
    for _, position in ipairs(hostileLocations) do
        local x, y = position.x - shelter.x, position.y - shelter.y
        if x * x + y * y < 700 * 700 then return end
    end
    if distance <= 310 then
        for _, creep in ipairs(bot:GetNearbyLaneCreeps(math.min(1000, bot:GetAttackRange()), true) or {}) do
            if creep:IsAlive() and creep:CanBeSeen() and not creep:IsAttackImmune()
                and GetUnitToLocationDistance(creep, from) <= 750 then
                return state, plan, shelter, desire, nil, creep
            end
        end
        return
    end
    if GetUnitToLocationDistance(bot, from) > 2300 then
        if health(tower) < 0.22 or attackers > math.max(1, defenders) or not townPortalReady(bot) then return end
        return state, plan, shelter, desire, bot:GetItemInSlot(15)
    end
    if attackers > defenders + 1 and distance > 850 then return end
    return state, plan, shelter, desire
end

function coach.fightDesire(bot, baseline)
    if defendAncient(bot) then
        return math.max(baseline, BOT_MODE_DESIRE_VERYHIGH)
    end
    local state = tacticalState(bot)
    if state then return fight.desire(bot, state, baseline) end
    local _, _, _, desire = defenseApproach(bot, baseline)
    if desire then return desire end
    return baseline
end

function coach.fightThink(bot)
    local ancient = defendAncient(bot)
    if ancient then
        local state = bot.teamCoachState
        local now = GameTime()
        if state and state.lastBaseOrder and now - state.lastBaseOrder < 0.55 then return true end
        if state then state.lastBaseOrder = now end
        local home = ancient:GetLocation()
        local distance = GetUnitToLocationDistance(bot, home)
        local enemies = bot:GetNearbyHeroes(1200, true, BOT_MODE_NONE) or {}
        if distance > 3000 and #enemies == 0 and not recentHeroDamage(bot) and townPortalReady(bot) then
            local baseTower = GetTower(bot:GetTeam(), TOWER_BASE_1)
            if not baseTower or not baseTower:IsAlive() then baseTower = GetTower(bot:GetTeam(), TOWER_BASE_2) end
            local landing = baseTower and baseTower:IsAlive() and baseTower:GetLocation() or home
            bot:Action_UseAbilityOnLocation(bot:GetItemInSlot(15), landing)
        elseif distance > 1200 then
            bot:Action_MoveToLocation(home)
        else
            local target, lowestHP
            for _, enemy in pairs(enemies) do
                if enemy:CanBeSeen() and not enemy:IsInvulnerable() and not enemy:IsAttackImmune()
                    and GetUnitToLocationDistance(enemy, home) <= 1900 and (not lowestHP or enemy:GetHealth() < lowestHP) then
                    target, lowestHP = enemy, enemy:GetHealth()
                end
            end
            if target then
                bot:Action_AttackUnit(target, true)
            else
                bot:Action_AttackMove(home)
            end
        end
        return true
    end
    local state = tacticalState(bot)
    if state then return fight.think(bot, state) end
    local rotationState, plan, shelter, _, tp, creep = defenseApproach(bot)
    if rotationState then
        local now = DotaTime()
        local previous = rotationState.defenseRotation
        if bot:NumQueuedActions() > 0 or (previous and previous.id == plan.id and now - previous.at < 0.75) then
            return true
        end
        if tp then
            bot:Action_UseAbilityOnLocation(tp, shelter)
        elseif creep then
            if bot:GetAttackTarget() ~= creep then bot:Action_AttackUnit(creep, true) end
        else
            bot:Action_MoveToLocation(shelter)
        end
        rotationState.defenseRotation = {id = plan.id, at = now}
        return true
    end
    return false
end

return coach
