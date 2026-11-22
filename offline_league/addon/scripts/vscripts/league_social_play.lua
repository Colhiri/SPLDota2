local SocialPlay = {}

local BANTER_COOLDOWN = 40
local SPEAKER_COOLDOWN = 105
local PING_COOLDOWN = 100
local TOWER_LANES = { "top", "mid", "bot" }
local FIRST_LINE = {
    ["Спокойный"] = "Хорошей игры. Начнём с линий.",
    ["Капитан"] = "Держим линии. Потом соберёмся на вышки.",
    ["Азартный"] = "Посмотрим, кто раньше возьмёт первую кровь.",
    ["Остряк"] = "Я уже придумал оправдание. Надеюсь, не понадобится.",
    ["Упрямый"] = "Я здесь за победой. Увидимся на карте.",
}
local PLAYER_KILLED = {
    ["Спокойный"] = "Поймал на выходе. До следующей встречи.",
    ["Капитан"] = "Сыграли вовремя. Теперь к вышке.",
    ["Азартный"] = "Хороший размен! Ещё раз?",
    ["Остряк"] = "Отличный план. Особенно до встречи со мной.",
    ["Упрямый"] = "Вот теперь я разыгрался.",
}
local PLAYER_SCORED = {
    ["Спокойный"] = "Красиво. В следующий раз сыграю осторожнее.",
    ["Капитан"] = "Принято. Надо перегруппироваться.",
    ["Азартный"] = "Твой раунд. Жди ответный.",
    ["Остряк"] = "Бесплатно показал тебе, где кнопка возрождения.",
    ["Упрямый"] = "Вернусь за реваншем.",
}

local function playing(league)
    return league.played and league.status == "playing" and not league.leavePending and not league.winner
        and GameRules:State_Get() == DOTA_GAMERULES_STATE_GAME_IN_PROGRESS
end

local function collectRoster(league)
    local roster = league:roster()
    local social = {
        byPlayer = {},
        bySlot = {},
        playerBySlot = {},
        allies = {},
        rivals = {},
        speakerTimes = {},
        sequence = 0,
    }
    for _, participant in ipairs(league.match.participants) do
        local slot = tonumber(participant.slot)
        local playerID = roster[slot]
        if playerID ~= nil then
            social.byPlayer[playerID] = participant
            social.bySlot[slot] = participant
            social.playerBySlot[slot] = playerID
        end
    end
    for _, pair in ipairs(league.match.social_pairs or {}) do
        local left = social.bySlot[tonumber(pair.left)]
        local right = social.bySlot[tonumber(pair.right)]
        if left and right and left ~= right then
            if pair.kind == "ally" and tonumber(left.team) == tonumber(right.team) then
                social.allies[#social.allies + 1] = { left, right }
            elseif pair.kind == "rival" and tonumber(left.team) ~= tonumber(right.team) then
                social.rivals[#social.rivals + 1] = { left, right }
            end
        end
    end
    return social
end

local function speak(league, social, speaker, line, now, relation)
    if not speaker or speaker.is_player or type(speaker.name) ~= "string" or speaker.name == ""
        or type(line) ~= "string" or line == ""
        or now < (social.speakerTimes[speaker.slot] or -SPEAKER_COOLDOWN) + SPEAKER_COOLDOWN then
        return false
    end
    social.sequence = social.sequence + 1
    social.lastLine = now
    social.speakerTimes[speaker.slot] = now
    CustomNetTables:SetTableValue("offline_league", "social_line", {
        sequence = social.sequence,
        id = league.match.id,
        name = speaker.name,
        text = line,
        relation = relation or speaker.relationship or "",
    })
    return true
end

function SocialPlay.start(league)
    league.mode:SetContextThink("league_social_intro", function()
        if not playing(league) or not league.match then
            return
        end
        local social = league.socialPlay or collectRoster(league)
        league.socialPlay = social
        if social.lastLine then
            return
        end

        local chosen, highest = nil, -1
        for _, participant in ipairs(league.match.participants) do
            if not participant.is_player and FIRST_LINE[participant.personality] then
                local weight = (participant.relationship == "Союзник" or participant.relationship == "Соперник") and 2 or 0
                if (tonumber(participant.effort) or 0) > 0 then
                    weight = weight + 4
                end
                if weight > highest then
                    chosen, highest = participant, weight
                end
            end
        end
        if chosen then
            local line = chosen.social_line
            if type(line) ~= "string" or line == "" then
                line = FIRST_LINE[chosen.personality]
            end
            speak(league, social, chosen, line, GameRules:GetDOTATime(false, false))
        end
    end, 14)
end

local function ownerOf(unit)
    if unit and type(unit.GetPlayerOwnerID) == "function" then
        local id = unit:GetPlayerOwnerID()
        if type(id) == "number" and id >= 0 then
            return id
        end
    end
end

local function entity(index)
    index = tonumber(index)
    if index and index > 0 then
        return EntIndexToHScript(index)
    end
end

local function distanceSquared(first, second)
    local x, y = first.x - second.x, first.y - second.y
    return x * x + y * y
end

local function defendTower(league, social, victim, killed, now)
    local team = tonumber(victim.team)
    if not MinimapEvent or not DOTA_MINIMAP_EVENT_HINT_LOCATION or not Entities
        or team ~= tonumber(league.match.player_team)
        or now < (social.lastPing or -PING_COOLDOWN) + PING_COOLDOWN then
        return false
    end

    local speaker, speakerHero
    for _, participant in ipairs(league.match.participants) do
        local playerSlot = tonumber(participant.slot)
        if not participant.is_player and tonumber(participant.team) == team
            and (tonumber(participant.effort) or 0) > 0
            and now >= (social.speakerTimes[playerSlot] or -SPEAKER_COOLDOWN) + SPEAKER_COOLDOWN then
            local id = social.playerBySlot[playerSlot]
            local hero = id ~= nil and PlayerResource:GetSelectedHeroEntity(id)
            if hero and hero:IsAlive() then
                speaker, speakerHero = participant, hero
                break
            end
        end
    end
    if not speaker then
        return false
    end

    local towerPrefix = team == DOTA_TEAM_GOODGUYS and "dota_goodguys_tower" or "dota_badguys_tower"
    local point = killed:GetAbsOrigin()
    local target, nearest = nil, 1400 * 1400
    for tier = 1, 3 do
        for _, lane in ipairs(TOWER_LANES) do
            local tower = Entities:FindByName(nil, towerPrefix .. tier .. "_" .. lane)
            if tower and tower:IsAlive() and tower:GetHealthPercent() < 90 then
                local origin = tower:GetAbsOrigin()
                local distance = distanceSquared(point, origin)
                if distance < nearest then
                    target, nearest = origin, distance
                end
            end
        end
    end
    if target and speak(league, social, speaker, "Вышка под давлением. Соберёмся у неё.", now) then
        MinimapEvent(team, speakerHero, target.x, target.y, DOTA_MINIMAP_EVENT_HINT_LOCATION, 3.0)
        social.lastPing = now
        return true
    end
    return false
end

function SocialPlay.onEntityKilled(league, event)
    if not playing(league) or not league.match then
        return
    end
    local now = GameRules:GetDOTATime(false, false)
    local social = league.socialPlay
    if social and social.lastLine and now < social.lastLine + BANTER_COOLDOWN then
        return
    end

    local killed = entity(event.entindex_killed)
    local victimID = ownerOf(killed)
    if not victimID or killed ~= PlayerResource:GetSelectedHeroEntity(victimID) then
        return
    end
    local attackerID = ownerOf(entity(event.entindex_attacker))
    if not attackerID or attackerID == victimID then
        return
    end

    social = social or collectRoster(league)
    league.socialPlay = social
    local victim, attacker = social.byPlayer[victimID], social.byPlayer[attackerID]
    if not victim or not attacker or tonumber(attacker.team) == tonumber(victim.team) then
        return
    end
    if defendTower(league, social, victim, killed, now) then
        return
    end
    if victim.is_player then
        local line = attacker.relationship == "Соперник" and "В этот раз за мной. Посмотрим, что будет дальше."
            or PLAYER_KILLED[attacker.personality]
        if speak(league, social, attacker, line, now) then
            return
        end
    elseif attacker.is_player then
        local line = victim.relationship == "Соперник" and "Один раунд за тобой. Я ещё вернусь."
            or PLAYER_SCORED[victim.personality]
        if speak(league, social, victim, line, now) then
            return
        end
    end

    for _, pair in ipairs(social.rivals) do
        if (pair[1] == attacker and pair[2] == victim) or (pair[1] == victim and pair[2] == attacker) then
            local line = "До следующей встречи, " .. victim.name .. "."
            if speak(league, social, attacker, line, now, "Соперник") then
                return
            end
        end
    end
    for _, pair in ipairs(social.allies) do
        local friend, survived
        if pair[1] == attacker or pair[1] == victim then
            friend, survived = pair[2], pair[1] == attacker
        elseif pair[2] == attacker or pair[2] == victim then
            friend, survived = pair[1], pair[2] == attacker
        end
        local line = survived and "Хороший размен. Закрепим." or "Вернёшься — сыграем вместе."
        if speak(league, social, friend, line, now, "Союзник") then
            return
        end
    end
end

return SocialPlay
