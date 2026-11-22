from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from team_coach.console_telemetry import ConsoleTelemetry, LuaMailbox
from team_coach.service import CoachService
import test_team_coach as fixtures
from test_team_coach import MatchClock, report


def packet(team, index=0, *, clock_s=950, calls=(), roshan=None):
    sample = report(
        clock_s, bot_id=index, team=team, threat="mid",
        push_window=True, enemies_alive=False,
    )
    sample["bot"].update(
        desires={"push_mid": 0.56, "defend_mid": 0.61, "roshan": 0.58},
        nearby_enemies=0, nearby_allies=2, under_fire=False,
    )
    if team == "dire":
        sample["bot"]["id"] += 5
        if "map" in sample:
            for enemy in sample["map"]["enemies"]:
                enemy["id"] -= 5
    if "map" in sample:
        sample["map"]["human_ids"] = [3 if team == "radiant" else 8]
    if calls:
        sample["calls"] = list(calls)
    if "map" in sample and roshan is not None:
        sample["map"]["roshan"] = roshan
    return sample


def call(text, *, id="call-1", player_id=3, clock_s=950, source="chat"):
    return {"id": id, "player_id": player_id, "source": source, "clock_s": clock_s, "text": text}


class DualCoachTests(unittest.TestCase):
    def new_service(self, team="radiant", *, enable_calls=False, mode="rules", journal=None):
        caller_id = (3 if team == "radiant" else 8) if enable_calls else None
        return CoachService(
            team=team, mode=mode, accept_player_calls=enable_calls, caller_id=caller_id,
            clock=MatchClock(), journal=journal,
        )

    def snapshot(self, service, **state):
        for index in (2, 1):
            service.exchange(packet(service.team, index, clock_s=state.get("clock_s", 950)))
        return service.exchange(packet(service.team, **state))

    def test_matching_chat_call_affects_only_its_team(self):
        radiant_events = []
        dire_events = []
        radiant = self.new_service(enable_calls=True, journal=radiant_events.append)
        dire = self.new_service("dire", journal=dire_events.append)
        radiant_reply = self.snapshot(radiant)
        dire_reply = self.snapshot(dire)
        self.assertNotEqual(radiant_reply["session_id"], dire_reply["session_id"])

        player_call = call("!пуш мид")
        decision = radiant.exchange(packet("radiant", calls=[player_call]))
        self.assertEqual((decision["plan"]["objective"], decision["plan"]["lane"]), ("push", "mid"))
        self.assertLessEqual(set(decision["plan"]["bot_ids"]), {0, 1, 2})
        radiant_call = radiant.status()["player_call"]
        self.assertEqual((radiant_call["action"], radiant_call["lane"], radiant_call["source"]),
                         ("push", "mid", "chat"))
        self.assertEqual(radiant_call["status"], "accepted")

        with self.assertRaises(ValueError):
            dire.exchange(packet("radiant", calls=[player_call]))
        opposing = dire.exchange(packet("dire", calls=[player_call]))
        self.assertEqual(opposing, dire_reply)
        self.assertNotIn("player_call", dire.status())
        self.assertTrue(radiant.status()["calls_enabled"])
        self.assertFalse(dire.status()["calls_enabled"])
        self.assertNotIn("player_call", opposing)
        self.assertNotIn("!пуш мид", str(dire_events))
        self.assertNotIn("!пуш мид", str(radiant.status()["context"]))

        with self.assertRaises(ValueError):
            dire.submit_command({
                "session_id": radiant_reply["session_id"], "clock_s": 950, "action": "defend",
                "target": {"kind": "lane", "lane": "mid"}, "bot_ids": [5, 6], "ttl_s": 25,
                "reason": "This session belongs to the other team.",
            })
        self.assertIsNone(dire.status()["plan"])

    def test_caller_id_is_checked_and_repeated_reports_do_not_reissue_call(self):
        journal = []
        service = self.new_service(enable_calls=True, journal=journal.append)
        self.snapshot(service)
        unauthorized = call("!пуш мид", player_id=4)
        reply = service.exchange(packet("radiant", calls=[unauthorized]))
        self.assertIsNone(reply["plan"])
        self.assertIsNone(service.status().get("player_call"))

        forged = packet("radiant", calls=[call("!пуш мид", id="wrong-roster")])
        forged["map"]["human_ids"] = [4]
        self.assertIsNone(service.exchange(forged)["plan"])
        self.assertIsNone(service.status().get("player_call"))

        authorized = call("!пуш мид", id="call-2")
        original = service.exchange(packet("radiant", calls=[authorized]))
        self.assertIsNotNone(original["plan"])
        repeated = self.snapshot(service, clock_s=955, calls=[authorized])
        self.assertEqual(repeated["plan"], original["plan"])
        self.assertEqual(sum(row["event"] == "plan_accepted" for row in journal), 1)

    def test_ping_is_a_hint_and_shadow_chat_cannot_publish_a_plan(self):
        cases = (
            ("rules", call("!пуш мид", source="ping"), "hint"),
            ("shadow", call("!пуш мид"), "observed"),
        )
        for mode, intent, expected in cases:
            with self.subTest(mode=mode, source=intent["source"]):
                service = self.new_service(enable_calls=True, mode=mode)
                reply = self.snapshot(service, calls=[intent])
                self.assertIsNone(reply["plan"])
                self.assertEqual(service.status()["player_call"]["source"], intent["source"])
                self.assertEqual(service.status()["player_call"]["status"], expected)
                self.assertEqual(service.status()["commands"], [])
                self.assertEqual(service.status()["calls"], 0)

    def test_player_cancel_drops_own_plan_but_keeps_manually_submitted_plan(self):
        service = self.new_service(enable_calls=True)
        accepted = self.snapshot(service, calls=[call("!пуш мид")])
        self.assertIsNotNone(accepted["plan"])
        cancel = call("!отмена", id="call-2", clock_s=951)
        self.assertIsNone(self.snapshot(service, clock_s=951, calls=[cancel])["plan"])
        self.assertEqual(service.status()["player_call"]["status"], "cancelled")

        typed = service.submit_command({
            "session_id": accepted["session_id"], "clock_s": 951, "action": "defend",
            "target": {"kind": "lane", "lane": "mid"}, "bot_ids": [0, 1], "ttl_s": 25,
            "reason": "Manual direction from the coach console.",
        })
        cancel = call("!отмена", id="call-3", clock_s=952)
        retained = self.snapshot(service, clock_s=952, calls=[cancel])
        self.assertEqual(retained["plan"]["id"], typed["id"])

    def test_delayed_older_chat_cannot_reactivate_cancelled_order(self):
        service = self.new_service(enable_calls=True)
        self.assertIsNotNone(self.snapshot(service, calls=[call("!пуш мид")])["plan"])
        cancel = call("!отмена", id="call-3", clock_s=954)
        self.assertIsNone(self.snapshot(service, clock_s=954, calls=[cancel])["plan"])

        earlier = call("!пуш мид", id="call-2", clock_s=952)
        self.assertIsNone(self.snapshot(service, clock_s=955, calls=[earlier])["plan"])
        self.assertEqual(service.status()["player_call"]["id"], "call-3")

        fresh = call("!пуш мид", id="call-4", clock_s=956)
        self.assertIsNotNone(self.snapshot(service, clock_s=956, calls=[fresh])["plan"])

    def test_roshan_call_needs_live_objective_and_eligible_bots(self):
        service = self.new_service(enable_calls=True)
        idle = self.snapshot(
            service, calls=[call("!рош")], roshan={"visible": False, "estimated_alive": False},
        )
        self.assertIsNone(idle["plan"])
        self.assertEqual(service.status()["player_call"]["status"], "waiting")
        self.assertFalse(any(command["action"] == "roshan" for command in service.status()["commands"]))

        live_roshan = {"visible": True, "estimated_alive": True}
        for index in (1, 2):
            contested = packet("radiant", index, clock_s=952)
            contested["bot"]["nearby_enemies"] = 2
            service.exchange(contested)
        service.exchange(packet("radiant", clock_s=952, roshan=live_roshan))
        self.assertEqual(service.status()["player_call"]["status"], "waiting")
        self.assertEqual(service.status()["commands"], [])

        self.snapshot(service, clock_s=954, roshan=live_roshan)
        commands = [command for command in service.status()["commands"] if command["action"] == "roshan"]
        command, = commands
        self.assertGreaterEqual(len(command["bot_ids"]), 2)
        self.assertEqual(command["target"], {"kind": "roshan"})
        self.assertEqual(service.status()["player_call"]["status"], "accepted")

    def test_roshan_call_waits_for_bots_to_leave_native_push_mode(self):
        service = self.new_service(enable_calls=True)
        live_roshan = {"visible": True, "estimated_alive": True}
        for index in (2, 1):
            pushing = packet("radiant", index)
            pushing["bot"]["mode"] = "push_mid"
            service.exchange(pushing)
        pushing = packet("radiant", roshan=live_roshan, calls=[call("!rosh")])
        pushing["bot"]["mode"] = "push_mid"
        service.exchange(pushing)
        self.assertEqual(service.status()["player_call"]["status"], "waiting")
        self.assertEqual(service.status()["commands"], [])

        for index in (2, 1):
            service.exchange(packet("radiant", index, clock_s=952))
        command, = service.status()["commands"]
        self.assertEqual(command["action"], "roshan")
        self.assertEqual(command["bot_ids"], [1, 2])

    def test_roshan_call_survives_the_old_expiry_while_allies_are_fighting(self):
        service = self.new_service(enable_calls=True)
        roshan_alive = {"visible": False, "estimated_alive": True}
        for tick in (950, 960):
            for index in (2, 1, 0):
                options = {"calls": [call("!rosh")]} if tick == 950 and index == 0 else {}
                fighting = packet("radiant", index, clock_s=tick, roshan=roshan_alive, **options)
                fighting["bot"].update(mode="attack", nearby_enemies=2)
                service.exchange(fighting)
            status = service.status()
            self.assertEqual(status["commands"], [])
            self.assertEqual(status["player_call"]["status"], "waiting")
            self.assertEqual(status["player_call"]["reason"], "Allies are engaged with enemy heroes")

        self.snapshot(service, clock_s=965, roshan=roshan_alive)
        command, = service.status()["commands"]
        self.assertEqual(command["action"], "roshan")
        self.assertEqual(command["expires_clock_s"], 995)
        self.assertEqual(service.status()["player_call"]["status"], "accepted")

    def test_ineligible_chat_call_expires_without_becoming_a_plan(self):
        service = self.new_service(enable_calls=True)
        self.snapshot(service, calls=[call("!push top")])
        self.assertEqual(service.status()["player_call"]["status"], "waiting")
        result = self.snapshot(service, clock_s=959)
        self.assertIsNone(result["plan"])
        self.assertEqual(service.status()["player_call"]["status"], "expired")

    def test_file_transport_acknowledges_player_orders_across_board_reporter_failover(self):
        journal = []
        service = self.new_service(enable_calls=True, journal=journal.append)
        roshan_alive = {"visible": True, "estimated_alive": True}
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            console = directory / "console.log"
            mailbox = LuaMailbox(directory / "team_coach_replies_radiant.lua")
            reader = ConsoleTelemetry(console, mailbox)
            sequence = 0

            def append(sample):
                nonlocal sequence
                sequence += 1
                nonce = f"900-{sequence}"
                with console.open("ab") as output:
                    output.writelines(fixtures.ConsoleTelemetryTests.fragments({
                        **sample, "request_id": nonce, "reply_team": True,
                    }))
                return nonce

            def response_line(nonce):
                return next(
                    line for line in mailbox.path.read_text(encoding="ascii").splitlines()
                    if nonce in line
                )

            for index in (1, 2):
                append(packet("radiant", index))
            append(packet("dire", calls=[call("!push mid", id="opposing")]))
            append(packet("radiant", calls=[call("!push mid", id="wrong-author", player_id=4)]))
            reader.ingest(service)
            self.assertIsNone(service.status()["plan"])
            self.assertIsNone(service.status()["player_call"])

            nonce = append(packet("radiant", calls=[call("!push mid")]))
            reader.ingest(service)
            accepted = service.status()
            push = accepted["plan"]
            self.assertEqual((push["objective"], push["lane"]), ("push", "mid"))
            self.assertIn(push["id"], response_line(nonce))

            pushing = packet("radiant", 1, clock_s=951)
            pushing["bot"]["mode"] = "push_mid"
            pushing["applied"] = {"plan_id": push["id"], "objective": "push", "lane": "mid"}
            append(pushing)
            reader.ingest(service)
            self.assertEqual(service.status()["applied"], [1])

            def backup_board(clock_s, roshan, *, calls=()):
                current = packet("radiant", 1, clock_s=clock_s, calls=calls)
                current["map"] = packet("radiant", clock_s=clock_s, roshan=roshan)["map"]
                return current

            former_reporter = packet("radiant", clock_s=952)
            former_reporter.pop("map")
            former_reporter["bot"].update(alive=False, hp=0)
            append(former_reporter)
            first = call("!rosh", id="roshan-first", clock_s=952)
            nonce = append(backup_board(952, roshan_alive, calls=[first]))
            reader.ingest(service)
            status = service.status()
            self.assertEqual(status["session_id"], accepted["session_id"])
            self.assertEqual(status["player_call"]["status"], "accepted")
            rosh, = status["commands"]
            self.assertEqual((rosh["action"], rosh["target"], rosh["bot_ids"]),
                             ("roshan", {"kind": "roshan"}, [1, 2]))
            self.assertIn(rosh["id"], response_line(nonce))
            self.assertNotIn(push["id"], response_line(nonce))

            unauthorized = {**former_reporter, "clock_s": 953, "applied_commands": [rosh["id"]]}
            unauthorized["bot"] = {**packet("radiant")["bot"], "mode": "roshan", "nearby_enemies": 2}
            wrong_nonce = append(unauthorized)
            ready = backup_board(953, roshan_alive)
            ready["bot"]["mode"] = "roshan"
            ready["applied_commands"] = [rosh["id"]]
            nonce = append(ready)
            reader.ingest(service)
            self.assertNotIn(rosh["id"], response_line(wrong_nonce))
            self.assertIn(rosh["id"], response_line(nonce))
            self.assertEqual(service.status()["command_applied"], {rosh["id"]: [1]})

            nonce = append(backup_board(954, {"visible": False, "estimated_alive": False}))
            reader.ingest(service)
            self.assertEqual(service.status()["commands"], [])
            self.assertNotIn(rosh["id"], response_line(nonce))

            retry = call("!rosh", id="roshan-retry", clock_s=955)
            append(backup_board(955, roshan_alive, calls=[retry]))
            reader.ingest(service)
            replacement, = service.status()["commands"]
            expiry = replacement["expires_clock_s"]
            nonce = append(backup_board(expiry, roshan_alive))
            reader.ingest(service)
            self.assertEqual(service.status()["commands"], [])
            self.assertNotIn(replacement["id"], response_line(nonce))
            self.assertEqual(sum(event["event"] == "bot_applied" for event in journal), 1)
            self.assertEqual(sum(event["event"] == "command_applied" for event in journal), 1)

    def test_console_router_keeps_both_file_sessions_and_plans_independent(self):
        services = {
            "radiant": self.new_service(enable_calls=True),
            "dire": self.new_service("dire", mode="observe"),
        }
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            console = directory / "console.log"
            reader = ConsoleTelemetry(console)
            mailboxes = {
                side: LuaMailbox(directory / f"team_coach_replies_{side}.lua")
                for side in services
            }

            def append(sample, request_id):
                request = {**sample, "request_id": request_id, "reply_team": True}
                with console.open("ab") as output:
                    output.writelines(fixtures.ConsoleTelemetryTests.fragments(request))

            append(packet("radiant", 1), "10-1")
            append(packet("dire", 1), "20-1")
            append(packet("radiant", calls=[call("!пуш мид")]), "11-1")
            append(packet("dire"), "21-1")
            reader.ingest_sides(services, mailboxes)
            radiant = services["radiant"].status()
            dire = services["dire"].status()
            self.assertNotEqual(radiant["session_id"], dire["session_id"])
            self.assertEqual(radiant["player_call"]["status"], "accepted")
            self.assertNotIn("player_call", dire)

            dire_plan = services["dire"].submit_command({
                "session_id": dire["session_id"], "clock_s": 950, "action": "defend",
                "target": {"kind": "lane", "lane": "mid"}, "bot_ids": [5, 6], "ttl_s": 25,
                "reason": "Protect the Dire mid tower.",
            })
            append(packet("radiant", clock_s=951), "11-2")
            append(packet("dire", clock_s=951), "21-2")
            append({**packet("radiant", clock_s=952), "team": "unknown"}, "31-2")
            reader.ingest_sides(services, mailboxes)
            radiant_id = radiant["plan"]["id"]
            radiant_output = mailboxes["radiant"].path.read_text(encoding="ascii")
            dire_output = mailboxes["dire"].path.read_text(encoding="ascii")
            self.assertIn("11-2", radiant_output)
            self.assertIn(radiant_id, radiant_output)
            self.assertIn(radiant["session_id"], radiant_output)
            self.assertIn("radiant", radiant_output)
            self.assertNotIn(r"\034player_call\034:", radiant_output)
            self.assertNotIn(dire_plan["id"], radiant_output)
            self.assertNotIn("31-2", radiant_output)
            self.assertIn("21-2", dire_output)
            self.assertIn(dire_plan["id"], dire_output)
            self.assertIn(dire["session_id"], dire_output)
            self.assertIn("dire", dire_output)
            self.assertNotIn("player_call", dire_output)
            self.assertNotIn(radiant_id, dire_output)

            append(packet("radiant", clock_s=10), "11-3")
            reader.ingest_sides(services, mailboxes)
            radiant_after = mailboxes["radiant"].path.read_text(encoding="ascii")
            self.assertNotIn(radiant["session_id"], radiant_after)
            self.assertEqual(mailboxes["dire"].path.read_text(encoding="ascii"), dire_output)

            with console.open("ab") as output:
                output.write(b"S:Gamerules: entering state 'DOTA_GAMERULES_STATE_POST_GAME'\n")
            reader.ingest_sides(services, mailboxes)
            self.assertTrue(all(service.status()["phase"] == "idle" for service in services.values()))
            self.assertTrue(all(mailbox.path.read_text(encoding="ascii") == "return {\n}\n"
                                for mailbox in mailboxes.values()))
