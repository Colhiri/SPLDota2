(function () {
    "use strict";

    var root = $.GetContextPanel();
    var selectedRole = 0;
    var state = {};
    var historySignature;
    var rosterId;
    var socialSignature;
    var selectedTab = "history";
    var renderedResult;
    var history = {};
    var historyRequest = null;
    var social;
    var coaches;
    var savedResult;
    var previewResult;
    var saveWaitExpired = false;
    var archiveRequest = 0;
    var archiveResult;
    var returningToLeague = false;
    var returnRequestNumber = 0;
    var returnFeedback = "";
    var queuePending = false;
    var tuningDraft;
    var tuningSaved;
    var tuningRequest;
    var tuningSaving = false;
    var updatingSliders = false;
    var tuningFeedback = "";
    var difficultyNames = ["Пассивный", "Лёгкий", "Средний", "Сложный", "Нечестный"];
    var tuningControls = [
        {key: "bot_difficulty", slider: "BotDifficultySlider", label: "BotDifficultyValue", maximum: 4},
        {key: "micro", slider: "MicroSlider", label: "MicroValue", maximum: 100},
        {key: "macro", slider: "MacroSlider", label: "MacroValue", maximum: 100},
        {key: "coach", slider: "CoachSlider", label: "CoachValue", maximum: 100}
    ];
    var denominations = ["Любая роль", "Керри", "Мид", "Офлейн", "Позиция 4", "Позиция 5"];
    var statusMessages = {
        loading: "Загрузка профиля…",
        ready: "Выбери роль и начни поиск.",
        queuing: "Подбираем составы…",
        starting: "Открываем выбор героев…",
        playing: "Матч начался.",
        saving: "Сохраняем результат…",
        finished: "Результат сохранён.",
        error: "Не удалось обновить данные лиги."
    };

    function panel(id) {
        return root.id === id ? root : root.FindChildTraverse(id);
    }

    function setText(id, value) {
        var label = panel(id);
        if (label) {
            label.text = value;
        }
    }

    function show(id, visible) {
        var target = panel(id);
        if (target) {
            target.SetHasClass("IsHidden", !visible);
            target.style.visibility = visible ? "visible" : "collapse";
            if (id === "LeagueResultCanvas") {
                target.hittest = visible;
                target.hittestchildren = visible;
            }
        }
    }

    function isPostGame() {
        var phase = integer(Game.GetState());
        var postGame = integer(DOTA_GameState.DOTA_GAMERULES_STATE_POST_GAME);
        return phase !== null && postGame !== null && phase === postGame;
    }

    function checked(value) {
        return value === true || value === 1 || value === "1" || value === "true";
    }

    function integer(value) {
        if (value === undefined || value === null || value === "") {
            return null;
        }
        var parsed = Number(value);
        return isFinite(parsed) ? Math.round(parsed) : null;
    }

    function displayNumber(value) {
        var parsed = integer(value);
        return parsed === null ? "—" : String(parsed);
    }

    function pad(number) {
        return number < 10 ? "0" + number : String(number);
    }

    function duration(seconds) {
        var length = integer(seconds);
        if (length === null || length < 0) {
            return "—";
        }
        return Math.floor(length / 60) + ":" + pad(length % 60);
    }

    function shortDate(value) {
        if (!value) {
            return "";
        }
        var date = new Date(value);
        if (isNaN(date.getTime())) {
            return "";
        }
        return pad(date.getDate()) + "." + pad(date.getMonth() + 1);
    }

    function sign(value) {
        var delta = integer(value);
        if (delta === null) {
            return "—";
        }
        return (delta > 0 ? "+" : "") + delta;
    }

    function roleName(value) {
        var number = integer(value);
        return denominations[number] || "—";
    }

    function isHero(name) {
        return typeof name === "string" && name.indexOf("npc_dota_hero_") === 0;
    }

    function heroName(name) {
        if (!isHero(name)) {
            return name || "—";
        }
        var token = "#" + name;
        var localized = $.Localize(token);
        if (localized && localized !== token && localized !== name) {
            return localized;
        }
        var shortName = name.substring("npc_dota_hero_".length);
        var aliases = {
            furion: "Nature’s Prophet",
            nevermore: "Shadow Fiend",
            obsidian_destroyer: "Outworld Destroyer",
            rattletrap: "Clockwerk",
            shredder: "Timbersaw",
            skeleton_king: "Wraith King",
            windrunner: "Windranger",
            wisp: "Io",
            zuus: "Zeus"
        };
        return aliases[shortName] || shortName.split("_").map(function (word) {
            return word.charAt(0).toUpperCase() + word.substring(1);
        }).join(" ");
    }

    function ordered(value) {
        if (!value || typeof value !== "object") {
            return [];
        }
        if (Array.isArray(value)) {
            return value;
        }
        return Object.keys(value).filter(function (key) {
            return /^\d+$/.test(key);
        }).sort(function (left, right) {
            return Number(left) - Number(right);
        }).map(function (key) {
            return value[key];
        });
    }

    function addLabel(parent, classNames, value) {
        var label = $.CreatePanel("Label", parent, "");
        classNames.split(" ").forEach(function (name) {
            if (name) {
                label.AddClass(name);
            }
        });
        label.text = value;
        return label;
    }

    function addHero(parent, className, hero) {
        if (isHero(hero)) {
            var image = $.CreatePanel("DOTAHeroImage", parent, "", {
                heroname: hero,
                heroimagestyle: "icon"
            });
            image.AddClass(className);
        }
    }

    function recordKda(entry) {
        return [entry.kills, entry.deaths, entry.assists].map(displayNumber).join(" / ");
    }

    function renderHistory(history, status) {
        var nextSignature = history.map(function (entry) {
            return String(entry.id) + ":" + String(entry.rating_after);
        }).join(",");
        if (nextSignature !== historySignature) {
            historySignature = nextSignature;
            var rows = panel("HistoryList");
            rows.RemoveAndDeleteChildren();
            history.forEach(function (entry) {
                var row = $.CreatePanel("Button", rows, "");
                row.AddClass("HistoryRow");
                row.SetPanelEvent("onactivate", function () { openArchive(entry); });
                var won = checked(entry.won);
                var outcome = $.CreatePanel("Panel", row, "");
                outcome.AddClass("ColResult");
                outcome.AddClass("RowResult");
                addLabel(outcome, "RowOutcome " + (won ? "Gain" : "Loss"), won ? "Победа" : "Поражение");
                var date = shortDate(entry.completed_at);
                if (date) {
                    addLabel(outcome, "RowDate", date);
                }

                var identity = $.CreatePanel("Panel", row, "");
                identity.AddClass("ColHero");
                identity.AddClass("RowIdentity");
                addHero(identity, "RowHeroIcon", entry.hero);
                addLabel(identity, "RowHeroName", heroName(entry.hero));
                addLabel(row, "ColRole Quiet", roleName(entry.role));
                addLabel(row, "ColKDA Quiet", recordKda(entry));
                addLabel(row, "ColDuration Quiet", duration(entry.duration_seconds));
                var delta = integer(entry.rating_delta);
                var deltaClass = delta > 0 ? "Gain" : (delta < 0 ? "Loss" : "Quiet");
                addLabel(row, "ColDelta " + deltaClass, sign(delta));
            });
        }

        show("HistoryHeader", history.length > 0);
        show("HistoryList", history.length > 0);
        show("HistoryEmpty", history.length === 0);
        if (!history.length) {
            var waiting = status === "loading";
            var failed = status === "error" && !state.profile;
            setText("HistoryEmptyTitle", waiting ? "Загрузка истории…" : (failed ? "История недоступна" : "Матчей пока нет"));
            setText("HistoryEmptyNote", waiting || failed ? "" : "Завершённые матчи появятся здесь.");
        }
    }

    function renderScoreboard(id, result) {
        var board = panel(id);
        board.RemoveAndDeleteChildren();
        var participants = ordered(result.participants);
        if (!participants.length) {
            addLabel(board, "ArchiveStatus", "Статистика участников не сохранилась.");
            return;
        }
        var columns = [
            ["У / С / П", recordKda],
            ["Доб./отр.", function (row) { return displayNumber(row.last_hits) + " / " + displayNumber(row.denies); }],
            ["GPM/XPM", function (row) { return displayNumber(row.gpm) + " / " + displayNumber(row.xpm); }],
            ["Ценность", function (row) { return groupedNumber(row.net_worth); }],
            ["Урон", function (row) { return groupedNumber(row.hero_damage); }],
            ["Лечение", function (row) { return groupedNumber(row.healing); }]
        ];
        [2, 3].forEach(function (team) {
            var side = $.CreatePanel("Panel", board, "");
            side.AddClass("ScoreSide");
            var header = $.CreatePanel("Panel", side, "");
            header.AddClass("ScoreHeader");
            header.AddClass(team === 2 ? "Radiant" : "Dire");
            var score = team === 2 ? result.radiant_score : result.dire_score;
            var teamTitle = (team === 2 ? "Силы Света" : "Силы Тьмы") +
                (integer(score) === null ? "" : "  ·  " + score);
            if (team === integer(result.winner)) {
                teamTitle += "  ·  победа";
            }
            addLabel(header, "ScoreIdentity", teamTitle);
            columns.forEach(function (column) { addLabel(header, "ScoreStat", column[0]); });

            participants.filter(function (row) { return integer(row.team) === team; }).forEach(function (entry) {
                var row = $.CreatePanel("Panel", side, "");
                row.AddClass("ScoreRow");
                row.SetHasClass("ScorePlayer", checked(entry.is_player));
                var identity = $.CreatePanel("Panel", row, "");
                identity.AddClass("ScoreIdentity");
                addHero(identity, "ScoreHeroIcon", entry.hero);
                var name = checked(entry.is_player) ? "Ты" : (entry.name || "Бот");
                var level = integer(entry.level);
                var detail = heroName(entry.hero) + (level === null ? "" : " · " + level + " ур.");
                addLabel(identity, "ScoreName", name + " · " + detail);
                columns.forEach(function (column) { addLabel(row, "ScoreStat", column[1](entry)); });
            });
        });
    }

    function openArchive(entry) {
        archiveResult = null;
        var request = ++archiveRequest;
        selectedTab = "archive";
        renderHubTabs();
        panel("ArchiveScoreboard").RemoveAndDeleteChildren();
        show("ArchiveReplay", false);
        setText("ArchiveTitle", (checked(entry.won) ? "Победа" : "Поражение") + " · " + duration(entry.duration_seconds));
        setText("ArchiveStatus", "Загружаем статистику…");
        send("offline_league_match_details", {id: entry.id, request: request});
        $.Schedule(12, function () {
            if (root.IsValid() && selectedTab === "archive" && archiveRequest === request && !archiveResult) {
                setText("ArchiveStatus", "Ответ не получен. Открой матч заново.");
            }
        });
    }

    function receiveArchive(data) {
        if (!root.IsValid() || selectedTab !== "archive" || integer(data.request) !== archiveRequest) {
            return;
        }
        archiveResult = data.result;
        if (!archiveResult) {
            setText("ArchiveStatus", data.message || "Не удалось загрузить статистику.");
            return;
        }
        setText("ArchiveStatus", roleName(archiveResult.role) + " · " + sign(archiveResult.rating_delta) +
            " MMR  ·  " + (archiveResult.replay ? "Повтор сохранён" : "Повтор недоступен"));
        show("ArchiveReplay", Boolean(archiveResult.replay));
        renderScoreboard("ArchiveScoreboard", archiveResult);
    }

    function renderCoaches() {
        ["Radiant", "Dire"].forEach(function (team) {
            var info = coaches && coaches[team.toLowerCase()];
            var description = !coaches ? "проверяем связь" : "нет связи";
            if (info) {
                description = info.mode === "rules" ? "локальные правила" :
                    (checked(info.model_ready) ? "тренер готов" : "сервер готов");
            }
            setText("Coach" + team, team + " · " + description);
            panel("Coach" + team).SetHasClass("CoachOnline", Boolean(info));
        });
    }

    function requestHistory(offset) {
        if (historyRequest !== null) {
            return;
        }
        historyRequest = offset;
        setText("HistoryFeedback", "Загружаем…");
        renderSetup();
        send("offline_league_history", {offset: offset});
        $.Schedule(12, function () {
            if (root.IsValid() && historyRequest === offset) {
                historyRequest = null;
                setText("HistoryFeedback", "Ответ не получен. Попробуй снова.");
                renderSetup();
            }
        });
    }

    function groupedNumber(value) {
        var parsed = integer(value);
        return parsed === null ? "—" : String(parsed).replace(/\B(?=(\d{3})+(?!\d))/g, "\u00a0");
    }

    function relationshipLabel(kind) {
        if (kind === "ally") {
            return "Союзник";
        }
        if (kind === "rival") {
            return "Соперник";
        }
        return "Знакомый";
    }

    function relationshipClass(kind) {
        if (kind === "ally") {
            return " Gain";
        }
        if (kind === "rival") {
            return " Loss";
        }
        return "";
    }

    function renderSocial(social, isLoading) {
        var contacts = ordered(social && social.contacts);
        var ties = ordered(social && social.bot_ties);
        show("SocialLeagueStats", Boolean(social));
        if (social) {
            var description = "Ботов в лиге " + groupedNumber(social.population);
            if (integer(social.simulated_matches) !== null) {
                description += "  ·  Симуляций матчей " + groupedNumber(social.simulated_matches);
            }
            setText("SocialLeagueStats", description);
        }
        show("SocialEmpty", contacts.length === 0);
        setText("SocialEmpty", isLoading ? "Загрузка знакомых…" : (social ?
            "Знакомые появятся после первого завершённого матча." : "Сведения о знакомых пока недоступны."));
        show("SocialContacts", contacts.length > 0);
        show("SocialTies", ties.length > 0);

        var signature = JSON.stringify([contacts, ties]);
        if (signature === socialSignature) {
            return;
        }
        socialSignature = signature;
        var contactList = panel("SocialContacts");
        contactList.RemoveAndDeleteChildren();
        contacts.forEach(function (contact) {
            var row = $.CreatePanel("Panel", contactList, "");
            row.AddClass("SocialRow");
            addHero(row, "SocialHeroIcon", contact.hero);

            var identity = $.CreatePanel("Panel", row, "");
            identity.AddClass("SocialIdentity");
            addLabel(identity, "SocialName", contact.name || "Бот");
            var trait = contact.personality ? " · " + contact.personality : "";
            addLabel(identity, "SocialKind" + relationshipClass(contact.kind), relationshipLabel(contact.kind) + trait);

            var totals = $.CreatePanel("Panel", row, "");
            totals.AddClass("SocialTotals");
            addLabel(totals, "SocialCount", "Вместе " + displayNumber(contact.with_games));
            addLabel(totals, "SocialCount", "Друг против друга " + displayNumber(contact.against_games));
        });

        var tiesList = panel("SocialTiesList");
        tiesList.RemoveAndDeleteChildren();
        ties.forEach(function (tie) {
            var row = $.CreatePanel("Panel", tiesList, "");
            row.AddClass("SocialRow");
            row.AddClass("SocialTieRow");
            var identity = $.CreatePanel("Panel", row, "");
            identity.AddClass("SocialIdentity");
            addLabel(identity, "SocialName", (tie.first_name || "Бот") + " и " + (tie.second_name || "бот"));
            var kind = tie.kind === "ally" ? "Союзники" : (tie.kind === "rival" ? "Соперники" : "Знакомые");
            addLabel(identity, "SocialKind" + relationshipClass(tie.kind), kind);
            addLabel(row, "SocialCount", "Матчей " + displayNumber(tie.played));
        });
    }

    function renderHubTabs() {
        ["history", "social", "settings"].forEach(function (tab) {
            var prefix = tab.charAt(0).toUpperCase() + tab.substring(1);
            panel(prefix + "Tab").SetHasClass("HubTabSelected", selectedTab === tab || (tab === "history" && selectedTab === "archive"));
            show(prefix + "Pane", selectedTab === tab);
        });
        show("ArchivePane", selectedTab === "archive");
    }

    function normalizedTuning(value) {
        var settings = value || {};
        var result = {};
        tuningControls.forEach(function (control) {
            var number = integer(settings[control.key]);
            result[control.key] = number === null ? control.maximum : Math.max(0, Math.min(control.maximum, number));
        });
        return result;
    }

    function sameTuning(left, right) {
        return Boolean(left && right) && tuningControls.every(function (control) {
            return left[control.key] === right[control.key];
        });
    }

    function tuningDirty() {
        return !sameTuning(tuningDraft, tuningSaved);
    }

    function canEditTuning() {
        return (state.status === "ready" || state.status === "error") && !(state.match && state.match.id) && !tuningSaving;
    }

    function updateSliders() {
        updatingSliders = true;
        tuningControls.forEach(function (control) {
            var slider = panel(control.slider);
            slider.min = 0;
            slider.max = control.maximum;
            slider.increment = 1;
            slider.SetValueNoEvents(tuningDraft[control.key]);
        });
        updatingSliders = false;
    }

    function renderTuning() {
        var saved = normalizedTuning(state.tuning);
        var adoptSaved = !tuningDraft || (!tuningDirty() && !tuningSaving && !sameTuning(tuningSaved, saved));
        if (tuningRequest && sameTuning(tuningRequest, saved)) {
            tuningSaving = false;
            tuningRequest = null;
            tuningFeedback = "Сохранено. Настройки применятся со следующего матча.";
            adoptSaved = true;
        } else if (tuningSaving && state.status === "error") {
            tuningSaving = false;
            tuningRequest = null;
            tuningFeedback = state.message || "Не удалось сохранить настройки. Повтори попытку.";
        }
        tuningSaved = saved;
        if (adoptSaved) {
            tuningDraft = normalizedTuning(saved);
            updateSliders();
        }

        var editable = canEditTuning();
        tuningControls.forEach(function (control) {
            panel(control.slider).enabled = editable;
            var value = tuningDraft[control.key];
            setText(control.label, control.key === "bot_difficulty" ? difficultyNames[value] : value + "%");
        });
        panel("SaveSettingsButton").enabled = editable && tuningDirty();
        panel("MaxSettingsButton").enabled = editable && !sameTuning(tuningDraft, normalizedTuning());
        setText("SaveSettingsLabel", tuningSaving ? "Сохраняем…" : "Сохранить");
        var message = tuningFeedback;
        if (tuningSaving) {
            message = "Сохраняем настройки…";
        } else if (tuningDirty() && editable) {
            message = tuningFeedback || "Есть изменения. Сохрани их перед поиском матча.";
        } else if (state.match && state.match.id) {
            message = "Изменить сложность можно после завершения или отмены подбора.";
        } else if (!message) {
            message = sameTuning(tuningDraft, normalizedTuning()) ? "Все параметры выставлены на максимум." : "Настройки сохранены.";
        }
        setText("SettingsFeedback", message);
        panel("SettingsFeedback").SetHasClass("SettingsError", Boolean(tuningFeedback) && state.status === "error");
    }

    function renderRoster(match, visible) {
        show("HistoryCard", !visible);
        show("RosterCard", visible);
        if (!visible || match.id === rosterId) {
            return;
        }

        rosterId = match.id;
        var radiant = panel("RadiantTeam");
        var dire = panel("DireTeam");
        radiant.RemoveAndDeleteChildren();
        dire.RemoveAndDeleteChildren();
        var team = integer(match.player_team) === 3 ? "Силы Тьмы" : "Силы Света";
        setText("RosterNote", team + " · " + roleName(match.role));
        var participants = ordered(match.participants);
        participants.forEach(function (participant) {
            var target = integer(participant.team) === 3 ? dire : radiant;
            var row = $.CreatePanel("Panel", target, "");
            row.AddClass("TeamRow");
            addHero(row, "RowHeroIcon", participant.hero);
            var details = $.CreatePanel("Panel", row, "");
            details.AddClass("TeamRowProfile");
            var iconName = participant.hero ? " · " + heroName(participant.hero) : "";
            addLabel(details, "TeamPlayerName", checked(participant.is_player) ? "Ты" : (participant.name || "Бот"));
            addLabel(details, "TeamPlayerRole", roleName(participant.role) + iconName);
            var socialDetails = [participant.relationship, participant.personality].filter(Boolean);
            var effort = integer(participant.effort);
            if (effort) {
                socialDetails.push("Настрой " + sign(effort));
            }
            if (!checked(participant.is_player) && socialDetails.length) {
                row.AddClass("TeamRowSocial");
                addLabel(details, "TeamPlayerSocial", socialDetails.join(" · "));
            }
            addLabel(row, "TeamRating", displayNumber(participant.rating));
        });
    }

    function renderProfile(profile) {
        if (!profile) {
            setText("MedalText", state.status === "error" ? "Профиль недоступен" : "Загрузка…");
            return;
        }
        var calibrated = checked(profile.calibrated);
        var played = integer(profile.calibration_played) || 0;
        var goal = integer(profile.calibration_required) || 0;
        setText("MedalText", calibrated ? (profile.medal || "Ранг определён") : "Калибровка");
        setText("RatingText", displayNumber(profile.rating));
        setText("WinText", "Победы " + displayNumber(profile.wins));
        setText("LossText", "Поражения " + displayNumber(profile.losses));
        show("CalibrationBox", !calibrated);
        if (!calibrated) {
            setText("CalibrationText", goal > 0 ? played + " / " + goal : displayNumber(played));
            var progress = goal > 0 ? Math.min(100, Math.max(0, played * 100 / goal)) : 0;
            panel("CalibrationProgress").style.width = progress + "%";
        }
    }

    function renderSetup() {
        renderTuning();
        var status = state.status || "loading";
        var canQueue = status === "ready";
        var match = state.match;
        var canResume = Boolean(match && match.id && (canQueue || status === "error"));
        var activeRole = canResume ? integer(match.role) : selectedRole;
        var queue = panel("QueueButton");
        queue.enabled = (canQueue || canResume) && !queuePending && !tuningDirty() && !tuningSaving;
        setText("QueueButtonLabel", status === "queuing" ? "Подбираем команды…" :
            (status === "starting" ? "Матч найден" : (canResume ? "Продолжить подбор" : "Найти матч")));
        show("CancelButton", canResume);
        show("RefreshButton", status === "ready" || status === "error");
        for (var index = 0; index <= 5; index++) {
            var button = panel("Role" + index);
            button.enabled = canQueue && !canResume;
            button.SetHasClass("SelectedRole", index === activeRole);
        }

        setText("StatusText", canQueue && tuningDirty() ? "Сохрани изменения на вкладке «Сложность» перед поиском." :
            (state.message || statusMessages[status] || ""));
        panel("StatusText").SetHasClass("StatusError", status === "error");
        panel("StatusDot").SetHasClass("StatusError", status === "error");
        panel("StatusDot").SetHasClass("StatusReady", canQueue);
        renderProfile(state.profile);
        renderHubTabs();
        renderHistory(ordered(history.matches), status);
        show("HistoryPrevious", integer(history.offset) > 0);
        show("HistoryNext", checked(history.more));
        panel("HistoryPrevious").enabled = historyRequest === null;
        panel("HistoryNext").enabled = historyRequest === null;
        renderSocial(social, status === "loading");
        renderRoster(match, Boolean(match && match.id && (canResume || status === "starting")));
        renderCoaches();
    }

    function renderResult() {
        var inMatch = Game.GetState() === DOTA_GameState.DOTA_GAMERULES_STATE_GAME_IN_PROGRESS;
        var resultActive = isPostGame() || (inMatch && state.status === "finished" && Boolean(savedResult));
        show("LeagueResultCanvas", resultActive);
        if (!resultActive) {
            return;
        }
        var result = savedResult || previewResult;
        var hasResult = Boolean(result && result.id && integer(result.winner) > 0);
        var success = Boolean(savedResult && hasResult);
        var won = hasResult && checked(result.won);
        var shell = panel("ResultShell");
        shell.SetHasClass("ResultWon", won);
        shell.SetHasClass("ResultLost", hasResult && !won);
        show("ResultDetails", hasResult);
        var retry = state.status === "error" && checked(state.retry_save);
        show("RetrySaveButton", retry);
        panel("RetrySaveButton").enabled = retry;
        show("RematchButton", inMatch && success);
        panel("RematchButton").enabled = !returningToLeague;
        setText("RematchButtonLabel", returningToLeague ? "Возвращаемся…" : "Вернуться в лигу");
        setText("ResultReturnStatus", returnFeedback);
        var saveMessage = saveWaitExpired ? "Подтверждение сохранения можно проверить в истории лиги." : statusMessages.saving;
        if (!hasResult) {
            setText("ResultTitle", "Матч завершён");
            var message = state.status === "error" || state.status === "saving" ? state.message : "";
            setText("ResultSubtitle", message || saveMessage);
            return;
        }

        setText("ResultTitle", won ? "Победа" : "Поражение");
        setText("ResultSubtitle", (success ? "Матч сохранён" : state.status === "error" && state.message || saveMessage) +
            " · " + duration(result.duration_seconds) + " · " + roleName(result.role));
        setText("ResultRating", success ? displayNumber(result.rating_before) + " → " + displayNumber(result.rating_after) +
            " MMR  (" + sign(result.rating_delta) + ")" : "");
        if (renderedResult !== result) {
            renderedResult = result;
            renderScoreboard("ResultScoreboard", result);
        }
    }

    function returnToLeague() {
        if (returningToLeague || (!savedResult && !saveWaitExpired && !(state.status === "error" && !checked(state.retry_save)))) {
            return;
        }
        returningToLeague = true;
        var requestNumber = ++returnRequestNumber;
        returnFeedback = "Открываем меню локальной лиги…";
        send("offline_league_rematch");
        renderResult();
        $.Schedule(8.0, function () {
            if (root.IsValid() && returningToLeague && requestNumber === returnRequestNumber) {
                returningToLeague = false;
                returnFeedback = "Если меню не открылось, выполни offline_league в игровой консоли.";
                renderResult();
            }
        });
    }

    function pollPostgame() {
        if (!root.IsValid()) {
            return;
        }
        if (isPostGame() && !savedResult && !saveWaitExpired) {
            send("offline_league_poll");
        }
        renderResult();
        $.Schedule(0.5, pollPostgame);
    }

    function send(event, value) {
        var payload = value || {};
        payload.PlayerID = Game.GetLocalPlayerID();
        GameEvents.SendCustomGameEventToServer(event, payload);
    }

    function activate(id, callback) {
        panel(id).SetPanelEvent("onactivate", callback);
    }

    function start() {
        var isSetup = Boolean(panel("LeagueShell"));
        var isResult = Boolean(panel("ResultShell"));
        if (!isSetup && !isResult) {
            return;
        }
        var render = isSetup ? renderSetup : renderResult;
        var readyToRender = function (table, key, payload) {
            if (!root.IsValid()) {
                return;
            }
            if (key === "state") {
                state = payload || {};
                queuePending = false;
            } else if (key === "history") {
                history = payload || {};
            } else if (key === "social") {
                social = payload;
            } else if (key === "coaches") {
                coaches = payload;
            } else if (key === "result") {
                savedResult = payload;
            } else if (key === "result_preview") {
                previewResult = payload;
            }
            render();
        };
        CustomNetTables.SubscribeNetTableListener("offline_league", readyToRender);
        state = CustomNetTables.GetTableValue("offline_league", "state") || {};
        history = CustomNetTables.GetTableValue("offline_league", "history") || {};
        social = CustomNetTables.GetTableValue("offline_league", "social");
        coaches = CustomNetTables.GetTableValue("offline_league", "coaches");
        savedResult = CustomNetTables.GetTableValue("offline_league", "result");
        previewResult = CustomNetTables.GetTableValue("offline_league", "result_preview");
        if (isSetup) {
            GameEvents.Subscribe("offline_league_archive", receiveArchive);
            GameEvents.Subscribe("offline_league_history_loaded", function (data) {
                if (root.IsValid() && integer(data.offset) === historyRequest) {
                    historyRequest = null;
                    setText("HistoryFeedback", data.message || "");
                    renderSetup();
                }
            });
            activate("HistoryPrevious", function () { requestHistory(Math.max(0, integer(history.offset) - 12)); });
            activate("HistoryNext", function () { requestHistory((integer(history.offset) || 0) + 12); });
            activate("ArchiveBack", function () {
                selectedTab = "history";
                renderHubTabs();
            });
            activate("ArchiveReplay", function () {
                if (archiveResult && archiveResult.replay) {
                    send("offline_league_watch_replay", {id: archiveResult.id});
                }
            });
            activate("HistoryTab", function () {
                selectedTab = "history";
                renderHubTabs();
            });
            activate("SocialTab", function () {
                selectedTab = "social";
                renderHubTabs();
            });
            activate("SettingsTab", function () {
                selectedTab = "settings";
                renderHubTabs();
                $.Schedule(0.05, function () {
                    if (root.IsValid() && selectedTab === "settings" && tuningDraft) {
                        updateSliders();
                    }
                });
            });
            tuningControls.forEach(function (control) {
                var slider = panel(control.slider);
                slider.SetPanelEvent("onvaluechanged", function () {
                    if (updatingSliders || !canEditTuning() || !tuningDraft) {
                        return;
                    }
                    var value = integer(slider.value);
                    tuningDraft[control.key] = Math.max(0, Math.min(control.maximum, value === null ? control.maximum : value));
                    tuningRequest = null;
                    tuningFeedback = "";
                    renderSetup();
                });
            });
            activate("MaxSettingsButton", function () {
                if (canEditTuning()) {
                    tuningDraft = normalizedTuning();
                    tuningRequest = null;
                    tuningFeedback = "";
                    updateSliders();
                    renderSetup();
                }
            });
            activate("SaveSettingsButton", function () {
                if (!canEditTuning() || !tuningDirty()) {
                    return;
                }
                tuningRequest = normalizedTuning(tuningDraft);
                tuningSaving = true;
                tuningFeedback = "";
                send("offline_league_settings", tuningRequest);
                var requested = tuningRequest;
                $.Schedule(12.0, function () {
                    if (root.IsValid() && tuningSaving && tuningRequest === requested) {
                        tuningSaving = false;
                        tuningRequest = null;
                        tuningFeedback = "Сохранение не подтверждено. Повтори попытку.";
                        renderSetup();
                    }
                });
                renderSetup();
            });
            denominations.forEach(function (_, role) {
                activate("Role" + role, function () {
                    if (state.status === "ready") {
                        selectedRole = role;
                        renderSetup();
                    }
                });
            });
            activate("QueueButton", function () {
                if (panel("QueueButton").enabled) {
                    queuePending = true;
                    renderSetup();
                    var activeRole = state.match && integer(state.match.role);
                    send("offline_league_queue", {role: activeRole || selectedRole});
                    $.Schedule(12, function () {
                        if (root.IsValid() && queuePending) {
                            queuePending = false;
                            renderSetup();
                        }
                    });
                }
            });
            activate("CancelButton", function () {
                send("offline_league_cancel");
            });
            activate("RefreshButton", function () {
                send("offline_league_refresh");
            });
        } else {
            activate("RetrySaveButton", function () {
                panel("RetrySaveButton").enabled = false;
                send("offline_league_refresh");
            });
            activate("RematchButton", function () {
                returnToLeague();
            });
            $.Schedule(0.0, pollPostgame);
            $.Schedule(8.0, function () {
                if (root.IsValid() && isPostGame() && !savedResult) {
                    saveWaitExpired = true;
                    renderResult();
                }
            });
        }
        render();
    }

    $.Schedule(0.0, start);
}());
