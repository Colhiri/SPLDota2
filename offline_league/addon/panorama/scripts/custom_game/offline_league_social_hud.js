(function () {
    "use strict";

    var root = $.GetContextPanel();
    var nameLabel = root.FindChildTraverse("LeagueSocialName");
    var relationLabel = root.FindChildTraverse("LeagueSocialRelation");
    var messageLabel = root.FindChildTraverse("LeagueSocialMessage");
    var lastLine;
    var displayedLine;

    function playing() {
        return Game.GetState() === DOTA_GameState.DOTA_GAMERULES_STATE_GAME_IN_PROGRESS;
    }

    function hide() {
        displayedLine = null;
        root.RemoveClass("LeagueSocialVisible");
    }

    function lineKey(line) {
        return line && line.sequence !== undefined ? (line.id || "") + ":" + line.sequence : null;
    }

    function showLine(line) {
        var key = lineKey(line);
        if (!key || key === lastLine || !playing() || !line.name || !line.text) {
            return;
        }
        var state = CustomNetTables.GetTableValue("offline_league", "state");
        if (line.id && state && state.match && state.match.id !== line.id) {
            return;
        }

        lastLine = key;
        displayedLine = key;
        nameLabel.text = line.name;
        relationLabel.text = line.relation || "";
        messageLabel.text = line.text;
        root.AddClass("LeagueSocialVisible");

        $.Schedule(6, function () {
            if (root.IsValid() && displayedLine === key) {
                hide();
            }
        });
    }

    lastLine = lineKey(CustomNetTables.GetTableValue("offline_league", "social_line"));
    CustomNetTables.SubscribeNetTableListener("offline_league", function (table, key, value) {
        if (key === "social_line" && root.IsValid()) {
            showLine(value);
        }
    });
    GameEvents.Subscribe("game_rules_state_change", function () {
        if (root.IsValid() && !playing()) {
            hide();
        }
    });
}());
