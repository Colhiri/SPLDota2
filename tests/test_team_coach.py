import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from team_coach.console_telemetry import ConsoleTelemetry, LuaMailbox
from team_coach.service import CoachService, make_server
from team_coach.shadow import ShadowObserver


class MatchClock:
    def __init__(self):
        self.wall = 1000

    def __call__(self):
        return self.wall

    def advance(self, seconds):
        self.wall += seconds


def lane(*, under_attack=False, own_tower=2):
    return {
        "ally_front": 0.40,
        "enemy_front": 0.76 if under_attack else 0.45,
        "own_tower": own_tower,
        "enemy_tower": 2,
        "own_tower_hp": 0.38 if under_attack else 0.94,
        "enemy_tower_hp": 0.89,
        "enemy_near_own": 2 if under_attack else 0,
        "ally_near_enemy": 0,
        "enemy_wave_near_own": 4 if under_attack else 0,
    }


def report(
    clock_s=950, *, bot_id=0, threat="mid", team="radiant", applied=None, mid_tier=2, enemies_alive=True,
    push_window=False, tuning=None,
):
    result = {
        "team": team,
        "clock_s": clock_s,
        "bot": {
            "id": bot_id,
            "hero": ("npc_dota_hero_axe", "npc_dota_hero_lion", "npc_dota_hero_oracle")[bot_id],
            "role": (3, 4, 5)[bot_id],
            "alive": True,
            "hp": 0.94,
            "zone": "mid",
            "mode": "farm",
            "tp_ready": True,
        },
    }
    if bot_id == 0:
        result["map"] = {
            "lanes": {
                name: lane(under_attack=name == threat, own_tower=mid_tier if name == "mid" else 2)
                for name in ("top", "mid", "bot")
            },
            "enemies": [
                {"id": 5, "hero": "npc_dota_hero_sniper", "alive": enemies_alive, "zone": threat or "unknown", "seen_s": 0},
                {"id": 6, "hero": "npc_dota_hero_sven", "alive": enemies_alive, "zone": threat or "unknown", "seen_s": 0},
            ],
        }
        if push_window:
            result["map"]["lanes"]["mid"].update(
                ally_front=0.82, ally_near_enemy=3, ally_wave_near_enemy=5, enemy_near_enemy=0,
            )
        if tuning is not None:
            result["map"]["tuning"] = tuning
    if applied is not None:
        result["applied"] = applied
        result["bot"]["mode"] = f'{applied["objective"]}_{applied["lane"]}'
    return result


def defend(context):
    choice = next(choice for choice in context["choices"] if choice["objective"] == "defend")
    return {
        "objective": choice["objective"],
        "lane": choice["lane"],
        "bot_ids": choice["bot_ids"][:2],
        "ttl_s": 30,
        "reason": "Cover the threatened tower.",
    }, {"input_tokens": 120, "output_tokens": 42}


class ConsoleTelemetryTests(unittest.TestCase):
    @staticmethod
    def fragments(packet):
        payload = json.dumps(packet, separators=(",", ":")).encode()
        chunks = [payload[start:start + 600] for start in range(0, len(payload), 600)]
        return [
            f"11/17 23:05:06 [VScript] [team-coach-snapshot] {index}/{len(chunks)} ".encode()
            + chunk + b"\r\n"
            for index, chunk in enumerate(chunks, 1)
        ]

    def test_follows_fresh_complete_snapshots_and_ends_match(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "offline_league.log"
            path.write_bytes(b"".join(self.fragments(report(50))))
            journal = []
            service = CoachService(mode="shadow", journal=journal.append)
            telemetry = ConsoleTelemetry(path)
            telemetry.ingest(service)
            self.assertEqual(service.status()["phase"], "idle")

            fragments = self.fragments(report(12))
            self.assertGreater(len(fragments), 1)
            with path.open("ab") as stream:
                stream.write(b"".join(fragments)[:-2])
            telemetry.ingest(service)
            self.assertEqual(service.status()["phase"], "idle")

            with path.open("ab") as stream:
                stream.write(b"\r\n")
                stream.writelines(self.fragments(report(12, bot_id=1)))
            telemetry.ingest(service)
            service.observe()
            status = service.status()
            self.assertEqual(status["phase"], "active")
            self.assertEqual(status["context"]["clock_s"], 12)
            self.assertEqual([bot["id"] for bot in status["context"]["allies"]], [0, 1])
            self.assertIsNone(status["plan"])

            with path.open("ab") as stream:
                stream.write(b"S:Gamerules: entering state 'DOTA_GAMERULES_STATE_POST_GAME'\n")
            telemetry.ingest(service)
            self.assertEqual(service.status()["phase"], "idle")
            self.assertEqual([row["event"] for row in journal],
                             ["match_connected", "observation", "match_ended"])

            path.write_bytes(b"".join(self.fragments(report(3))))
            telemetry.ingest(service)
            self.assertEqual(service.status()["context"]["clock_s"], 3)
            self.assertNotEqual(service.status()["session_id"], status["session_id"])

    def test_file_replies_track_each_bot_and_clear_canceled_commands(self):
        with TemporaryDirectory() as tmp:
            log = Path(tmp) / "dota.log"
            replies = Path(tmp) / "replies.lua"
            service = CoachService(mode="observe")
            telemetry = ConsoleTelemetry(log, LuaMailbox(replies))

            def snapshot(bot_id, clock_s):
                packet = report(clock_s, bot_id=bot_id)
                packet["request_id"] = f"{bot_id + 10}-{clock_s}"
                packet["bot"].update(hp=0.51, under_fire=True, desires={"retreat": 0.5})
                with log.open("ab") as stream:
                    stream.writelines(self.fragments(packet))

            snapshot(0, 950)
            snapshot(1, 950)
            telemetry.ingest(service)
            session = service.status()["session_id"]
            command = service.submit_command({
                "session_id": session, "clock_s": 950, "action": "retreat",
                "target": {"kind": "self"}, "bot_ids": [1], "ttl_s": 8, "reason": "Disengage.",
            })

            snapshot(0, 952)
            snapshot(1, 952)
            telemetry.ingest(service)
            lines = replies.read_text(encoding="ascii").splitlines()
            self.assertIn('[0] = {"10-952"', lines[1])
            self.assertNotIn(command["id"], lines[1])
            self.assertIn('[1] = {"11-952"', lines[2])
            self.assertIn(command["id"], lines[2])

            service.cancel_command({"session_id": session, "command_id": command["id"]})
            snapshot(1, 954)
            telemetry.ingest(service)
            self.assertNotIn(command["id"], replies.read_text(encoding="ascii"))

            with log.open("ab") as stream:
                stream.write(b"[Server] SV:  Server shutting down:\n")
            telemetry.ingest(service)
            self.assertEqual(replies.read_text(encoding="ascii"), "return {\n}\n")
            self.assertEqual(service.status()["phase"], "idle")

    def test_shadow_does_not_publish_even_when_a_mailbox_is_supplied(self):
        with TemporaryDirectory() as tmp:
            log = Path(tmp) / "dota.log"
            replies = Path(tmp) / "replies.lua"
            service = CoachService(mode="shadow")
            telemetry = ConsoleTelemetry(log, LuaMailbox(replies))
            log.write_bytes(b"".join(self.fragments({**report(), "request_id": "100-1"})))
            telemetry.ingest(service)
            self.assertEqual(service.status()["phase"], "active")
            self.assertFalse(replies.exists())


class ShadowObserverTests(unittest.TestCase):
    def setUp(self):
        self.observer = ShadowObserver()

    def context(self, clock_s, **state):
        packets = [
            report(clock_s, bot_id=bot_id, threat=None, **state)
            for bot_id in range(3)
        ]
        context = {
            "clock_s": clock_s,
            "team": "radiant",
            "allies": [packet["bot"] for packet in packets],
            **packets[0]["map"],
            "choices": [],
        }
        if state.get("push_window"):
            context["choices"].append({"objective": "push", "lane": "mid", "bot_ids": [0, 1, 2]})
        return context

    def test_push_window_needs_continuous_evidence_and_acknowledges_existing_push(self):
        def opportunity(clock_s):
            return self.context(clock_s, push_window=True, enemies_alive=False)

        self.assertEqual(self.observer.update(opportunity(950)), [])
        self.assertEqual(self.observer.update(opportunity(963)), [])
        self.assertEqual(self.observer.update(opportunity(967)), [])
        ongoing = opportunity(971)
        ongoing["allies"][0]["mode"] = "push_mid"
        self.assertEqual(self.observer.update(ongoing), [])

        self.assertEqual(self.observer.update(opportunity(975)), [])
        self.assertEqual(self.observer.update(opportunity(979)), [])
        event, = self.observer.update(opportunity(983))
        self.assertEqual((event["kind"], event["lane"]), ("push_window_unclaimed", "mid"))
        self.assertGreaterEqual(event["duration_s"], 8)
        self.assertEqual(len(event["evidence"]["enemies_down"]), 2)
        self.assertEqual(self.observer.update(opportunity(987)), [])
        self.assertEqual(self.observer.recent(), [event])

    def test_isolated_commitment_reports_the_risk_once_after_sustained_pressure(self):
        def fight(clock_s, *, under_fire=True):
            context = self.context(clock_s)
            context["allies"][0].update(
                mode="attack", hp=0.60, nearby_allies=0, nearby_enemies=3, under_fire=under_fire,
            )
            return context

        self.assertEqual(self.observer.update(fight(950)), [])
        self.assertEqual(self.observer.update(fight(952, under_fire=False)), [])
        self.assertEqual(self.observer.update(fight(954)), [])
        event, = self.observer.update(fight(958))
        self.assertEqual((event["kind"], event["bot_id"]), ("isolated_commitment", 0))
        self.assertEqual(event["evidence"]["nearby_enemies"], 3)
        self.assertTrue(event["evidence"]["under_fire"])
        self.assertEqual(self.observer.update(fight(964)), [])

    def test_ultimate_active_and_cooldown_start_require_consecutive_observations(self):
        def ability(clock_s, cooldown_s, *, active=False):
            context = self.context(clock_s)
            context["allies"][0]["ultimates"] = [{
                "name": "axe_culling_blade", "level": 2, "cooldown_s": cooldown_s, "active": active,
            }]
            return context

        self.assertEqual(self.observer.update(ability(950, 30)), [])
        self.assertEqual(self.observer.update(ability(954, 0)), [])
        cooldown, = self.observer.update(ability(956, 80))
        self.assertEqual((cooldown["kind"], cooldown["bot_id"]), ("ultimate_cooldown_started", 0))
        self.assertEqual(cooldown["evidence"]["ability"], "axe_culling_blade")
        self.assertFalse(cooldown["evidence"]["active"])
        active, = self.observer.update(ability(958, 78, active=True))
        self.assertEqual((active["kind"], active["bot_id"]), ("ultimate_active", 0))

        self.observer.update(None)
        self.assertEqual(self.observer.update(ability(970, 0)), [])
        self.assertEqual(self.observer.update(ability(980, 71)), [])
        self.assertEqual(len(self.observer.recent()), 2)


class TeamCoachTests(unittest.TestCase):
    def setUp(self):
        self.clock = MatchClock()

    def coach(self, decision=defend, **kwargs):
        return CoachService(coach=decision, clock=self.clock, **kwargs)

    def snapshot(self, service, clock_s=950, **kwargs):
        service.exchange(report(clock_s, bot_id=2, **kwargs))
        service.exchange(report(clock_s, bot_id=1, **kwargs))
        return service.exchange(report(clock_s, **kwargs))

    def bot_update(self, service, *, bot_id=1, clock_s=950, **fields):
        packet = report(clock_s, bot_id=bot_id)
        packet["bot"].update(fields)
        return service.exchange(packet)

    def command(self, service, **fields):
        return {
            "session_id": service.status()["session_id"],
            "clock_s": 950,
            "action": "defend",
            "target": {"kind": "lane", "lane": "mid"},
            "bot_ids": [1, 2],
            "ttl_s": 30,
            "reason": "Protect the mid tower.",
            **fields,
        }

    def test_plan_round_trip_tracks_applied_bot_and_expires_in_match_time(self):
        service = self.coach()
        session = self.snapshot(service)["session_id"]
        plan = service.review()
        self.assertEqual((plan["objective"], plan["lane"]), ("defend", "mid"))
        bot_id = plan["bot_ids"][0]
        applied = {"plan_id": plan["id"], "objective": "defend", "lane": "mid"}
        reply = service.exchange(report(951, bot_id=bot_id, applied=applied))
        self.assertEqual(reply, {"session_id": session, "plan": plan})
        self.assertEqual(service.status()["applied"], [bot_id])
        unrelated_bot = next(candidate for candidate in (0, 1, 2) if candidate not in plan["bot_ids"])
        service.exchange(report(952, bot_id=unrelated_bot, applied=applied))
        self.assertEqual(service.status()["applied"], [bot_id])

        self.clock.advance(3)
        self.assertEqual(self.snapshot(service, 952)["plan"], plan)
        self.assertIsNone(self.snapshot(service, plan["expires_clock_s"] + 1)["plan"])
        self.assertEqual(service.status()["applied"], [])

    def test_unchanged_state_skips_model_and_call_budget_caps_new_events(self):
        service = self.coach(cooldown_s=10, max_calls=2)
        self.snapshot(service)
        service.step()
        self.clock.advance(12)
        self.snapshot(service, 962)
        self.assertIsNone(service.step())
        self.assertEqual(service.status()["calls"], 1)

        self.snapshot(service, 963, threat="top")
        self.assertEqual(service.step()["lane"], "top")
        self.clock.advance(12)
        self.snapshot(service, 975, threat="bot")
        self.assertIsNone(service.step())
        self.assertEqual(service.status()["calls"], 2)

    def test_model_failure_consumes_budget_and_respects_cooldown(self):
        observations = []

        def failing_coach(context):
            observations.append(context)
            raise RuntimeError("Model unavailable")

        service = self.coach(failing_coach, cooldown_s=10, max_calls=2)
        self.snapshot(service)
        self.assertIsNone(service.step())
        self.snapshot(service, 951, threat="top")
        self.assertIsNone(service.step())
        self.assertEqual(service.status()["calls"], 1)
        self.assertEqual(len(observations), 1)

        self.clock.advance(11)
        self.snapshot(service, 962, threat="top")
        self.assertIsNone(service.step())
        self.assertEqual(service.status()["calls"], 2)

    def test_hold_uses_budget_without_publishing_an_order(self):
        def hold(_context):
            return {
                "objective": "hold", "lane": "none", "bot_ids": [], "ttl_s": 0,
                "reason": "Stay with current assignments.",
            }, {"input_tokens": 83, "output_tokens": 26}

        service = self.coach(hold, cooldown_s=5)
        self.snapshot(service)
        self.assertIsNone(service.step())
        self.assertIsNone(service.status()["plan"])
        self.assertEqual(service.status()["usage"]["input_tokens"], 83)
        self.clock.advance(6)
        self.snapshot(service, 956)
        self.assertIsNone(service.step())
        self.assertEqual(service.status()["calls"], 1)

    def test_delayed_advice_cannot_survive_invalidated_facts(self):
        started = Event()
        finish = Event()

        def slow_coach(context):
            started.set()
            finish.wait(timeout=5)
            return defend(context)

        service = self.coach(slow_coach)
        self.snapshot(service)
        result = []
        worker = Thread(target=lambda: result.append(service.step()))
        worker.start()
        try:
            self.assertTrue(started.wait(timeout=2))
            self.snapshot(service, 955, threat=None)
        finally:
            finish.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [None])
        self.assertIsNone(service.status()["plan"])

    def test_rewind_starts_new_session_and_discards_previous_plan(self):
        service = self.coach()
        old_session = self.snapshot(service)["session_id"]
        old_plan = service.review()
        self.assertIsNotNone(old_plan)

        reply = service.exchange(report(12))
        self.assertNotEqual(reply["session_id"], old_session)
        self.assertIsNone(reply["plan"])
        self.assertEqual(service.status()["calls"], 0)
        self.assertEqual(service.status()["reviews"], 0)

    def test_stale_map_suppresses_planning_and_active_plan(self):
        service = self.coach()
        self.snapshot(service)
        self.clock.advance(9)
        self.assertIsNone(service.step())
        self.assertIsNone(service.review())
        self.assertEqual(service.status()["calls"], 0)
        self.assertEqual(service.status()["reviews"], 0)

        self.snapshot(service)
        self.assertIsNotNone(service.review())
        self.clock.advance(9)
        self.assertIsNone(service.exchange(report(954, bot_id=1))["plan"])

    def test_review_runs_by_match_clock_and_renews_matching_plan(self):
        service = self.coach(mode="rules", decision_interval_s=15)
        self.snapshot(service)
        plan = service.review()
        self.assertIsNotNone(plan)
        self.assertEqual(service.status()["reviews"], 1)
        self.assertIsNone(service.step())
        for _ in range(4):
            self.clock.advance(5)
            self.snapshot(service, 950)
            service.review()
        self.assertEqual(service.status()["reviews"], 1)
        self.assertEqual(service.status()["plan"], plan)

        self.snapshot(service, 964)
        service.review()
        self.assertEqual(service.status()["reviews"], 1)
        self.snapshot(service, 965)
        renewed = service.review()
        self.assertEqual(service.status()["reviews"], 2)
        self.assertEqual(renewed["id"], plan["id"])
        self.assertGreater(renewed["expires_clock_s"], plan["expires_clock_s"])
        self.assertEqual(service.status()["calls"], 0)

    def test_review_counts_empty_game_states_at_interval(self):
        service = self.coach(mode="rules")
        self.snapshot(service, threat=None)
        self.assertIsNone(service.review())
        self.assertEqual(service.status()["reviews"], 1)
        self.snapshot(service, 965, threat=None)
        self.assertIsNone(service.review())
        self.assertEqual(service.status()["reviews"], 2)

    def test_review_uses_native_desire_to_select_defenders(self):
        service = self.coach(mode="rules")
        for bot_id, desire in ((2, 0.52), (1, 0.63), (0, 0)):
            packet = report(bot_id=bot_id, mid_tier=3)
            packet["bot"]["desires"] = {"defend_mid": desire}
            service.exchange(packet)

        plan = service.review()
        self.assertEqual((plan["objective"], plan["lane"]), ("defend", "mid"))
        self.assertEqual(set(plan["bot_ids"]), {1, 2})

    def test_tower_and_fight_changes_prompt_an_early_review(self):
        for change in ({"mid_tier": 3}, {"enemies_alive": False, "threat": None}):
            with self.subTest(change=change):
                service = self.coach(mode="rules")
                self.snapshot(service)
                plan = service.review()
                self.assertIsNotNone(plan)
                self.snapshot(service, 956, **change)
                self.assertIsNone(service.status()["plan"])
                service.review()
                self.assertEqual(service.status()["reviews"], 2)
                self.assertNotEqual(service.status()["plan"], plan)

    def test_local_review_continues_while_model_computes_hint(self):
        started = Event()
        finish = Event()
        result = []

        def slow_coach(context):
            started.set()
            finish.wait(timeout=5)
            decision, usage = defend(context)
            return {**decision, "bot_ids": [2]}, usage

        service = self.coach(slow_coach)
        self.snapshot(service)
        worker = Thread(target=lambda: result.append(service.step()))
        worker.start()
        try:
            self.assertTrue(started.wait(timeout=2))
            self.assertTrue(service.status()["pending"])
            plan = service.review()
            self.clock.advance(5)
            self.snapshot(service, 965)
            reviewed = service.review()
            self.assertEqual(plan["id"], reviewed["id"])
            self.assertEqual(service.status()["reviews"], 2)
        finally:
            finish.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        status = service.status()
        hint, = result
        self.assertEqual(status["strategy_hint"], hint)
        self.assertEqual(status["plan"], reviewed)
        self.assertEqual(hint["bot_ids"], [2])
        self.assertEqual(status["usage"]["input_tokens"], 120)

    def test_observe_mode_accepts_manual_plan_for_current_match(self):
        service = self.coach(mode="observe")
        session = self.snapshot(service)["session_id"]
        plan = {
            "session_id": session,
            "clock_s": 949,
            "objective": "defend",
            "lane": "mid",
            "bot_ids": [1, 2],
            "ttl_s": 30,
            "reason": "Rotate two supports to the threatened tower.",
        }
        self.assertIsNone(service.step())
        self.assertIsNone(service.review())
        self.assertEqual(service.status()["reviews"], 0)
        with self.assertRaisesRegex(ValueError, "stale match snapshot"):
            service.submit_plan({**plan, "clock_s": 935})
        with self.assertRaisesRegex(ValueError, "stale match snapshot"):
            service.submit_plan({**plan, "session_id": "old-match"})
        accepted = service.submit_plan(plan)
        self.assertEqual(service.exchange(report(951, bot_id=2))["plan"], accepted)
        self.assertEqual(service.status()["calls"], 0)

    def test_shadow_mode_observes_a_match_without_submitting_plans(self):
        calls = []
        journal = []

        def unexpected_coach(context):
            calls.append(context)
            return defend(context)

        service = self.coach(unexpected_coach, mode="shadow", journal=journal.append)
        session = self.snapshot(service)["session_id"]
        self.assertIsNone(service.step())
        self.assertIsNone(service.review())

        plan = {
            "session_id": session,
            "clock_s": 950,
            "objective": "defend",
            "lane": "mid",
            "bot_ids": [1, 2],
            "ttl_s": 30,
            "reason": "Rotate two supports to the threatened tower.",
        }
        with self.assertRaises(ValueError):
            service.submit_plan(plan)

        for clock_s in (951, 955, 959):
            self.snapshot(service, clock_s, threat=None, enemies_alive=False, push_window=True)
            service.observe()
        self.assertIsNone(service.step())
        self.assertIsNone(service.review())

        status = service.status()
        self.assertEqual(status["mode"], "shadow")
        self.assertGreater(status["observations"], 0)
        self.assertEqual(status["calls"], 0)
        self.assertEqual(status["reviews"], 0)
        self.assertIsNone(status["plan"])
        self.assertEqual(calls, [])
        self.assertNotIn("plan_accepted", {entry["event"] for entry in journal})
        kinds = [entry["kind"] for entry in status["events"]]
        self.assertIn("push_window_unclaimed", kinds)
        self.assertIn("push_window_unclaimed", {
            entry["kind"] for entry in journal if entry["event"] == "match_event"
        })

    def test_shadow_observer_does_not_infer_cooldown_transition_across_telemetry_outage(self):
        service = self.coach(mode="shadow")

        def snapshot_with_ultimate(clock_s, cooldown_s):
            self.snapshot(service, clock_s, threat=None)
            sample = report(clock_s, threat=None)
            sample["bot"]["ultimates"] = [{
                "name": "axe_culling_blade", "level": 2, "cooldown_s": cooldown_s, "active": False,
            }]
            service.exchange(sample)

        snapshot_with_ultimate(950, 0)
        self.assertEqual(service.observe(), [])
        self.clock.advance(9)
        self.assertEqual(service.observe(), [])
        snapshot_with_ultimate(954, 50)
        self.assertEqual(service.observe(), [])
        self.assertEqual(service.status()["events"], [])

    def test_typed_command_rejects_invalid_targets_and_wire_types(self):
        service = self.coach(mode="observe")
        self.snapshot(service)
        command = self.command(service)
        cases = (
            {"action": "focus", "target": {"kind": "enemy", "player_id": 5}},
            {"target": "mid"},
            {"target": {"kind": "self"}},
            {"target": {"kind": "lane", "lane": "center"}},
            {"target": {"kind": "lane", "lane": "mid", "x": 0}},
            {"log": True},
            {"ttl_s": True},
            {"ttl_s": 30.0},
            {"ttl_s": 14},
            {"bot_ids": [True]},
            {"bot_ids": [1, 1]},
            {"session_id": "old-match"},
            {"clock_s": 937},
            {"clock_s": 951},
        )
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                service.submit_command({**command, **invalid})
        self.assertIsNone(service.status()["plan"])
        accepted = service.submit_command(command)
        self.assertEqual(accepted["action"], "defend")
        self.assertEqual(accepted["target"], {"kind": "lane", "lane": "mid"})
        plan = service.status()["plan"]
        self.assertEqual((plan["objective"], plan["lane"]), ("defend", "mid"))
        self.assertEqual(plan["bot_ids"], [1, 2])

    def test_farm_command_reaches_only_assigned_bot_and_expires_in_game_time(self):
        service = self.coach(mode="observe")
        self.snapshot(service)
        command = self.command(
            service, action="farm", target={"kind": "self"}, bot_ids=[1], ttl_s=12,
        )
        with self.assertRaises(ValueError):
            service.submit_command(command)
        self.bot_update(
            service, desires={"farm": 0.55}, under_fire=True, nearby_enemies=0, nearby_allies=1,
        )
        with self.assertRaises(ValueError):
            service.submit_command(command)
        self.bot_update(
            service, desires={"farm": 0.55}, under_fire=False, nearby_enemies=0, nearby_allies=1,
        )
        accepted = service.submit_command(command)
        self.assertEqual(accepted["action"], "farm")
        self.assertNotIn("commands", service.exchange(report(bot_id=2)))
        actual = self.bot_update(
            service, desires={"farm": 0.55}, under_fire=False, nearby_enemies=0, nearby_allies=1,
        )
        self.assertEqual(actual["commands"], [accepted])

        self.clock.advance(4)
        paused = self.bot_update(
            service, desires={"farm": 0.55}, under_fire=False, nearby_enemies=0, nearby_allies=1,
        )
        self.assertEqual(paused["commands"], [accepted])
        expiry = accepted["expires_clock_s"]
        service.exchange(report(expiry))
        after_expiry = self.bot_update(
            service, clock_s=expiry, desires={"farm": 0.55}, under_fire=False, nearby_enemies=0,
        )
        self.assertNotIn("commands", after_expiry)

    def test_retreat_requires_native_mode_and_current_danger(self):
        service = self.coach(mode="observe")
        self.snapshot(service)
        command = self.command(
            service, action="retreat", target={"kind": "self"}, bot_ids=[1], ttl_s=5,
        )
        with self.assertRaises(ValueError):
            service.submit_command(command)
        self.bot_update(service, desires={"retreat": 0.7}, under_fire=False, nearby_enemies=0, nearby_allies=1)
        with self.assertRaises(ValueError):
            service.submit_command(command)

        self.bot_update(
            service, hp=0.4, mode="attack", desires={"retreat": 0.7},
            under_fire=True, nearby_enemies=3, nearby_allies=0,
        )
        with self.assertRaises(ValueError):
            service.submit_command({**command, "clock_s": 944})
        with self.assertRaises(ValueError):
            service.submit_command({**command, "clock_s": 945})
        accepted = service.submit_command({**command, "clock_s": 947})
        self.assertEqual(accepted["action"], "retreat")
        self.assertEqual(accepted["bot_ids"], [1])
        self.assertEqual(accepted["expires_clock_s"], 952)

    def test_sustained_isolation_issues_retreat_only_in_active_coaching_modes(self):
        for mode in ("rules", "shadow"):
            with self.subTest(mode=mode):
                service = self.coach(mode=mode)
                danger = {
                    "hp": 0.43, "mode": "attack", "desires": {"retreat": 0.7},
                    "under_fire": True, "nearby_enemies": 3, "nearby_allies": 0,
                }
                for clock_s in (950, 952, 954):
                    self.snapshot(service, clock_s, threat=None)
                    self.bot_update(service, clock_s=clock_s, **danger)
                    events = service.observe()
                self.assertIn("isolated_commitment", {event["kind"] for event in events})
                received = self.bot_update(service, clock_s=954, **danger)
                self.assertNotIn("commands", service.exchange(report(954, bot_id=2)))
                if mode == "rules":
                    retreat, = received["commands"]
                    self.assertEqual((retreat["action"], retreat["target"]), ("retreat", {"kind": "self"}))
                    self.assertEqual(retreat["bot_ids"], [1])
                else:
                    self.assertNotIn("commands", received)
                    self.assertEqual(service.status()["commands"], [])

    def test_ultimate_order_requires_learned_ready_spell_and_keeps_macro_plan(self):
        service = self.coach(mode="observe")
        self.snapshot(service)
        command = self.command(
            service, action="hold_ultimate", target={"kind": "ability", "name": "lina_laguna_blade"},
            bot_ids=[1], ttl_s=6,
        )
        ability = {"name": "lina_laguna_blade", "level": 2, "cooldown_s": 0, "active": False}

        with self.assertRaises(ValueError):
            service.submit_command(command)
        self.bot_update(service, hero="npc_dota_hero_lina", ultimates=[])
        with self.assertRaises(ValueError):
            service.submit_command(command)
        for fields in ({"cooldown_s": 26}, {"active": True}):
            with self.subTest(fields=fields):
                self.bot_update(service, hero="npc_dota_hero_lina", ultimates=[{**ability, **fields}])
                with self.assertRaises(ValueError):
                    service.submit_command(command)

        self.bot_update(service, hero="npc_dota_hero_lina", ultimates=[ability])
        service.submit_command(self.command(service))
        plan = service.status()["plan"]
        accepted = service.submit_command(command)
        self.assertEqual(accepted["action"], "hold_ultimate")
        self.assertEqual(service.status()["plan"], plan)
        self.assertNotIn("commands", service.exchange(report(bot_id=2)))
        reply = self.bot_update(service, hero="npc_dota_hero_lina", ultimates=[ability])
        self.assertEqual(reply["commands"], [accepted])
        self.assertEqual(reply["plan"], plan)
        cancel = {"session_id": command["session_id"], "command_id": accepted["id"]}
        with self.assertRaises(ValueError):
            service.cancel_command({**cancel, "session_id": "old-match"})
        self.assertEqual(service.cancel_command(cancel), {"removed": True})
        self.assertEqual(service.cancel_command(cancel), {"removed": False})
        reply = self.bot_update(service, hero="npc_dota_hero_lina", ultimates=[ability])
        self.assertNotIn("commands", reply)
        self.assertEqual(reply["plan"], plan)

    def fight_sample(self, service, clock_s, *, enemy=None, bots=(0, 1), ready=None):
        for bot_id in bots:
            packet = report(clock_s, bot_id=bot_id, threat=None)
            packet["bot"]["fight"] = {
                "x": -850, "y": 80 * bot_id,
                "enemies": [enemy] if enemy is not None else [],
            }
            if ready:
                packet["fight_ready"] = {"id": ready, "phase": "stage"}
            service.exchange(packet)
        return service.status()

    def test_bait_waits_for_both_bots_then_reenters_after_an_observed_bkb(self):
        journal = []
        service = self.coach(mode="observe", journal=journal.append)
        target = {"id": 5, "x": 0, "y": 0, "hp": 0.84, "bkb": False, "bkb_ready": True}
        status = self.fight_sample(service, 950, enemy=target)
        accepted = service.submit_command(self.command(
            service, action="bait", target={"kind": "enemy", "id": 5}, bot_ids=[0, 1], ttl_s=26,
        ))
        fight_id = accepted["id"]
        self.assertEqual(status["fight_choices"][0]["tactic"], "bait")
        self.assertEqual(accepted["phase"], "stage")

        only_one = self.fight_sample(service, 952, enemy=target, bots=(0,), ready=fight_id)
        self.assertEqual(only_one["fight"]["phase"], "stage")
        gathered = self.fight_sample(service, 952, enemy=target, ready=fight_id)
        self.assertEqual(gathered["fight"]["phase"], "probe")

        active_bkb = {**target, "bkb": True, "bkb_ready": False}
        self.assertEqual(self.fight_sample(service, 954, enemy=active_bkb)["fight"]["phase"], "reset")
        self.assertEqual(self.fight_sample(service, 956, enemy=active_bkb)["fight"]["phase"], "reset")
        reentry = self.fight_sample(service, 958, enemy={**target, "bkb_ready": False})
        self.assertEqual(reentry["fight"]["phase"], "commit")
        self.assertIsNone(self.fight_sample(service, 960)["fight"])
        self.assertEqual(
            [event.get("evidence") for event in journal if event["event"] == "fight_phase"],
            ["allies_in_position", "enemy_defensive_resource_active", "bkb_ended"],
        )
        self.assertEqual(journal[-1]["reason"], "target_not_visible")

    def test_fight_counter_window_needs_consecutive_visible_cooldown_samples(self):
        service = self.coach(mode="observe")
        target = {"id": 5, "x": 0, "y": 0, "hp": 0.84, "bkb": False}
        def sight(cooldown):
            return {**target, "ultimate_cooldowns": [{"name": "tidehunter_ravage", "cooldown_s": cooldown}]}

        self.fight_sample(service, 950, enemy=sight(0))
        self.fight_sample(service, 952)
        lost_vision = self.fight_sample(service, 953, enemy=sight(100))
        self.assertNotIn("counterattack", [item["tactic"] for item in lost_vision["fight_choices"]])
        self.fight_sample(service, 957, enemy=sight(0))
        visible = self.fight_sample(service, 959, enemy=sight(120))
        self.assertEqual(visible["fight_choices"][0]["tactic"], "counterattack")
        self.assertEqual(visible["fight_choices"][0]["signal"], "tidehunter_ravage")

    def test_only_automatic_modes_start_fights_without_a_manual_request(self):
        for mode in ("shadow", "observe", "rules"):
            with self.subTest(mode=mode):
                service = self.coach(mode=mode)
                target = {"id": 5, "x": 0, "y": 0, "hp": 0.50, "bkb": False}
                self.fight_sample(service, 950, enemy=target)
                service.observe()
                status = service.status()
                self.assertEqual(status["fight_choices"][0]["tactic"], "pickoff")
                self.assertEqual(bool(status["fight"]), mode == "rules")

    def test_opponent_ganks_snowballing_mid_in_observe_mode_after_three_ready_reports(self):
        service = self.coach(mode="observe", opponent_pressure=True)
        target = {"id": 5, "x": 0, "y": 0, "hp": 0.95, "level": 10, "role": 2,
                  "kills": 6, "deaths": 1, "under_tower": False}

        def send(clock, positions, *, ready_ids=(), plan_id=None, target_update=None):
            sighting = {**target, **(target_update or {})}
            for bot_id, x in enumerate(positions):
                packet = report(clock, bot_id=bot_id, threat=None)
                packet["bot"]["fight"] = {"x": x, "y": 0, "enemies": [sighting]}
                if bot_id in ready_ids:
                    packet["fight_ready"] = {"id": plan_id, "phase": "stage"}
                reply = service.exchange(packet)
            return reply

        send(480, [700, 1900])
        service.observe()
        self.assertIsNone(service.status()["fight"])
        send(482, [700, 1900, 2250])
        service.observe()
        plan = service.status()["fight"]
        self.assertEqual((plan["tactic"], plan["phase"], plan["bot_ids"]), ("gank", "stage", [0, 1, 2]))
        self.assertTrue(plan["priority_target"])
        send(484, [700, 900, 1500], ready_ids=(0, 1), plan_id=plan["id"])
        self.assertEqual(service.status()["fight"]["phase"], "stage")
        reply = send(486, [700, 900, 900], ready_ids=(0, 1, 2), plan_id=plan["id"])
        self.assertEqual(reply["fight"]["phase"], "commit")
        reply = send(488, [700, 900, 900], target_update={"under_tower": True})
        self.assertNotIn("fight", reply)
        send(498, [700, 900, 900])
        service.observe()
        self.assertIsNone(service.status()["fight"])

    def test_only_opponent_pressure_rotates_onto_healthy_core_at_full_macro(self):
        target = {"id": 5, "x": 0, "y": 0, "hp": 1, "level": 7, "role": 1, "under_tower": False}
        for pressure, macro, role, expected in (
            (True, 100, 1, "gank"), (True, 0, 1, None), (False, 100, 1, None), (True, 100, 4, None),
        ):
            with self.subTest(pressure=pressure, macro=macro, role=role):
                service = self.coach(mode="observe", opponent_pressure=pressure)
                for bot_id, (x, bot_role) in enumerate(((650, 2), (1900, 4), (850, 1))):
                    packet = report(360, bot_id=bot_id, threat=None, tuning={"macro": macro})
                    packet["bot"].update(role=bot_role, level=6, fight={
                        "x": x, "y": 0, "enemies": [{**target, "role": role}],
                    })
                    service.exchange(packet)
                service.observe()
                plan = service.status()["fight"]
                self.assertEqual(plan and plan["tactic"], expected)
                if plan:
                    self.assertEqual(plan["bot_ids"], [0, 1])

    def test_opponent_rebuilds_a_failed_gank_with_a_fresh_support(self):
        journal = []
        service = self.coach(mode="observe", opponent_pressure=True, journal=journal.append)
        target = {"id": 5, "x": 0, "y": 0, "hp": 0.95, "level": 10, "role": 2,
                  "kills": 6, "deaths": 1, "under_tower": False}

        def packet(clock, bot_id, x, **changes):
            sample = report(clock, bot_id=min(bot_id, 2), threat=None)
            sample["bot"].update(id=bot_id, fight={"x": x, "y": 0, "enemies": [target]}, **changes)
            return sample

        for bot_id, position in enumerate((700, 1050, 1300, 1400)):
            service.exchange(packet(480, bot_id, position))
        initial = service.status()["fight"]
        self.assertEqual(initial["bot_ids"], [0, 1, 2])

        service.exchange(packet(481, 1, 1050, hp=0.58, mode="retreat"))
        rebuilt = service.status()["fight"]
        self.assertEqual(rebuilt["target_id"], 5)
        self.assertEqual(rebuilt["bot_ids"], [0, 2, 3])
        self.assertNotEqual(rebuilt["id"], initial["id"])
        self.assertEqual(rebuilt["phase"], "stage")
        self.assertTrue(any(entry["event"] == "pressure_replanned" and entry["previous_id"] == initial["id"]
                            and entry["replacement"]["id"] == rebuilt["id"] for entry in journal))

    def test_opponent_trades_another_lane_when_first_defense_stalls(self):
        journal = []
        service = self.coach(mode="observe", opponent_pressure=True, journal=journal.append)

        def frame(clock, *, push_ready=False):
            for bot_id, x in enumerate((900, 4800, 5000)):
                sample = report(clock, bot_id=bot_id, threat="mid")
                sample["bot"].update(zone="mid" if bot_id == 0 else "top", tp_ready=False,
                                     fight={"x": x, "y": 0, "enemies": []},
                                     desires={"defend_mid": 0, "push_top": 0.36})
                if "map" in sample:
                    sample["map"]["lanes"]["mid"].update(own_tower_pos={"x": 0, "y": 0})
                    if push_ready:
                        sample["map"]["lanes"]["top"].update(
                            enemy_tower=1, enemy_tower_pos={"x": 5400, "y": 0}, enemy_tower_hp=0.25,
                            ally_near_enemy=2, ally_wave_near_enemy=5,
                        )
                service.exchange(sample)

        frame(600)
        initial = service.status()["plan"]
        self.assertEqual((initial["objective"], initial["lane"], initial["bot_ids"]), ("defend", "mid", [0]))
        frame(602, push_ready=True)
        frame(604, push_ready=True)
        frame(606, push_ready=True)
        followup = service.status()["plan"]
        self.assertEqual((followup["objective"], followup["lane"]), ("push", "top"))
        self.assertTrue(any(entry["event"] == "pressure_replanned" and entry["previous_id"] == initial["id"]
                            and entry["cause"] == "no_bot_response" for entry in journal))

    def playbook_frame(self, service, clock, *, opener=True, ready=None, bkb=False, down=False):
        target = {"id": 5, "x": 0, "y": 0, "hp": 0.55, "level": 12, "role": 2, "under_tower": False, "bkb": bkb}
        for bot_id in (0, 1, 2):
            packet = report(clock, bot_id=bot_id, threat=None)
            packet["bot"]["fight"] = {"x": -820, "y": bot_id * 80, "enemies": [] if down else [target]}
            if bot_id == 0:
                packet["map"]["lanes"]["top"].update(ally_near_enemy=2, ally_wave_near_enemy=5)
                packet["map"]["enemies"][0]["alive"] = not down
                packet["bot"]["tactical_openers"] = ["axe_berserkers_call"] if opener else []
                if bkb:
                    packet["bot"].update(under_fire=True, nearby_enemies=1, desires={"retreat": 0.5})
            if ready and bot_id in (0, 1):
                packet["fight_ready"] = {"id": ready, "phase": "stage"}
            service.exchange(packet)

    def playbook_request(self, service):
        def step(action, target, bots, ttl, opener=None):
            return {"action": action, "target": target, "bot_ids": bots, "ttl_s": ttl,
                    "reason": "Execute this group decision", "opener": opener}

        return {"session_id": service.session_id, "clock_s": service.game_clock, "playbook": {
            "window_s": 45, "chains": [
                {"id": "catch", "wait_s": 4, "start": step("pickoff", {"kind": "enemy", "id": 5}, [0, 1], 18,
                  {"bot_id": 0, "ability": "axe_berserkers_call"}), "on_success": None,
                 "on_failure": step("retreat", {"kind": "self"}, [0], 6)},
                {"id": "trade", "wait_s": 4, "start": step("push", {"kind": "lane", "lane": "top"}, [2], 25),
                 "on_success": None, "on_failure": None},
            ],
        }}

    def test_playbook_runs_fight_and_lane_in_parallel_and_reacts_on_next_packet(self):
        journal = []
        service = self.coach(mode="observe", journal=journal.append)
        self.playbook_frame(service, 950)
        result = service.submit_playbook(self.playbook_request(service))
        catch, trade = result["chains"]
        self.assertEqual((catch["status"], trade["status"]), ("running", "running"))
        fight_id, plan_id = catch["order_id"], trade["order_id"]
        fight = service.status()["fight"]
        self.assertEqual(fight["opener"], {"bot_id": 0, "ability": "axe_berserkers_call"})
        self.assertEqual(fight["phase"], "stage")
        self.playbook_frame(service, 952, ready=fight_id)
        self.assertEqual(service.status()["fight"]["phase"], "commit")

        packet = report(953, bot_id=0, threat=None)
        packet["bot"].update(hp=0.5, under_fire=True, nearby_enemies=1, desires={"retreat": 0.5})
        packet["map"]["lanes"]["top"].update(ally_near_enemy=2, ally_wave_near_enemy=5)
        packet["applied_opener"] = {"id": fight_id, "ability": "axe_berserkers_call"}
        reply = service.exchange(packet)
        self.assertEqual(reply["plan"]["id"], plan_id)
        self.assertNotIn("fight", reply)
        self.assertEqual(reply["commands"][0]["action"], "retreat")
        self.assertEqual(service.status()["playbook"][0]["branch"], "on_failure")
        self.assertTrue(any(row["event"] == "opener_applied" and row["target_id"] == 5 for row in journal))

    def test_playbook_waits_for_the_named_spell_and_switches_to_success_branch(self):
        service = self.coach(mode="observe")
        self.playbook_frame(service, 950, opener=False)
        request = self.playbook_request(service)
        catch, _ = request["playbook"]["chains"]
        catch["on_success"] = {"action": "push", "target": {"kind": "lane", "lane": "top"},
                               "bot_ids": [0], "ttl_s": 20, "reason": "Take the tower after the kill", "opener": None}
        request["playbook"]["chains"] = [catch]
        self.assertEqual(service.submit_playbook(request)["chains"][0]["status"], "waiting")
        self.playbook_frame(service, 952)
        fight_id = service.status()["fight"]["id"]
        self.playbook_frame(service, 954, ready=fight_id)
        self.assertEqual(service.status()["fight"]["phase"], "commit")
        self.playbook_frame(service, 956, down=True)
        status = service.status()
        self.assertEqual(status["playbook"][0]["branch"], "on_success")
        self.assertEqual(status["plan"]["lane"], "top")

    def test_playbook_revision_preserves_running_step_and_rejects_shared_groups(self):
        service = self.coach(mode="observe")
        self.playbook_frame(service, 950)
        request = self.playbook_request(service)
        result = service.submit_playbook(request)
        fight_id = result["chains"][0]["order_id"]
        request["playbook"]["chains"][0]["on_failure"]["reason"] = "Regroup after contact"
        self.assertEqual(service.submit_playbook(request)["chains"][0]["order_id"], fight_id)
        request["playbook"]["chains"][1]["start"]["bot_ids"] = [0, 2]
        with self.assertRaisesRegex(ValueError, "distinct bots"):
            service.submit_playbook(request)
        self.assertEqual(service.status()["fight"]["id"], fight_id)

    def test_playbook_reacts_to_bkb_on_the_current_snapshot(self):
        journal = []
        service = self.coach(mode="observe", journal=journal.append)
        self.playbook_frame(service, 950)
        service.submit_playbook(self.playbook_request(service))
        fight_id = service.status()["fight"]["id"]
        self.playbook_frame(service, 952, ready=fight_id)
        self.playbook_frame(service, 953, bkb=True)
        status = service.status()
        self.assertIsNone(status["fight"])
        self.assertEqual(status["plan"]["objective"], "push")
        self.assertEqual(status["playbook"][0]["branch"], "on_failure")
        self.assertTrue(any(row["event"] == "fight_ended" and row["reason"] == "enemy_defense_activated"
                            and row["clock_s"] == 953 for row in journal))

    def test_match_tuning_defaults_to_max_and_scales_each_runtime_control(self):
        service = self.coach(mode="rules")
        self.snapshot(service)
        maximum = service.status()
        self.assertEqual(maximum["runtime_revision"], 4)
        self.assertEqual(maximum["tuning"], {"bot_difficulty": 4, "micro": 100, "macro": 100, "coach": 100})
        self.assertEqual((maximum["decision_interval_s"], maximum["max_calls"], maximum["model_cooldown_s"]), (2, 4096, 15))
        self.snapshot(service, tuning={"macro": 50, "coach": 50})
        half = service.status()
        self.assertEqual((half["decision_interval_s"], half["max_calls"], half["model_cooldown_s"]), (11, 2048, 30))
        self.assertEqual(half["context"]["tuning"]["micro"], 100)
        for invalid in ({"macro": True}, {"macro": 101}, {"coach": -1}, {"bot_difficulty": 5}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.snapshot(service, tuning=invalid)

    def test_macro_zero_ends_automatic_plans_and_preserves_manual_orders(self):
        service = self.coach(mode="rules")
        self.snapshot(service)
        self.assertEqual(service.review()["source"], "local")
        self.snapshot(service, tuning={"macro": 0})
        self.assertIsNone(service.status()["plan"])
        self.assertIsNone(service.review())
        manual = service.submit_command(self.command(service))
        self.assertEqual(manual["source"], "manual")
        self.assertIsNone(service.review())
        self.assertEqual(service.status()["plan"]["source"], "manual")

        service.cancel_command({"session_id": service.session_id, "command_id": manual["id"]})
        target = {"id": 5, "x": 0, "y": 0, "hp": 0.50, "bkb": False}
        self.fight_sample(service, 952, enemy=target)
        disabled = report(952, threat=None, tuning={"macro": 0})
        disabled["bot"]["fight"] = {"x": -850, "y": 0, "enemies": [target]}
        service.exchange(disabled)
        service.observe()
        self.assertIsNone(service.status()["fight"])
        order = service.submit_command(self.command(
            service, clock_s=952, action="pickoff", target={"kind": "enemy", "id": 5},
            bot_ids=[0, 1], ttl_s=18,
        ))
        self.assertEqual(order["source"], "manual")
        self.assertEqual(service.status()["fight"]["id"], order["id"])

    def test_coach_zero_skips_consultation_and_discards_an_in_flight_reply(self):
        service = self.coach()
        self.snapshot(service, tuning={"coach": 0})
        self.assertIsNone(service.step())
        self.assertEqual(service.status()["calls"], 0)
        entered, release = Event(), Event()
        def slow(context):
            entered.set()
            release.wait(2)
            return defend(context)
        service.coach = slow
        self.snapshot(service)
        worker = Thread(target=service.step)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.snapshot(service, tuning={"coach": 0})
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(service.status()["strategy_hint"])
        self.assertFalse(service.status()["pending"])

    def test_a_26_second_strategy_reply_is_revalidated_against_current_tower(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                service = self.coach()
                def delayed(context):
                    self.clock.advance(26)
                    self.snapshot(service, 976, mid_tier=3 if changed else 2)
                    return defend(context)
                service.coach = delayed
                self.snapshot(service)
                self.assertEqual(service.step() is not None, not changed)
                self.assertEqual(service.status()["strategy_hint"] is not None, not changed)

    def test_successful_pickoff_prioritizes_a_two_hero_tower_push(self):
        service = self.coach(mode="rules")
        self.snapshot(service, threat=None)
        target = {"id": 5, "x": 0, "y": 0, "hp": 0.50, "bkb": False}
        self.fight_sample(service, 950, enemy=target)
        service.observe()
        fight = service.status()["fight"]
        finish = report(952, threat=None, push_window=True, enemies_alive=False)
        finish["bot"]["mode"] = "attack"
        finish["applied_fight"] = {"id": fight["id"], "phase": "commit"}
        service.exchange(finish)
        self.assertTrue(service.status()["context"]["objective_window"])
        self.assertIsNone(service.status()["fight"])
        service.observe()
        self.assertIsNone(service.status()["fight"])
        plan = service.review()
        self.assertEqual(plan["objective"], "push")
        self.assertEqual(len(plan["bot_ids"]), 2)

    def test_nearby_pickoff_commits_and_yields_to_ancient_defense(self):
        service = self.coach(mode="rules")
        target = {"id": 5, "x": 0, "y": 0, "hp": 0.50, "bkb": False}
        self.fight_sample(service, 950, enemy=target)
        service.observe()
        self.assertEqual(service.status()["fight"]["phase"], "commit")

        defense = report(952, threat=None, mid_tier=0, push_window=True)
        defense["map"]["ancient_threat"] = True
        defense["bot"]["fight"] = {"x": -850, "y": 0, "enemies": [target]}
        reply = service.exchange(defense)
        self.assertNotIn("fight", reply)
        status = service.status()
        self.assertEqual(status["fight_choices"], [])
        self.assertEqual(status["context"]["choices"], [])

    def test_ultimate_hold_uses_the_actual_report_for_any_hero(self):
        service = self.coach(mode="observe")
        self.snapshot(service)
        command = self.command(
            service, action="hold_ultimate", target={"kind": "ability", "name": "tidehunter_ravage"},
            bot_ids=[1], ttl_s=6,
        )
        self.bot_update(service, hero="npc_dota_hero_tidehunter", ultimates=[])
        with self.assertRaises(ValueError):
            service.submit_command(command)
        self.bot_update(service, hero="npc_dota_hero_tidehunter", ultimates=[
            {"name": "tidehunter_ravage", "level": 2, "cooldown_s": 0, "active": False},
        ])
        self.assertEqual(service.submit_command(command)["target"], command["target"])

    def test_shadow_mode_rejects_valid_typed_command_over_http(self):
        service = self.coach(mode="shadow")
        self.snapshot(service)
        with make_server(service, port=0) as server:
            worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            worker.start()
            url = f"http://127.0.0.1:{server.server_port}/commands"
            try:
                with urlopen(url, timeout=2) as response:
                    catalog = json.load(response)
                self.assertIn("catalog", catalog)
                self.assertEqual(catalog["active"], [])

                request = Request(
                    url, data=json.dumps(self.command(service)).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=2)
                self.assertEqual(caught.exception.code, 400)
                caught.exception.close()
                self.assertIsNone(service.status()["plan"])
            finally:
                server.shutdown()
                worker.join(timeout=2)

    def test_http_round_trip_limits_exchange_to_selected_team(self):
        service = self.coach()
        with make_server(service, port=0) as server:
            self.assertEqual(server.server_address[0], "127.0.0.1")
            worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            worker.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def exchange(data):
                return Request(
                    f"{base}/exchange",
                    data=data,
                    headers={"Content-Type": "application/json"},
                )

            try:
                with urlopen(f"{base}/health", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                for invalid in (b"{", json.dumps(report(team="dire")).encode()):
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(exchange(invalid), timeout=2)
                    self.assertEqual(caught.exception.code, 400)
                    caught.exception.close()

                for bot_id in (2, 1, 0):
                    with urlopen(exchange(json.dumps(report(bot_id=bot_id)).encode()), timeout=2) as response:
                        initial = json.load(response)
                plan = service.review()
                self.assertIsNotNone(plan)
                with urlopen(exchange(json.dumps(report(951)).encode()), timeout=2) as response:
                    self.assertEqual(json.load(response), {"session_id": initial["session_id"], "plan": plan})
                with urlopen(f"{base}/status", timeout=2) as response:
                    status = json.load(response)
                self.assertEqual((status["phase"], status["reviews"], status["calls"]), ("active", 1, 0))

                command = self.command(service, bot_ids=[1], clock_s=951)
                encoded = json.dumps(command)
                duplicates = (
                    encoded.replace('"ttl_s": 30', '"ttl_s": 45, "ttl_s": 30'),
                    encoded.replace('"lane": "mid"', '"lane": "top", "lane": "mid"'),
                )
                for data in duplicates:
                    duplicate_request = Request(
                        f"{base}/commands", data=data.encode(), headers={"Content-Type": "application/json"},
                    )
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(duplicate_request, timeout=2)
                    self.assertEqual(caught.exception.code, 400)
                    caught.exception.close()
                typed_request = Request(
                    f"{base}/commands", data=encoded.encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(typed_request, timeout=2) as response:
                    typed = json.load(response)
                self.assertEqual((typed["action"], typed["bot_ids"]), ("defend", [1]))
                self.assertEqual(service.status()["plan"]["bot_ids"], [1])
                cancel_request = Request(
                    f"{base}/commands",
                    data=json.dumps({"session_id": command["session_id"], "command_id": typed["id"]}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="DELETE",
                )
                with urlopen(cancel_request, timeout=2) as response:
                    self.assertEqual(json.load(response), {"removed": True})
                self.assertIsNone(service.status()["plan"])
            finally:
                server.shutdown()
                worker.join(timeout=2)
