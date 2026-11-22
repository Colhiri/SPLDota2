local json = require("league_json")
local socialPlay = require("league_social_play")

local ENDPOINT = "http://127.0.0.1:32157"
local RADIANT = DOTA_TEAM_GOODGUYS
local DIRE = DOTA_TEAM_BADGUYS
local HISTORY_LIMIT = 12
local IPC_ARGUMENT = "-offline_league_file_ipc"
local OPEN_LEAGUE_COMMAND = "dota_launch_custom_game offline_league dota"
local IPC_POLL_INTERVAL = 0.2
local IPC_REQUEST_TIMEOUT = 10
local DEFAULT_BOT_DIFFICULTY = 4
local LEAGUE = {}

local function realTime()
    if type(GetSystemTimeMS) == "function" then return GetSystemTimeMS() / 1000 end
    return Time()
end

local function optional(object, method, ...)
    if object and type(object[method]) == "function" then
        return object[method](object, ...)
    end
end

local function urlEncode(value)
    return (tostring(value):gsub("[^%w%-%._~]", function(char)
        return ("%%%02X"):format(char:byte())
    end))
end

local function queryString(values)
    local parts = {}
    for key, value in pairs(values) do
        parts[#parts + 1] = key .. "=" .. urlEncode(value)
    end
    return table.concat(parts, "&")
end

local function safeHero(value)
    if type(value) == "string" and value:match("^npc_dota_hero_[a-z0-9_]+$") then
        return value
    end
end

local function integerStat(value)
    return type(value) == "number" and math.max(0, math.floor(value)) or nil
end

local function ipcNonce()
    local time = GetSystemTimeMS and GetSystemTimeMS() or 0
    local random = RandomInt and RandomInt(0, 0x7FFFFFFE) or math.random(0, 0x7FFFFFFE)
    return ("%08x_%08x"):format(math.floor(time) % 0x80000000, random)
end

local function ipcReply(reply)
    if type(reply) ~= "table" then
        return
    end
    reply = reply.reply or reply
    if type(reply) ~= "table" then
        return
    end
    local status = tonumber(reply.status)
    if not status then
        return
    end
    local chunks = {}
    for index = 1, 512 do
        local part = reply["part_" .. index]
        if part == nil then
            if index > 1 then
                return status, table.concat(chunks)
            end
            return
        end
        if type(part) ~= "string" or #part == 0 or #part > 1024 or #part % 2 ~= 0 or not part:match("^%x+$") then
            return
        end
        chunks[index] = part:gsub("..", function(pair)
            return string.char(tonumber(pair, 16))
        end)
    end
    if reply.part_513 == nil then
        return status, table.concat(chunks)
    end
end

function LEAGUE:publish(status, message)
    self.status = status
    self.message = message or ""
    local history = {}
    for index = 1, math.min(HISTORY_LIMIT, #(self.history or {})) do
        history[index] = self.history[index]
    end
    CustomNetTables:SetTableValue("offline_league", "state", {
        status = self.status,
        message = self.message,
        profile = self.profile,
        social = self.social,
        history = history,
        match = self.match,
        result = self.result,
        retry_save = self.finishData ~= nil,
        tuning = self.tuning,
    })
end

function LEAGUE:completeRequest(path, status, body, callback)
    local code = tonumber(status)
    local decoded, data = pcall(json.decode, body or "")
    if code == 200 and decoded and type(data) == "table" then
        callback(data)
        return
    end
    print("[offline-league] " .. path .. " failed, status " .. tostring(code))
    local message = decoded and type(data) == "table" and data.error
    if code == 409 and path == "/queue" then
        message = "Сбрось предыдущий подбор или продолжи с прежней ролью."
    end
    if type(message) ~= "string" or code == nil or code <= 0 then
        message = "Нет связи с локальной лигой. Проверь запуск фонового сервиса."
    end
    callback(nil, message)
end

function LEAGUE:pollFile(id)
    local request = self.fileRequests and self.fileRequests[id]
    if not request then return end
    local now = realTime()
    if now < request.startedAt then request.startedAt = now end
    if request.lastPoll and now >= request.lastPoll and now - request.lastPoll < IPC_POLL_INTERVAL then
        return IPC_POLL_INTERVAL
    end
    request.lastPoll = now
    local ok, reply = pcall(LoadKeyValues, request.replyPath)
    if ok then
        local status, body = ipcReply(reply)
        if status then
            self.fileRequests[id] = nil
            self:completeRequest(request.path, status, body, request.callback)
            return
        end
    end
    if now - request.startedAt < IPC_REQUEST_TIMEOUT then return IPC_POLL_INTERVAL end
    self.fileRequests[id] = nil
    self:completeRequest(request.path, nil, nil, request.callback)
end

function LEAGUE:poll()
    local now = realTime()
    if self.lastClientPoll and now >= self.lastClientPoll and now - self.lastClientPoll < IPC_POLL_INTERVAL then
        return
    end
    self.lastClientPoll = now
    local pending = {}
    for id in pairs(self.fileRequests or {}) do pending[#pending + 1] = id end
    for _, id in ipairs(pending) do self:pollFile(id) end
    if self.finishData and not self.requestPending
        and (not self.saveAttempts or self.retrySaveAt and now >= self.retrySaveAt) then
        self:saveResult()
    elseif self.played and self.match and self.winner and not self.finishData and not self.result then
        self:beginResult(self.winner)
    end
end

function LEAGUE:requestFile(path, query, callback)
    if not self.mode or not LoadKeyValues then
        self:completeRequest(path, nil, nil, callback)
        return
    end
    self.ipcPrefix = self.ipcPrefix or ipcNonce()
    self.ipcSerial = (self.ipcSerial or 0) + 1
    local id = self.ipcPrefix .. ("_%08x"):format(self.ipcSerial)
    local operation = path:sub(2)
    self.fileRequests = self.fileRequests or {}
    self.fileRequests[id] = {
        path = path, replyPath = "scripts/league_ipc/" .. id .. ".txt", callback = callback, startedAt = realTime(),
    }
    print("[offline-league-ipc] " .. id .. " " .. operation .. " " .. (query ~= "" and query or "-"))
    self.mode:SetContextThink("league_ipc_" .. id, function()
        return self:pollFile(id)
    end, 0.25)
end

function LEAGUE:request(path, parameters, callback)
    local query = parameters and queryString(parameters) or ""
    local function useFiles()
        self.fileIPC = true
        self:requestFile(path, query, callback)
    end
    if self.fileIPC or not CreateHTTPRequestScriptVM then
        useFiles()
        return
    end
    local url = ENDPOINT .. path .. (query ~= "" and "?" .. query or "")
    local ok, request = pcall(CreateHTTPRequestScriptVM, "GET", url)
    if not ok or not request then
        print("[offline-league] Could not create request " .. path)
        useFiles()
        return
    end
    optional(request, "SetHTTPRequestAbsoluteTimeoutMS", 5000)
    local handled = false
    local sent, accepted = pcall(request.Send, request, function(response)
        if handled then
            return
        end
        handled = true
        local code = response and tonumber(response.StatusCode)
        if not code or code <= 0 then
            useFiles()
        else
            self:completeRequest(path, code, response.Body, callback)
        end
    end)
    if (not sent or accepted == false) and not handled then
        handled = true
        useFiles()
    end
end

function LEAGUE:acceptState(data)
    self.profile = data.profile or self.profile
    self.social = data.social or self.social
    self.history = data.history or self.history
    self.tuning = data.tuning or self.tuning
end

function LEAGUE:saveSettings(data)
    if self.requestPending or self.started or self.match
        or GameRules:State_Get() ~= DOTA_GAMERULES_STATE_CUSTOM_GAME_SETUP then
        return
    end
    local values = {}
    for _, key in ipairs({"bot_difficulty", "micro", "macro", "coach"}) do
        local value = tonumber(data[key])
        local maximum = key == "bot_difficulty" and 4 or 100
        if not value or value % 1 ~= 0 or value < 0 or value > maximum then
            self:publish("error", "Проверь значения настроек сложности.")
            return
        end
        values[key] = value
    end
    self.requestPending = true
    self:publish("loading", "Сохраняем настройки…")
    self:request("/settings", values, function(reply, message)
        self.requestPending = false
        if not reply then
            self:publish("error", message)
            return
        end
        self:acceptState(reply)
        self:publish("ready", "Настройки сохранены для следующих матчей.")
    end)
end

function LEAGUE:refresh()
    self:poll()
    if self.finishData then
        self:saveResult()
        return
    end
    if self.requestPending or self.started then
        self:publish(self.status, self.message)
        return
    end
    self.requestPending = true
    self:publish("loading", "Загрузка профиля…")
    self:request("/state", nil, function(data, message)
        self.requestPending = false
        if not data then
            self:publish("error", message)
            return
        end
        self:acceptState(data)
        self.match = data.active_match
        self:publish("ready", self.match and "Есть незавершённый подбор." or "")
    end)
end

function LEAGUE:allowOrder(order)
    if self.playerID == nil or order.issuer_player_id_const ~= self.playerID then
        return true
    end
    for _, index in pairs(order.units or {}) do
        local unit = EntIndexToHScript(index)
        if unit then
            local owner = unit:GetPlayerOwnerID()
            if owner ~= self.playerID and (unit:IsHero() or owner >= 0) then
                return false
            end
        end
    end
    return true
end

function LEAGUE:setup()
    GameRules:SetCustomGameTeamMaxPlayers(RADIANT, 5)
    GameRules:SetCustomGameTeamMaxPlayers(DIRE, 5)
    GameRules:SetCustomGameSetupTimeout(-1)
    GameRules:EnableCustomGameSetupAutoLaunch(false)
    self.mode:SetBotThinkingEnabled(true)
    self.mode:SetFreeCourierModeEnabled(true)
    self.mode:SetExecuteOrderFilter(Dynamic_Wrap(LEAGUE, "allowOrder"), self)
end

function LEAGUE:applyBotSettings()
    self.botSettings = {
        cheats = optional(Convars, "GetBool", "sv_cheats") == true,
        pickSlots = optional(Convars, "GetBool", "dota_bot_force_pick_slot") == true,
        picks = optional(Convars, "GetStr", "dota_bot_force_pick") or "",
    }
    -- BotPopulate uses the engine's local development path in a custom game.
    Convars:SetBool("sv_cheats", true)
    self:applyForcedPicks()
    local tuning = self.match.tuning or {}
    local level = tonumber(tuning.bot_difficulty) or DEFAULT_BOT_DIFFICULTY
    self.botDifficulty = math.max(0, math.min(4, math.floor(level)))
    optional(GameRules, "SetCustomGameDifficulty", self.botDifficulty)
end

function LEAGUE:applyForcedPicks()
    local heroes = {}
    for _, participant in ipairs(self.match.participants) do
        local slot = tonumber(participant.slot)
        if slot and slot >= 0 and slot <= 9 then
            heroes[slot + 1] = safeHero(participant.hero) or "npc_dota_hero_tiny"
        end
    end
    if #heroes == 10 then
        Convars:SetBool("dota_bot_force_pick_slot", true)
        Convars:SetStr("dota_bot_force_pick", table.concat(heroes, ","))
    end
end

function LEAGUE:restoreBotSettings()
    if not self.botSettings then
        return
    end
    Convars:SetBool("dota_bot_force_pick_slot", self.botSettings.pickSlots)
    Convars:SetStr("dota_bot_force_pick", self.botSettings.picks)
    Convars:SetBool("sv_cheats", self.botSettings.cheats)
    self.botSettings = nil
end

function LEAGUE:queue(playerID, role)
    if self.requestPending or self.started or GameRules:State_Get() ~= DOTA_GAMERULES_STATE_CUSTOM_GAME_SETUP then
        return
    end
    if role < 0 or role > 5 or role ~= math.floor(role) then
        return
    end
    self.requestPending = true
    self:publish("queuing", "Подбираем команды…")
    self:request("/queue", { role = role }, function(data, message)
        self.requestPending = false
        if not data or type(data.match) ~= "table" or #(data.match.participants or {}) ~= 10 then
            self:publish("error", message or "Сервис вернул неполный состав матча.")
            return
        end
        self:acceptState(data)
        self.match = data.match
        self.playerID = playerID
        self.started = true
        PlayerResource:SetCustomTeamAssignment(playerID, RADIANT)
        self:applyBotSettings()
        self:publish("starting", "Матч найден. Начинается выбор героев.")
        print("[offline-league] Match queued " .. self.match.id)
        self.mode:SetContextThink("league_start", function()
            GameRules:FinishCustomGameSetup()
        end, 0.8)
    end)
end

function LEAGUE:cancel()
    if self.requestPending or self.started or not self.match then
        return
    end
    self.requestPending = true
    self:publish("loading", "Отменяем подбор…")
    self:request("/cancel", { id = self.match.id }, function(data, message)
        self.requestPending = false
        if not data then
            self:publish("error", message)
            return
        end
        self:acceptState(data)
        self.match = nil
        self:publish("ready")
    end)
end

function LEAGUE:roster()
    local byTeam = { [RADIANT] = {}, [DIRE] = {} }
    for playerID = 0, (DOTA_MAX_TEAM_PLAYERS or 24) - 1 do
        if playerID ~= self.playerID and PlayerResource:IsValidPlayerID(playerID) then
            local slots = byTeam[PlayerResource:GetTeam(playerID)]
            if slots then
                slots[#slots + 1] = playerID
            end
        end
    end
    local mapped = { [tonumber(self.match.player_slot)] = self.playerID }
    local nextIndex = { [RADIANT] = 1, [DIRE] = 1 }
    for _, participant in ipairs(self.match.participants) do
        local slot = tonumber(participant.slot)
        local team = tonumber(participant.team)
        if slot ~= tonumber(self.match.player_slot) and byTeam[team] then
            mapped[slot] = byTeam[team][nextIndex[team]]
            nextIndex[team] = nextIndex[team] + 1
        end
    end
    return mapped
end

function LEAGUE:assignBotDifficulties()
    local mapped = self:roster()
    self.assignedDifficulty = self.assignedDifficulty or {}
    local applied = 0
    for _, participant in ipairs(self.match.participants) do
        local slot = tonumber(participant.slot)
        local playerID = mapped[slot]
        if playerID ~= nil and playerID ~= self.playerID then
            local hero = PlayerResource:GetSelectedHeroEntity(playerID)
            if hero and type(hero.SetBotDifficulty) == "function" then
                if not self.assignedDifficulty[playerID] then
                    hero:SetBotDifficulty(self.botDifficulty or DEFAULT_BOT_DIFFICULTY)
                    self.assignedDifficulty[playerID] = true
                end
                applied = applied + 1
            end
        end
    end
    self.difficultyAttempts = (self.difficultyAttempts or 0) + 1
    if applied < 9 and self.difficultyAttempts < 20 then
        return 0.25
    end
    self:restoreBotSettings()
    -- Disabling cheats restores dota_bot_disable to its custom-game default.
    Convars:SetBool("dota_bot_disable", false)
    print(("[offline-league] Set bot difficulty to %d for %d/9 bots")
        :format(self.botDifficulty or DEFAULT_BOT_DIFFICULTY, applied))
end

function LEAGUE:populateBots()
    if self.botsAdded or self.leavePending then
        return
    end
    self.botsAdded = true
    GameRules:BotPopulate()
end

function LEAGUE:prepareBotPicks()
    if self.botsAdded or self.requestPending or GameRules:State_Get() ~= DOTA_GAMERULES_STATE_STRATEGY_TIME then
        return
    end
    local selected = safeHero(optional(PlayerResource, "GetSelectedHeroName", self.playerID))
    if selected == nil then
        self.heroWaitAttempts = (self.heroWaitAttempts or 0) + 1
        if self.heroWaitAttempts < 10 then
            return 0.2
        end
    end
    for _, participant in ipairs(self.match.participants) do
        if not participant.is_player and participant.hero == selected then
            self.requestPending = true
            local identity = self.match.id
            self:request("/draft", { id = identity, hero = selected }, function(data, message)
                self.requestPending = false
                if type(data and data.match) == "table" and data.match.id == identity
                    and #(data.match.participants or {}) == 10 then
                    self.match = data.match
                    self:applyForcedPicks()
                    self:publish(self.status, self.message)
                else
                    print("[offline-league] Could not revise the bot pick for " .. identity .. ": " .. tostring(message))
                end
                self:populateBots()
            end)
            return
        end
    end
    self:populateBots()
end

function LEAGUE:playerStats(playerID)
    local player = PlayerResource
    return {
        hero = safeHero(optional(player, "GetSelectedHeroName", playerID)),
        kills = integerStat(optional(player, "GetKills", playerID)),
        deaths = integerStat(optional(player, "GetDeaths", playerID)),
        assists = integerStat(optional(player, "GetAssists", playerID)),
        gpm = integerStat(optional(player, "GetGoldPerMin", playerID)),
        xpm = integerStat(optional(player, "GetXPPerMin", playerID)),
        lastHits = integerStat(optional(player, "GetLastHits", playerID)),
        denies = integerStat(optional(player, "GetDenies", playerID)),
    }
end

function LEAGUE:beginResult(winner)
    if not self.played or not self.match or self.leavePending or self.finishData or self.result
        or (winner ~= RADIANT and winner ~= DIRE) then
        return
    end
    local player = self:playerStats(self.playerID)
    local rows = {}
    local mapped = self:roster()
    local score = { [RADIANT] = 0, [DIRE] = 0 }
    local counts = { [RADIANT] = 0, [DIRE] = 0 }
    for _, participant in ipairs(self.match.participants) do
        local slot = participant.slot
        local playerID = mapped[slot]
        local team = participant.team
        if playerID then
            local stats = self:playerStats(playerID)
            rows[#rows + 1] = table.concat({
                slot, stats.hero or "", stats.kills or "", stats.deaths or "",
                stats.assists or "", stats.gpm or "", stats.xpm or "",
                stats.lastHits or "", stats.denies or "",
            }, ",")
            if stats.kills ~= nil then
                score[team] = score[team] + stats.kills
                counts[team] = counts[team] + 1
            end
        end
    end
    self.finishData = {
        id = self.match.id,
        winner = winner,
        duration = integerStat(GameRules:GetDOTATime(false, false)) or 0,
        hero = player.hero,
        kills = player.kills,
        deaths = player.deaths,
        assists = player.assists,
        stats = table.concat(rows, ";"),
        radiant_score = integerStat(optional(PlayerResource, "GetTeamKills", RADIANT))
            or (counts[RADIANT] == 5 and score[RADIANT] or nil),
        dire_score = integerStat(optional(PlayerResource, "GetTeamKills", DIRE))
            or (counts[DIRE] == 5 and score[DIRE] or nil),
    }
    self:saveResult()
end

function LEAGUE:saveResult()
    if self.requestPending or not self.finishData then
        return
    end
    self.requestPending = true
    self.retrySaveAt = nil
    self.saveAttempts = (self.saveAttempts or 0) + 1
    self:publish("saving", "Сохраняем результат матча…")
    self:request("/finish", self.finishData, function(data, message)
        self.requestPending = false
        if not data or not data.result then
            self:publish("error", message or "Результат не сохранён. Повтори сохранение.")
            if self.saveAttempts < 4 then
                self.retrySaveAt = realTime() + 3
                self.mode:SetContextThink("league_retry_save", function()
                    self:poll()
                end, 3)
            end
            return
        end
        self:acceptState(data)
        self.result = data.result
        self.finishData = nil
        self.match = nil
        self:publish("finished")
        print("[offline-league] Match saved " .. tostring(self.result.id))
    end)
end

function LEAGUE:stateChanged()
    local state = GameRules:State_Get()
    if state == DOTA_GAMERULES_STATE_CUSTOM_GAME_SETUP then
        self:refresh()
    elseif state == DOTA_GAMERULES_STATE_STRATEGY_TIME and self.started and not self.botsAdded then
        local delay = self:prepareBotPicks()
        if delay then
            self.mode:SetContextThink("league_bot_picks", function() return self:prepareBotPicks() end, delay)
        end
    elseif state == DOTA_GAMERULES_STATE_PRE_GAME and self.started then
        self.mode:SetContextThink("league_bot_difficulty", function()
            return self:assignBotDifficulties()
        end, 0.25)
    elseif state == DOTA_GAMERULES_STATE_GAME_IN_PROGRESS and self.started then
        self.played = true
        if not self.leavePending then
            self:publish("playing")
            socialPlay.start(self)
        end
        print("[offline-league] Match in progress")
    elseif state == DOTA_GAMERULES_STATE_POST_GAME and self.played then
        self.mode:SetContextThink("league_finish", function()
            if self.winner then
                self:beginResult(self.winner)
            elseif not self.result and not self.finishData then
                self:publish("error", "Игра не передала победителя матча.")
            end
        end, 1)
    end
end

function LEAGUE:onAncientKilled(event)
    if not self.played or not event.entindex_killed then
        return
    end
    local unit = EntIndexToHScript(event.entindex_killed)
    local name = optional(unit, "GetUnitName")
    if name == "npc_dota_goodguys_fort" then
        self.winner = DIRE
    elseif name == "npc_dota_badguys_fort" then
        self.winner = RADIANT
    else
        return
    end
    self:beginResult(self.winner)
    local winner = self.winner
    self.mode:SetContextThink("league_ancient_victory", function()
        if GameRules:State_Get() == DOTA_GAMERULES_STATE_GAME_IN_PROGRESS then
            GameRules:SetGameWinner(winner)
        end
    end, 4)
end

function LEAGUE:leave()
    if not self.started or not self.match or self.requestPending or self.leavePending or self.finishData then
        return
    end
    local state = GameRules:State_Get()
    local inMatch = state == DOTA_GAMERULES_STATE_PLAYER_DRAFT
        or state == DOTA_GAMERULES_STATE_HERO_SELECTION
        or state == DOTA_GAMERULES_STATE_STRATEGY_TIME
        or state == DOTA_GAMERULES_STATE_TEAM_SHOWCASE
        or state == DOTA_GAMERULES_STATE_WAIT_FOR_MAP_TO_LOAD
        or state == DOTA_GAMERULES_STATE_PRE_GAME
        or state == DOTA_GAMERULES_STATE_GAME_IN_PROGRESS
    if not inMatch then
        return
    end
    if self.winner then
        self:beginResult(self.winner)
        return
    end

    self.leavePending = true
    self.requestPending = true
    local identity = self.match.id
    self:publish("leaving", "Возвращаемся в лигу…")
    self:request("/cancel", { id = identity }, function(data, message)
        self.requestPending = false
        if not data then
            self.leavePending = false
            if self.winner then
                self:beginResult(self.winner)
            else
                self:publish(self.played and "playing" or "starting", message or "Не удалось выйти в лигу. Попробуй ещё раз.")
            end
            return
        end

        self.match = nil
        self:restoreBotSettings()
        print("[offline-league] Left unfinished match " .. identity)
        SendToServerConsole(OPEN_LEAGUE_COMMAND)
    end)
end

function LEAGUE:rematch()
    if self.status ~= "finished" and not (self.status == "error" and self.played and not self.finishData) then
        return
    end
    SendToServerConsole(OPEN_LEAGUE_COMMAND)
end

function LEAGUE:listen(name, callback)
    CustomGameEventManager:RegisterListener(name, function(source, data)
        local sender = tonumber(data and data.PlayerID)
        if sender == nil or not PlayerResource:IsValidPlayerID(sender) then
            return
        end
        if self.playerID ~= nil and sender ~= self.playerID then
            return
        end
        callback(sender, data)
    end)
end

function Activate()
    local league = setmetatable({}, { __index = LEAGUE })
    GameRules.OfflineLeague = league
    league.mode = GameRules:GetGameModeEntity()
    league.fileIPC = optional(GlobalSys, "CommandLineCheck", IPC_ARGUMENT) == true
    league:setup()
    ListenToGameEvent("game_rules_state_change", function() league:stateChanged() end, nil)
    ListenToGameEvent("game_rules_shutdown", function() league:restoreBotSettings() end, nil)
    ListenToGameEvent("entity_killed", function(event)
        league:onAncientKilled(event)
        socialPlay.onEntityKilled(league, event)
    end, nil)
    ListenToGameEvent("dota_match_done", function(event)
        local winner = tonumber(event.winningteam)
        if winner == RADIANT or winner == DIRE then
            league.winner = winner
            league:beginResult(winner)
        end
    end, nil)
    league:listen("offline_league_refresh", function() league:refresh() end)
    league:listen("offline_league_poll", function() league:poll() end)
    league:listen("offline_league_settings", function(_, data) league:saveSettings(data) end)
    league:listen("offline_league_queue", function(sender, data) league:queue(sender, tonumber(data.role) or 0) end)
    league:listen("offline_league_cancel", function() league:cancel() end)
    league:listen("offline_league_leave", function() league:leave() end)
    league:listen("offline_league_rematch", function() league:rematch() end)
    league:publish("loading", "Загрузка профиля…")
    print("[offline-league] Addon activated")
end
