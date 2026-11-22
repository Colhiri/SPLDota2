(function () {
    "use strict";

    var root = $.GetContextPanel();
    var card = root.FindChildTraverse("LeagueAllyInspection");
    var title = card.FindChildTraverse("LeagueInspectedHeroName");
    var portrait = card.FindChildTraverse("LeagueInspectedHeroPortrait");
    var items = card.FindChildTraverse("LeagueInspectedItems");
    var slots = [];
    var inspected = -1;
    var refreshing = false;

    function createItemSlot(index) {
        var slot = $.CreatePanel("DOTAItemImage", items, "");
        slot.AddClass(index < 6 ? "LeagueInspectItem" : "LeagueInspectBackpack");
        slot.SetPanelEvent("onmouseover", function () {
            if (slot.itemname) {
                $.DispatchEvent("DOTAShowAbilityTooltip", slot, slot.itemname);
            }
        });
        slot.SetPanelEvent("onmouseout", function () {
            $.DispatchEvent("DOTAHideAbilityTooltip", slot);
        });
        return slot;
    }

    for (var i = 0; i < 9; i++) {
        slots.push(createItemSlot(i));
    }

    function closeInspection() {
        if (inspected < 0) {
            return;
        }
        inspected = -1;
        card.AddClass("LeagueLeaveHidden");
    }

    function matchIsActive() {
        var phase = Game.GetState();
        return !root.BHasClass("LeagueLeaveHidden") &&
            (phase === DOTA_GameState.DOTA_GAMERULES_STATE_PRE_GAME ||
             phase === DOTA_GameState.DOTA_GAMERULES_STATE_GAME_IN_PROGRESS);
    }

    function refreshItems() {
        if (inspected < 0 || !matchIsActive() || !Entities.IsValidEntity(inspected) ||
            GameUI.GetClickBehaviors() !== CLICK_BEHAVIORS.DOTA_CLICK_BEHAVIOR_NONE) {
            closeInspection();
            refreshing = false;
            return;
        }
        for (var i = 0; i < slots.length; i++) {
            var item = Entities.GetItemInSlot(inspected, i);
            slots[i].itemname = item >= 0 ? Abilities.GetAbilityName(item) : "";
        }
        $.Schedule(0.1, refreshItems);
    }

    function isOtherAlliedHero(player, localHero, selected) {
        if (selected === localHero || !Entities.IsValidEntity(selected) ||
            Entities.GetTeamNumber(selected) !== Entities.GetTeamNumber(localHero)) {
            return false;
        }
        for (var id = 0; id < Players.GetMaxPlayers(); id++) {
            if (id !== player && Players.GetPlayerHeroEntityIndex(id) === selected) {
                return true;
            }
        }
        return false;
    }

    function onSelection() {
        if (!matchIsActive()) {
            closeInspection();
            return;
        }
        var player = Players.GetLocalPlayer();
        var ownHero = Players.GetPlayerHeroEntityIndex(player);
        var selection = Players.GetSelectedEntities(player);
        if (selection.length !== 1 || ownHero < 0) {
            closeInspection();
            return;
        }
        var selected = selection[0];
        if (!isOtherAlliedHero(player, ownHero, selected)) {
            if (selected !== ownHero) {
                closeInspection();
            }
            return;
        }

        // Inspect the ally independently so the next native order uses our hero.
        inspected = selected;
        var name = Entities.GetUnitName(selected);
        title.text = $.Localize("#" + name);
        portrait.heroname = name;
        card.RemoveClass("LeagueLeaveHidden");
        GameUI.SelectUnit(ownHero, false);
        if (!refreshing) {
            refreshing = true;
            refreshItems();
        }
    }

    GameEvents.Subscribe("dota_player_update_selected_unit", onSelection);
    GameEvents.Subscribe("dota_player_update_query_unit", function () {
        if (Players.GetQueryUnit(Players.GetLocalPlayer()) >= 0) {
            closeInspection();
        }
    });
    function onPlayerAction(event) {
        if (event.PlayerID === Players.GetLocalPlayer()) {
            closeInspection();
        }
    }

    GameEvents.Subscribe("dota_player_used_ability", onPlayerAction);
    GameEvents.Subscribe("dota_item_used", onPlayerAction);
    GameUI.SetMouseCallback(function (eventType, button) {
        if (eventType === "pressed" && button === 1) {
            closeInspection();
        }
        return false;
    });
}());
