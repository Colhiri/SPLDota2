local league = {}
local dataPath = GetScriptDirectory() .. "/team_coach_league_roles"
local customMode = GAMEMODE_CUSTOM or 15
local current, readAt
local rosterData, rosterState, rosters
local pickAttempts = {}
local defaultTuning = {bot_difficulty = 4, micro = 100, macro = 100, coach = 100}
local currentTuning = defaultTuning

local function role(value)
    return type(value) == "number" and value % 1 == 0 and value >= 1 and value <= 5
end

local function validRoster(rows, humanRole, hasHuman, heroes)
    if type(rows) ~= "table" or #rows ~= 5 then return false end
    local roles = {}
    for slot, player in ipairs(rows) do
        if type(player) ~= "table" or not role(player.role) or roles[player.role] then return false end
        roles[player.role] = true
        if hasHuman and slot == 1 then
            if player.role ~= humanRole or player.hero ~= nil then return false end
        else
            local hero = player.hero
            if type(hero) ~= "string" or #hero > 64
                or not hero:match("^npc_dota_hero_[a-z0-9_]+$") or heroes[hero] then
                return false
            end
            heroes[hero] = true
        end
    end
    return true
end

local function read()
    if GetGameMode() ~= customMode then return end
    local now = GameTime()
    if readAt and now >= readAt and now - readAt < 0.5 then return current end
    readAt = now
    local ok, data = pcall(dofile, dataPath)
    local heroes = {}
    if not ok or type(data) ~= "table" or type(data.id) ~= "string" or #data.id > 64
        or not data.id:match("^[%w_-]+$") or not role(data.human_role)
        or not validRoster(data.radiant, data.human_role, true, heroes)
        or not validRoster(data.dire, data.human_role, false, heroes) then
        current = nil
        currentTuning = defaultTuning
        return
    end
    if not current or data.id ~= current.id then pickAttempts = {} end
    currentTuning = {}
    for key, maximum in pairs(defaultTuning) do
        local value = type(data.tuning) == "table" and data.tuning[key]
        if type(value) ~= "number" or value % 1 ~= 0 or value < 0 or value > maximum then
            value = maximum
        end
        currentTuning[key] = value
    end
    current = data
    return current
end

local function teamRoster(team, data)
    local ids = GetTeamPlayers(team, true)
    if type(ids) ~= "table" or #ids ~= 5 then return end
    local seen, bots, human = {}, {}, nil
    for _, id in ipairs(ids) do
        if type(id) ~= "number" or id % 1 ~= 0 or id < 0 or id > 23 or seen[id] then return end
        seen[id] = true
        if IsPlayerBot(id) then
            bots[#bots + 1] = id
        elseif team == TEAM_RADIANT and human == nil then
            human = id
        else
            return
        end
    end
    table.sort(bots)
    local byID, rows, offset = {}, data.dire, 0
    if team == TEAM_RADIANT then
        if human == nil or #bots ~= 4 then return end
        rows, offset = data.radiant, 1
        byID[human] = rows[1]
    elseif team ~= TEAM_DIRE or #bots ~= 5 then
        return
    end
    for index, id in ipairs(bots) do byID[id] = rows[index + offset] end
    return byID, ids
end

local function activeTeam(team)
    local data = read()
    if not data then return end
    local state = GetGameState()
    if rosterData ~= data or rosterState ~= state then
        rosterData, rosterState, rosters = data, state, nil
        local radiant, radiantIDs = teamRoster(TEAM_RADIANT, data)
        local dire, direIDs = teamRoster(TEAM_DIRE, data)
        if not radiant or not dire then return end
        if state >= GAME_STATE_PRE_GAME then
            local selected, matched = 0, 0
            for _, roster in ipairs({radiant, dire}) do
                for id, player in pairs(roster) do
                    local selectedHero = GetSelectedHeroName(id)
                    if player.hero and type(selectedHero) == "string" and selectedHero ~= "" then
                        selected = selected + 1
                        if selectedHero == player.hero then matched = matched + 1 end
                    end
                end
            end
            if selected >= 4 and matched < 2 then return end
        end
        rosters = {
            [TEAM_RADIANT] = {radiant, radiantIDs},
            [TEAM_DIRE] = {dire, direIDs},
        }
    end
    local entry = rosters and rosters[team]
    if entry then return entry[1], entry[2], data end
end

function league.configured()
    -- BotPopulate can load the first bot's laning script before the roster is complete.
    return read() ~= nil
end

function league.tuning()
    if not read() then return defaultTuning end
    return currentTuning
end

function league.microReady(bot, channel)
    local strength = league.tuning().micro
    if strength == 100 then return true end
    local now = GameTime()
    bot.leagueMicroTimes = bot.leagueMicroTimes or {}
    local last = bot.leagueMicroTimes[channel]
    if last and now >= last and now - last < (100 - strength) * 0.0075 then
        return false
    end
    bot.leagueMicroTimes[channel] = now
    return true
end

function league.isHumanPosition(team, position)
    local _, _, data = activeTeam(team)
    if data then return team == TEAM_RADIANT and position == data.human_role end
end

function league.position(team, playerID)
    local roster = activeTeam(team)
    return roster and roster[playerID] and roster[playerID].role
end

local function lane(team, position)
    if position == 2 then return LANE_MID end
    if position == 1 or position == 5 then
        return team == TEAM_RADIANT and LANE_BOT or LANE_TOP
    end
    return team == TEAM_RADIANT and LANE_TOP or LANE_BOT
end

function league.lane(bot)
    local team = bot:GetTeam()
    local position = league.position(team, bot:GetPlayerID())
    if position then return lane(team, position) end
end

function league.lanes(team, assignment)
    local roster, ids = activeTeam(team)
    if not roster then return end
    local lanes, positions = {}, {}
    for slot, id in ipairs(ids) do
        local position = roster[id].role
        lanes[slot], positions[slot] = lane(team, position), position
    end
    if assignment then
        assignment[team == TEAM_RADIANT and "TEAM_RADIANT" or "TEAM_DIRE"] = positions
    end
    return lanes
end

function league.pick(team)
    local roster, ids = activeTeam(team)
    if not roster then return false end
    local now = GameTime()
    local occupied = {}
    for _, side in ipairs({TEAM_RADIANT, TEAM_DIRE}) do
        for _, id in ipairs(GetTeamPlayers(side, true)) do
            local hero = GetSelectedHeroName(id)
            if type(hero) == "string" and hero ~= "" then occupied[hero] = true end
        end
    end
    local pending = false
    for _, id in ipairs(ids) do
        local hero = roster[id].hero
        local selected = GetSelectedHeroName(id)
        if hero and not occupied[hero] and (selected == "" or selected == nil)
            and IsPlayerInHeroSelectionControl(id) then
            local attempt = pickAttempts[id]
            if not attempt or attempt.hero ~= hero then
                attempt = {hero = hero, count = 0}
            end
            if not attempt.at or now - attempt.at >= 0.5 then
                if attempt.count < 2 then
                    attempt.at, attempt.count = now, attempt.count + 1
                    pickAttempts[id] = attempt
                    SelectHero(id, hero)
                    return true
                end
            else
                pending = true
            end
        end
    end
    return pending
end

return league
