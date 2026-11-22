"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "addon/panorama/scripts/custom_game/offline_league.js"), "utf8");
const layout = fs.readFileSync(path.join(__dirname, "addon/panorama/layout/custom_game/offline_league_result.xml"), "utf8");
const manifest = fs.readFileSync(path.join(__dirname, "addon/panorama/layout/custom_game/custom_ui_manifest.xml"), "utf8");

function mount(phase, initialState, missingPhase = false) {
    const scheduled = [];
    const panels = new Map();
    const sent = [];
    let listener;
    let gamePhase = phase;
    const stack = [];
    let root;
    for (const tag of layout.matchAll(/<(\/?)(Panel|Label|Button)\b([^>]*?)(\/?)>/g)) {
        if (tag[1]) {
            stack.pop();
            continue;
        }
        const attributes = Object.fromEntries([...tag[3].matchAll(/([\w]+)="([^"]*)"/g)].map(item => [item[1], item[2]]));
        const node = {id: attributes.id || "", children: [], style: {}, classes: new Set((attributes.class || "").split(" ")), enabled: true,
                SetHasClass(name, value) { value ? this.classes.add(name) : this.classes.delete(name); },
                SetPanelEvent(name, callback) { this[name] = callback; },
                RemoveAndDeleteChildren() { this.children = []; },
                IsValid() { return true; },
                FindChildTraverse(id) {
                    for (const child of this.children) {
                        if (child.id === id) return child;
                        const found = child.FindChildTraverse?.(id);
                        if (found) return found;
                    }
                    return null;
                }};
        node.style.visibility = /visibility:\s*collapse/.test(attributes.style || "") ? "collapse" : "visible";
        node.hittest = attributes.hittest !== "false";
        node.hittestchildren = attributes.hittestchildren !== "false";
        if (stack.length) stack[stack.length - 1].children.push(node);
        else root = node;
        if (node.id) panels.set(node.id, node);
        if (!tag[4]) stack.push(node);
    }
    const canvas = panels.get("LeagueResultCanvas");
    assert.notEqual(canvas, root, "The engine-owned HUD mount must not also be the result canvas");
    function engineShowsHud() {
        root.id = "Hud";
        root.style.visibility = "visible";
        root.hittest = false;
        root.hittestchildren = true;
    }
    engineShowsHud();
    const context = {
        $: {
            GetContextPanel: () => root,
            Localize: (token) => token,
            Schedule: (_, callback) => scheduled.push(callback),
            CreatePanel(_, parent) {
                const child = {
                    children: [],
                    AddClass() {},
                    SetHasClass() {},
                };
                parent.children.push(child);
                return child;
            },
        },
        Game: {GetState: () => gamePhase, GetLocalPlayerID: () => 0},
        DOTA_GameState: missingPhase ? {} : {DOTA_GAMERULES_STATE_GAME_IN_PROGRESS: 7, DOTA_GAMERULES_STATE_POST_GAME: 8},
        CustomNetTables: {
            SubscribeNetTableListener: (_, callback) => { listener = callback; },
            GetTableValue: (_, key) => key === "state" ? initialState : null,
        },
        GameEvents: {SendCustomGameEventToServer(event) { sent.push(event); }},
    };
    vm.runInNewContext(source, context, {filename: "offline_league.js"});
    scheduled.shift()();
    engineShowsHud();
    return {root: canvas, panels, sent, tick: () => scheduled.shift()(), update(nextPhase, state, key = "state") {
        engineShowsHud();
        gamePhase = nextPhase;
        listener("offline_league", key, state);
        engineShowsHud();
    }};
}

for (const phase of [0, 6, 7]) {
    for (const status of ["starting", "playing", "saving", "finished", "error"]) {
        const {root} = mount(phase, {status, retry_save: true});
        assert.equal(root.style.visibility, "collapse", `Result overlay appeared during phase ${phase}, status ${status}`);
        assert.equal(root.hittest, false);
        assert.equal(root.hittestchildren, false);
    }
}

const {root, update} = mount(7, {status: "playing"});
update(8, {status: "saving", message: "Saving result"});
assert.equal(root.style.visibility, "visible");
assert.equal(root.hittest, true);
update(6, {status: "finished", retry_save: true});
assert.equal(root.style.visibility, "collapse", "A previous result must not cover a new match");
assert.equal(root.hittest, false);
assert.equal(mount(undefined, {}, true).root.style.visibility, "collapse", "Missing phase constants must fail closed");

assert.match(layout, /id="LeagueResultCanvas"[^>]*style="visibility: collapse;"[^>]*hittest="false"[^>]*hittestchildren="false"/);
assert.match(manifest, /type="EndScreen"[^>]*offline_league_result\.xml/);

const finished = mount(7, {status: "saving"});
finished.update(7, {
    id: "match", winner: 2, won: true, duration_seconds: 185,
    participants: [{team: 2, hero: "npc_dota_hero_sniper", kills: 0}, {team: 3, deaths: 0}],
}, "result");
finished.update(7, {status: "finished"});
assert.match(finished.panels.get("ResultSubtitle").text, /Матч сохранён · 3:05/);
assert.equal(finished.panels.get("RematchButton").style.visibility, "visible");
assert.equal(finished.panels.get("ResultScoreboard").children.length, 2);
for (let tick = 0; tick < 12; tick++) finished.tick();
assert.deepEqual(finished.sent, [], "A saved result remains open until the player acts");
finished.panels.get("RematchButton").onactivate();
assert.deepEqual(finished.sent, ["offline_league_rematch"]);
console.log("Postgame mounting, saved scoreboard and manual return checks passed.");
