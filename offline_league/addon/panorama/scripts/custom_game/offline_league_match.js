(function () {
    "use strict";

    var root = $.GetContextPanel();
    var draft = root.BHasClass("LeagueDraftHud");
    var leaveButton = root.FindChildTraverse("LeagueLeaveButton");
    var backdrop = root.FindChildTraverse("LeagueLeaveBackdrop");
    var stayButton = root.FindChildTraverse("LeagueStayButton");
    var confirmButton = root.FindChildTraverse("LeagueLeaveConfirm");
    var confirmLabel = root.FindChildTraverse("LeagueLeaveConfirmLabel");
    var errorLabel = root.FindChildTraverse("LeagueLeaveError");
    var status = "";
    var open = false;
    var pending = false;
    var requestNumber = 0;
    var error = "";
    var nativeArrow = null;

    function show(panel, visible) {
        panel.SetHasClass("LeagueLeaveHidden", !visible);
    }

    function activeMount() {
        var phase = Game.GetState();
        if (draft) {
            return phase === DOTA_GameState.DOTA_GAMERULES_STATE_HERO_SELECTION ||
                phase === DOTA_GameState.DOTA_GAMERULES_STATE_STRATEGY_TIME ||
                phase === DOTA_GameState.DOTA_GAMERULES_STATE_TEAM_SHOWCASE;
        }
        return phase === DOTA_GameState.DOTA_GAMERULES_STATE_PRE_GAME ||
            phase === DOTA_GameState.DOTA_GAMERULES_STATE_GAME_IN_PROGRESS;
    }

    function isMatchActive() {
        return status === "starting" || status === "playing";
    }

    function findNativeArrow() {
        var hud = root.GetParent();
        while (hud && hud.paneltype !== "DOTAHud" && hud.id !== "Hud") {
            hud = hud.GetParent();
        }
        if (!hud) {
            return null;
        }

        // The hero picker and live HUD each own a separate DashboardButton.
        var nativeType = draft ? "DOTAHudPreGame" : "DOTAHudMenuButtons";
        var panels = [hud];
        while (panels.length) {
            var panel = panels.pop();
            if (panel.paneltype === nativeType) {
                var bar = draft ? panel : panel.FindChild("ButtonBar");
                return bar && bar.FindChild("DashboardButton");
            }
            for (var index = 0; index < panel.GetChildCount(); index++) {
                panels.push(panel.GetChild(index));
            }
        }
        return null;
    }

    function restoreNativeArrow() {
        if (nativeArrow && nativeArrow.IsValid()) {
            var arrow = nativeArrow;
            arrow.SetPanelEvent("onactivate", function () {
                $.DispatchEvent("DOTAHUDShowDashboard");
            });
            arrow.SetPanelEvent("onmouseover", function () {
                $.DispatchEvent("UIShowTextTooltip", arrow, $.Localize("#DOTA_HUD_BackToDashboard"));
            });
            arrow.SetPanelEvent("onmouseout", function () {
                $.DispatchEvent("UIHideTextTooltip", arrow);
            });
        }
        nativeArrow = null;
    }

    function setReturnEvents(arrow, isStock) {
        arrow.SetPanelEvent("onactivate", function () {
            $.DispatchEvent("UIHideTextTooltip", arrow);
            if (!root.IsValid() || !activeMount() || !isMatchActive()) {
                if (isStock && (!root.IsValid() || !activeMount())) {
                    $.DispatchEvent("DOTAHUDShowDashboard");
                }
                return;
            }
            if (!open) {
                open = true;
                error = "";
                render();
            }
        });
        arrow.SetPanelEvent("onmouseover", function () {
            $.DispatchEvent("UIShowTextTooltip", arrow, "Выйти в лигу");
        });
        arrow.SetPanelEvent("onmouseout", function () {
            $.DispatchEvent("UIHideTextTooltip", arrow);
        });
    }

    function syncNativeArrow(active) {
        if (!active) {
            restoreNativeArrow();
        } else if (!nativeArrow || !nativeArrow.IsValid()) {
            nativeArrow = findNativeArrow();
            if (nativeArrow) {
                setReturnEvents(nativeArrow, true);
            }
        }
    }

    function render() {
        var leaving = status === "leaving";
        var working = pending || leaving;
        var active = activeMount() && (isMatchActive() || leaving);
        syncNativeArrow(active);
        show(root, active);
        show(leaveButton, !nativeArrow && !open);
        show(backdrop, open);
        show(errorLabel, Boolean(error));
        errorLabel.text = error;
        confirmLabel.text = working ? "Возвращаемся…" : (error ? "Повторить" : "Выйти в лигу");
        confirmButton.enabled = !working;
        stayButton.enabled = !working;
    }

    function receiveState(state) {
        var wasLeaving = pending || status === "leaving";
        status = (state && state.status) || "";
        if (status === "leaving") {
            open = true;
            pending = false;
            error = "";
        } else if (isMatchActive() && open && wasLeaving && state.message) {
            pending = false;
            error = state.message;
        } else if (!isMatchActive()) {
            open = false;
            pending = false;
            error = "";
        }
        render();
    }

    function confirmLeave() {
        if (!open || pending || !isMatchActive() || !activeMount()) {
            return;
        }

        var currentRequest = ++requestNumber;
        pending = true;
        error = "";
        render();
        GameEvents.SendCustomGameEventToServer("offline_league_leave", {});
        $.Schedule(14, function () {
            if (currentRequest !== requestNumber || !pending || !isMatchActive()) {
                return;
            }
            pending = false;
            error = "Возврат в лигу не подтверждён. Попробуй ещё раз.";
            render();
        });
    }

    setReturnEvents(leaveButton, false);
    stayButton.SetPanelEvent("onactivate", function () {
        if (!pending && isMatchActive() && activeMount()) {
            open = false;
            error = "";
            render();
        }
    });
    confirmButton.SetPanelEvent("onactivate", confirmLeave);

    GameEvents.Subscribe("game_rules_state_change", render);
    CustomNetTables.SubscribeNetTableListener("offline_league", function (table, key, data) {
        if (key === "state") {
            receiveState(data);
        }
    });
    receiveState(CustomNetTables.GetTableValue("offline_league", "state"));
    $.Schedule(0.2, render);
}());
