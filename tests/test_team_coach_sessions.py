import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from team_coach.__main__ import _watch
from team_coach.service import CoachService
from team_coach.session import CoachSession, SessionError, _isolated_connections
from team_coach.strategist import ModelCoach, decision_schema, validate_decision
from test_team_coach import MatchClock, report


def batch(*orders, cancel_ids=()):
    return {"orders": list(orders), "cancel_ids": list(cancel_ids), "reason": "Coordinate the team."}


def order(action="defend", *, ids=(1, 2), target=None, ttl=30):
    return {"action": action, "target": target or {"kind": "lane", "lane": "mid"},
            "bot_ids": list(ids), "ttl_s": ttl, "reason": "Act on the current opportunity."}


class PrimaryCoach:
    primary = True

    def __init__(self, decision=None):
        self.decision = batch() if decision is None else decision
        self.contexts = []

    def __call__(self, context):
        self.contexts.append(context)
        return self.decision, {"input_tokens": 100, "output_tokens": 40}


class SessionServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = MatchClock()
        self.coach = PrimaryCoach()
        self.service = CoachService(coach=self.coach, clock=self.clock)

    def snapshot(self, clock_s=950, *, service=None, **fields):
        service = service or self.service
        for bot_id in (2, 1, 0):
            packet = report(clock_s, bot_id=bot_id, **fields)
            packet["bot"].update(desires={"farm": 0.5, "retreat": 0.6, "defend_mid": 0.5},
                                 nearby_allies=2, nearby_enemies=0, under_fire=False)
            service.exchange(packet)

    def test_periodic_consultation_even_when_map_is_unchanged_and_not_while_paused(self):
        self.snapshot()
        self.service.step()
        self.clock.advance(20)
        self.snapshot()
        self.assertIsNone(self.service.step())
        self.snapshot(964)
        self.assertIsNone(self.service.step())
        self.snapshot(965)
        self.service.step()
        self.assertEqual(len(self.coach.contexts), 2)
        self.assertEqual(self.coach.contexts[-1]["trigger"], ["interval"])

    def test_significant_event_bypasses_fifteen_second_interval(self):
        self.snapshot()
        self.service.step()
        self.clock.advance(3)
        self.snapshot(953, enemies_alive=False)
        self.service.step()
        self.assertEqual(len(self.coach.contexts), 2)
        self.assertIn("enemies_down", self.coach.contexts[-1]["trigger"])

    def test_no_lane_choices_still_consults_for_tactical_orders(self):
        self.snapshot(threat=None)
        self.service.step()
        self.assertEqual(len(self.coach.contexts), 1)
        self.assertEqual(self.coach.contexts[0]["choices"], [])
        self.assertIn("farm", [entry["action"] for entry in self.coach.contexts[0]["available_commands"]])

    def test_session_decision_is_applied_directly_and_rules_cannot_replace_it(self):
        self.coach.decision = batch(order(ids=(2,)))
        self.snapshot()
        self.assertIsNone(self.service.review())
        result = self.service.step()
        self.assertEqual(len(result["accepted"]), 1)
        plan = self.service.status()["plan"]
        self.assertEqual((plan["source"], plan["bot_ids"]), ("model", [2]))
        self.snapshot(953, threat="top")
        self.service.review()
        self.assertIsNone(self.service.status()["plan"])
        self.assertEqual(self.service.status()["reviews"], 0)

    def test_short_retreat_and_ultimate_hold_use_full_command_contract(self):
        self.snapshot()
        packet = report(950, bot_id=1)
        packet["bot"].update(desires={"retreat": 0.6}, under_fire=True,
                             ultimates=[{"name": "lion_finger_of_death", "level": 1,
                                         "cooldown_s": 0, "active": False}])
        self.service.exchange(packet)
        self.coach.decision = batch(
            order("retreat", ids=(1,), target={"kind": "self"}, ttl=8),
            order("hold_ultimate", ids=(1,), target={"kind": "ability", "name": "lion_finger_of_death"}, ttl=6),
        )
        result = self.service.step()
        self.assertEqual(result["rejected"], [])
        self.assertEqual({entry["action"] for entry in result["accepted"]}, {"retreat", "hold_ultimate"})
        self.assertTrue(all(entry["source"] == "model" for entry in result["accepted"]))

    def test_session_chooses_fight_and_bkb_event_triggers_an_early_consultation(self):
        def sample(clock_s, bkb):
            for bot_id in (0, 1):
                packet = report(clock_s, bot_id=bot_id, threat=None)
                packet["bot"]["fight"] = {"x": -850, "y": 80 * bot_id,
                    "enemies": [{"id": 5, "x": 0, "y": 0, "hp": 0.5, "bkb": bkb}]}
                self.service.exchange(packet)

        sample(950, False)
        self.service.observe()
        self.assertIsNone(self.service.status()["fight"])
        self.coach.decision = batch(order("pickoff", ids=(0, 1), target={"kind": "enemy", "id": 5}, ttl=18))
        result = self.service.step()
        self.assertEqual(result["rejected"], [])
        self.assertEqual(self.service.status()["fight"]["source"], "model")
        self.clock.advance(3)
        sample(953, True)
        self.coach.decision = batch()
        self.service.step()
        self.assertEqual(len(self.coach.contexts), 2)
        self.assertIn("enemy_resources", self.coach.contexts[-1]["trigger"])

    def test_own_commands_can_be_cancelled_and_human_commands_are_protected(self):
        self.snapshot()
        self.coach.decision = batch(order())
        self.service.step()
        own = self.service.status()["plan"]["id"]
        self.coach.decision = batch(cancel_ids=(own,))
        self.snapshot(965)
        self.service.step()
        self.assertIsNone(self.service.status()["plan"])
        human = self.service.submit_command({"session_id": self.service.session_id, "clock_s": 965, **order()})
        self.coach.decision = batch(order(), cancel_ids=(human["id"],))
        self.snapshot(980)
        result = self.service.step()
        self.assertEqual(len(result["rejected"]), 2)
        self.assertEqual(self.service.status()["plan"]["id"], human["id"])

    def test_enemy_ids_and_invalidated_towers_are_rejected(self):
        self.snapshot()
        self.coach.decision = batch(order(ids=(5,)))
        result = self.service.step()
        self.assertEqual(result["accepted"], [])
        self.assertEqual(len(result["rejected"]), 1)

    def test_event_arriving_while_consulting_is_not_lost(self):
        entered, release = Event(), Event()
        original = self.coach.__class__.__call__

        def slow(coach, context):
            entered.set()
            release.wait(3)
            return original(coach, context)

        self.snapshot()
        with patch.object(PrimaryCoach, "__call__", slow):
            worker = Thread(target=self.service.step)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.clock.advance(3)
                self.snapshot(953, enemies_alive=False)
                self.assertIsNone(self.service.step())
            finally:
                release.set()
                worker.join(2)
        self.service.step()
        self.assertEqual(len(self.coach.contexts), 2)

    def test_previous_match_reply_is_discarded(self):
        class RewindingCoach(PrimaryCoach):
            def __call__(coach, context):
                self.snapshot(10)
                return batch(order()), {}

        self.service.coach = RewindingCoach()
        self.snapshot()
        self.assertIsNone(self.service.step())
        self.assertIsNone(self.service.status()["plan"])

    def test_short_tactical_order_expires_while_model_thinks(self):
        class SlowRetreat(PrimaryCoach):
            def __call__(coach, context):
                self.snapshot(956)
                return batch(order("retreat", ids=(1,), target={"kind": "self"}, ttl=3)), {}

        self.service.coach = SlowRetreat()
        self.snapshot()
        result = self.service.step()
        self.assertIn("expired", result["rejected"][0]["error"])

    def test_snapshot_has_only_own_team_and_allied_calls(self):
        self.service.accept_player_calls = True
        self.snapshot()
        self.service.step()
        rival = PrimaryCoach()
        dire = CoachService(team="dire", coach=rival, clock=self.clock)
        self.snapshot(service=dire, team="dire")
        dire.step()
        self.assertIn("player_call", self.coach.contexts[0])
        self.assertNotIn("player_call", rival.contexts[0])
        self.assertNotEqual(self.coach.contexts[0]["session_id"], rival.contexts[0]["session_id"])
        self.assertEqual(rival.contexts[0]["team"], "dire")

    def test_both_sessions_consult_concurrently_in_the_real_watch_loop(self):
        entered = [Event(), Event()]
        release, stop_server = Event(), Event()

        class WaitingService:
            mode, model_primary = "model", True

            def __init__(self, index):
                self.index = index

            def observe(self):
                pass

            def step(self):
                entered[self.index].set()
                release.wait(3)

        class Server:
            def serve_forever(self, **_):
                stop_server.wait(4)
                raise KeyboardInterrupt

            def shutdown(self):
                stop_server.set()

        # The secondary HTTP server should end normally rather than raise in a background thread.
        class OtherServer(Server):
            def serve_forever(self, **_):
                stop_server.wait(4)

        worker = Thread(target=_watch, args=(
            {"radiant": WaitingService(0), "dire": WaitingService(1)},
            {"radiant": Server(), "dire": OtherServer()}, "radiant", None, {},
            SimpleNamespace(max_calls=4096, model_timeout=1),
        ))
        worker.start()
        try:
            self.assertTrue(entered[0].wait(2))
            self.assertTrue(entered[1].wait(2))
        finally:
            release.set()
            stop_server.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_observation_worker_recovers_after_an_exception(self):
        recovered, finish = Event(), Event()
        attempts, journal = [], []
        service = CoachService(mode="observe", journal=journal.append)

        def observe():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("Transient telemetry error")
            recovered.set()

        class Server:
            def serve_forever(self, **_):
                finish.wait(4)
                raise KeyboardInterrupt

        service.observe = observe
        worker = Thread(target=_watch, args=(
            {"radiant": service}, {"radiant": Server()}, "radiant", None, {},
            SimpleNamespace(max_calls=0, model_timeout=1),
        ))
        worker.start()
        try:
            self.assertTrue(recovered.wait(3))
            self.assertTrue(service.status()["worker_health"]["telemetry"]["healthy"])
            self.assertEqual(journal[0]["event"], "worker_error")
        finally:
            finish.set()
            worker.join(2)


_PEER = '''
import json, sys, time
counter = 0
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    method, params = request.get("method"), request.get("params", {})
    if "id" not in request:
        continue
    result = {}
    if method == "thread/start":
        counter += 1
        result = {"thread": {"id": "team-session-" + str(counter)}}
    if method == "turn/start":
        thread = params["threadId"]
        turn = "turn-" + str(request["id"])
        if params["input"][0]["text"] == "timeout":
            time.sleep(5)
        result = {"turn": {"id": turn}}
        # Deliver an event before the RPC response to exercise notification buffering.
        send({"method": "item/completed", "params": {"threadId": thread, "turnId": turn,
              "item": {"type": "agentMessage", "text": json.dumps({"orders": [], "cancel_ids": [], "reason": "Keep the plan."})}}})
        send({"id": request["id"], "result": result})
        send({"method": "thread/tokenUsage/updated", "params": {"threadId": thread, "turnId": turn,
              "tokenUsage": {"last": {"inputTokens": 12, "outputTokens": 8}, "total": {"inputTokens": 999}}}})
        send({"method": "turn/completed", "params": {"threadId": thread,
              "turn": {"id": turn, "status": "completed", "items": []}}})
        continue
    send({"id": request["id"], "result": result})
'''


class SessionTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        peer = directory / "session_peer.py"
        peer.write_text(_PEER, encoding="utf-8")
        popen = subprocess.Popen
        self.launches = []

        def launch(command, **kwargs):
            self.launches.append(command)
            return popen([sys.executable, str(peer)], **kwargs)

        self.patcher = patch("team_coach.session.Popen", side_effect=launch)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.session = CoachSession(directory, model="test-model", effort="high", instructions="Coach radiant.",
                                    executable=sys.executable, timeout=2)
        self.addCleanup(self.session.close)

    def test_reuses_process_and_thread_and_counts_per_turn_usage(self):
        prepared = self.session.prepare()
        first = self.session.consult("first", decision_schema(), match_id="match-one")
        first_id = self.session.thread_id
        second = self.session.consult("second", decision_schema(), match_id="match-one")
        self.assertEqual(prepared["id"], first_id)
        self.assertEqual(self.session.thread_id, first_id)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(first, second)
        self.assertEqual(second[1], {"input_tokens": 12, "output_tokens": 8})
        self.assertEqual(json.loads((Path(self.temporary.name) / "session.json").read_text())["match_id"], "match-one")

    def test_game_session_disables_unrelated_startup_hooks_and_external_connections(self):
        settings = Path(self.temporary.name) / "settings.toml"
        settings.write_text('[mcp_servers.external_data]\ncommand = "private-command"\n', encoding="utf-8")
        overrides = _isolated_connections(settings)
        self.assertEqual(overrides, ["mcp_servers.external_data.enabled=false"])
        with patch("team_coach.session._isolated_connections", return_value=overrides):
            self.session.prepare()
        command = self.launches[0]
        self.assertIn("mcp_servers.external_data.enabled=false", command)
        self.assertNotIn("private-command", command)
        self.assertTrue(any(command[index:index + 2] == ["--disable", "hooks"]
                            for index in range(len(command) - 1)))

    def test_new_match_gets_fresh_history(self):
        self.session.consult("first", decision_schema(), match_id="match-one")
        previous = self.session.thread_id
        self.session.consult("second", decision_schema(), match_id="match-two")
        self.assertNotEqual(previous, self.session.thread_id)
        self.assertEqual(len(self.launches), 1)

    def test_empty_thread_is_not_a_ready_coach_until_model_acknowledges_startup(self):
        coach = ModelCoach(team="radiant")
        coach.session = self.session
        self.session.prepare()
        self.assertFalse(coach.status()["ready"])
        status = coach.prepare()
        self.assertTrue(status["ready"])
        self.assertTrue(status["initialized"])
        self.assertIsNotNone(status["turn_id"])
        self.assertEqual(status["state"], "waiting_for_match")
        self.assertEqual(status["startup_reason"], "Keep the plan.")

    def test_idle_heartbeat_probes_session_and_recovers_a_dead_process(self):
        self.session.prepare()
        previous = self.session.thread_id
        request_count = self.session._counter
        self.session._last_health -= 11
        status = self.session.maintain()
        self.assertTrue(status["ready"])
        self.assertLess(status["heartbeat_age_s"], 1)
        self.assertEqual(self.session._counter, request_count + 1)
        self.session.process.kill()
        self.session.process.wait(2)
        self.assertFalse(self.session.status()["ready"])
        self.session.maintain()
        self.assertTrue(self.session.status()["ready"])
        self.assertEqual(self.session.thread_id, previous)
        self.assertEqual(len(self.launches), 2)

    def test_timeout_retires_process_and_resume_preserves_session_identity(self):
        self.session.consult("first", decision_schema(), match_id="match-one")
        previous = self.session.thread_id
        self.session.timeout = 0.1
        with self.assertRaises(SessionError):
            self.session.consult("timeout", decision_schema(), match_id="match-one")
        # A request timeout must also terminate the stalled peer.
        self.assertIsNone(self.session.process)
        self.session.timeout = 2
        self.session.consult("recovered", decision_schema(), match_id="match-one")
        self.assertEqual(previous, self.session.thread_id)
        self.assertEqual(len(self.launches), 2)

    def test_contract_accepts_tactical_actions_and_rejects_extra_fields(self):
        validate_decision(batch(order("retreat", ids=(1,), target={"kind": "self"}, ttl=5)))
        with self.assertRaises(ValueError):
            validate_decision({**batch(), "other_team": "dire"})
        with self.assertRaises(ValueError):
            validate_decision(batch(order(), order()))

    def test_opponent_has_competitive_instructions_and_cross_team_snapshot_is_rejected(self):
        coach = ModelCoach(directory=self.temporary.name, team="dire", opponent=True)
        self.assertIn("maximum competitive pressure", coach.session.instructions)
        with self.assertRaisesRegex(RuntimeError, "different team"):
            coach({"team": "radiant", "session_id": "some-match"})


if __name__ == "__main__":
    unittest.main()
