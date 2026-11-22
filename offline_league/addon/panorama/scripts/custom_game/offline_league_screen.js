(function () {
    "use strict";

    function expandSetup() {
        var panel = $.GetContextPanel();
        while (panel && !panel.BHasClass("CustomUIContainerGameSetup")) {
            panel = panel.GetParent();
        }
        if (!panel) {
            return;
        }

        // Dota reserves a left gutter for its dashboard button in GameSetup.
        var container = panel.FindChild("CustomUIContainer");
        if (container) {
            container.style.marginLeft = "0px";
        }
        for (var index = 0; index < panel.GetChildCount(); index++) {
            var child = panel.GetChild(index);
            if (child.BHasClass("DOTAReturnToDashboardOverlay")) {
                child.style.visibility = "collapse";
                break;
            }
        }
    }

    $.Schedule(0.0, expandSetup);
}());
