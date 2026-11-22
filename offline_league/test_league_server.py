from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from uuid import UUID
from urllib.parse import urlencode

from league_server import CALIBRATION_MATCHES, DEFAULT_TUNING, HERO_POOLS, League, LeagueHTTPServer, LeagueLogIPC, START_RATING
from league_social import SIMULATED_MATCHES_PER_RESULT, chance, roster


# Match the game's deadline for file-backed league requests.
RESPONSE_TIMEOUT = 10


class LeagueServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.database = Path(self.temporary.name) / "league.sqlite3"
        self.start_server()

    def tearDown(self):
        self.stop_server()
        self.temporary.cleanup()

    def start_server(self):
        self.server = LeagueHTTPServer(League(self.database), port=0)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def request(self, path, payload=None, headers=None):
        client = HTTPConnection("127.0.0.1", self.server.server_port, timeout=RESPONSE_TIMEOUT)
        try:
            if payload is None:
                client.request("GET", path, headers=headers or {})
            else:
                client.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
            response = client.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            client.close()

    def get(self, path):
        response, value = self.request(path)
        self.assertEqual(response, 200, value)
        return value

    def test_queue_balances_two_role_complete_teams(self):
        self.assertEqual(self.get("/health")["schema"], 5)
        self.assertFalse(self.get("/health")["file_ipc"])
        match = self.get("/queue?role=2")["match"]
        self.assertEqual(match["role"], 2)
        lineup = match["participants"]
        self.assertEqual([player["slot"] for player in lineup], list(range(10)))
        self.assertEqual(Counter(player["team"] for player in lineup), {2: 5, 3: 5})
        for team in (2, 3):
            self.assertEqual({player["role"] for player in lineup if player["team"] == team}, set(range(1, 6)))
        self.assertEqual(sum(player["is_player"] for player in lineup), 1)
        bots = [player for player in lineup if not player["is_player"]]
        self.assertEqual(len({player["hero"] for player in bots}), 9)
        self.assertEqual(len({player["bot_id"] for player in bots}), 9)
        self.assertAlmostEqual(match["expected_win"], 0.5, delta=0.07)
        self.assertEqual(self.get("/queue?role=0")["match"], match)
        self.assertEqual(self.request("/queue?role=5")[0], 409)

    def test_every_human_role_is_unique_on_radiant_and_bot_picks_match_their_roles(self):
        for preferred in range(1, 6):
            match = self.get(f"/queue?role={preferred}")["match"]
            radiant = [member for member in match["participants"] if member["team"] == 2]
            self.assertEqual(radiant[0]["role"], preferred)
            self.assertEqual(
                {member["role"] for member in radiant if not member["is_player"]},
                set(range(1, 6)) - {preferred},
            )
            for member in match["participants"][1:]:
                self.assertIn(member["hero"].removeprefix("npc_dota_hero_"), HERO_POOLS[member["role"]])
            self.get(f"/cancel?id={match['id']}")

    def test_tuning_is_persistent_and_each_match_keeps_its_settings(self):
        self.assertEqual(self.get("/state")["tuning"], DEFAULT_TUNING)
        selected = {"bot_difficulty": 2, "micro": 75, "macro": 25, "coach": 0}
        status, saved = self.request("/settings", selected)
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["tuning"], selected)
        match = self.get("/queue?role=2")["match"]
        self.assertEqual(match["tuning"], selected)
        self.assertEqual(match["team_difficulties"], {"radiant": 2, "dire": 2})
        self.assertEqual(self.request("/settings", {"micro": 100})[0], 200)
        self.assertEqual(self.get("/queue?role=2")["match"]["tuning"], selected)
        self.stop_server()
        self.start_server()
        self.assertEqual(self.get("/state")["tuning"], {**selected, "micro": 100})
        self.assertEqual(self.get("/state")["active_match"]["tuning"], selected)
        self.get(f"/cancel?id={match['id']}")
        self.assertEqual(self.get("/queue?role=2")["match"]["tuning"], {**selected, "micro": 100})

    def test_tuning_rejects_bad_values_without_partial_writes(self):
        for invalid in ({"micro": -1}, {"macro": 101}, {"coach": True}, {"bot_difficulty": 5},
                        {"micro": 55.5}, {"micro": None}, {"bonus_gold": 100}):
            self.assertEqual(self.request("/settings", invalid)[0], 400, invalid)
            self.assertEqual(self.get("/state")["tuning"], DEFAULT_TUNING)

    def test_save_and_retry_result_across_parallel_clients_and_restart(self):
        match = self.get("/queue?role=3")["match"]
        query = urlencode({
            "id": match["id"],
            "winner": 2,
            "duration": 607,
            "radiant_score": 7,
            "dire_score": 3,
            "stats": "0,npc_dota_hero_axe,7,3,2,480,540,67,6;1,npc_dota_hero_lina,0,1,4",
        })

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(self.get, [f"/finish?{query}"] * 2))

        self.assertEqual(first["result"], second["result"])
        self.assertEqual(first["profile"]["wins"], 1)
        self.assertGreater(first["profile"]["rating"], START_RATING)
        self.assertEqual(first["result"]["hero"], "npc_dota_hero_axe")
        self.assertEqual(first["result"]["radiant_score"], 7)
        self.assertEqual(first["result"]["participants"][0]["last_hits"], 67)
        self.assertEqual(first["result"]["participants"][1]["hero"], "npc_dota_hero_lina")
        self.assertEqual(first["result"]["participants"][1]["planned_hero"], match["participants"][1]["hero"])
        self.assertIsNone(first["result"]["participants"][2]["kills"])
        self.assertEqual(self.request(f"/finish?id={match['id']}&winner=3")[0], 409)

        self.stop_server()
        self.start_server()
        after_restart = self.get("/state")
        self.assertEqual(after_restart["profile"], first["profile"])
        self.assertEqual(len(after_restart["history"]), 1)
        self.assertEqual(after_restart["history"][0]["kills"], 7)
        self.assertNotIn("participants", after_restart["history"][0])
        self.assertNotIn("active_match", after_restart)
        self.assertEqual(self.get(f"/match?id={match['id']}")["result"], first["result"])
        new_match = self.get("/queue?role=2")["match"]
        self.assertEqual(new_match["participants"][0]["rating"], first["profile"]["rating"])

    def test_cancel_has_no_effect_on_calibration(self):
        match = self.get("/queue")["match"]
        self.stop_server()
        self.start_server()
        self.assertEqual(self.get("/state")["active_match"], match)
        after = self.get(f"/cancel?id={match['id']}")
        self.assertEqual(after["profile"]["rating"], START_RATING)
        self.assertEqual(after["profile"]["calibration_played"], 0)
        self.assertEqual(after["history"], [])
        self.assertEqual(self.request(f"/finish?id={match['id']}&winner=2")[0], 409)
        self.assertNotEqual(self.get("/queue")["match"]["id"], match["id"])

    def test_social_memory_is_atomic_and_idempotent(self):
        match = self.get("/queue?role=2")["match"]
        start = self.get("/state")["social"]
        self.assertEqual(start, {
            "population": 10_000, "simulated_matches": 0, "contacts": [], "bot_ties": [],
        })
        self.assertEqual(len(roster()), 10_000)
        self.assertEqual(len({bot["name"] for bot in roster().values()}), 10_000)
        self.assertEqual(Counter(bot["role"] for bot in roster().values()), {role: 2000 for role in range(1, 6)})

        self.assertEqual(self.get(f"/cancel?id={match['id']}")["social"], start)
        finished_match = self.get("/queue?role=2")["match"]
        query = f"/finish?id={finished_match['id']}&winner=2"
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(self.get, [query] * 2))
        self.assertEqual(first["social"], second["social"])
        self.assertEqual(first["social"]["simulated_matches"], SIMULATED_MATCHES_PER_RESULT)
        self.assertEqual(len(first["social"]["contacts"]), 6)
        self.assertTrue(all(contact["played"] == 1 for contact in first["social"]["contacts"]))
        self.assertTrue(first["social"]["bot_ties"])
        with self.server.league.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM social_edges WHERE a = '@you'").fetchone()[0], 9)
            counts = db.execute("""
                SELECT SUM(with_games + against_games - simulated), SUM(simulated)
                FROM social_edges
            """).fetchone()
            self.assertEqual(tuple(counts), (45, 45 * SIMULATED_MATCHES_PER_RESULT))

        self.stop_server()
        self.start_server()
        self.assertEqual(self.get("/state")["social"], first["social"])

    def test_social_matchmaking_reuses_bonds_and_counts_intensity_in_balance(self):
        appearances = Counter()
        recurring_matches = boosted = 0
        for index in range(12):
            # Fixed UUIDs make both profile selection and simulation reproducible.
            with patch("league_server.uuid4", return_value=UUID(int=127 + index)):
                match = self.get("/queue?role=2")["match"]
            bots = match["participants"][1:]
            returning = [bot for bot in bots if bot["bot_id"] in appearances]
            self.assertLessEqual(len(returning), 4)
            appearances.update(bot["bot_id"] for bot in bots)
            recurring_matches += bool(match["social_pairs"])
            boosted += sum(bot["effort"] for bot in bots)
            strengths = Counter()
            for participant in match["participants"]:
                strengths[participant["team"]] += participant["rating"]
                if participant["is_player"]:
                    continue
                original = roster()[participant["bot_id"]]
                self.assertEqual(participant["difficulty"], original["difficulty"] + participant["effort"])
                if participant["effort"]:
                    self.assertGreater(participant["rating"], original["rating"])
            self.assertEqual(match["expected_win"], round(chance(strengths[2], strengths[3]), 4))
            self.assertAlmostEqual(match["expected_win"], 0.5, delta=0.08)
            winner = 2 if index % 3 != 2 else 3
            self.get(f"/finish?id={match['id']}&winner={winner}")
        self.assertGreaterEqual(recurring_matches, 6)
        self.assertGreaterEqual(boosted, 8)
        self.assertGreaterEqual(len(appearances), 70)

    def test_social_migration_replays_legacy_history_without_changing_profile(self):
        # Simulate a database from before social history.
        self.stop_server()
        identity = "0" * 32
        with self.server.league.connection(write=True) as db:
            db.execute("DROP TABLE social_edges")
            db.execute("DROP TABLE social_world")
            bot_ids = [f"bot-{role}-2-{hero}" for role in range(1, 6) for hero in (0, 1)][1:]
            participants = [{"name": "Ты", "team": 2, "is_player": True}]
            participants.extend(
                {**roster()[bot_id], "team": 2 if slot < 4 else 3}
                for slot, bot_id in enumerate(bot_ids)
            )
            lineup = {"id": identity, "participants": participants}
            result = {"id": identity, "winner": 2, "participants": participants}
            db.execute("""
                INSERT INTO matches (id, queued_at, status, lineup, completed_at, result)
                VALUES (?, ?, 'complete', ?, ?, ?)
            """, (identity, "2026-11-17T09:00:00Z", json.dumps(lineup), "2026-11-17T09:15:00Z", json.dumps(result)))
            db.execute("UPDATE profile SET rating = 1554, wins = 1 WHERE id = 1")
        self.start_server()
        state = self.get("/state")
        self.assertEqual((state["profile"]["rating"], state["profile"]["wins"]), (1554, 1))
        self.assertEqual(state["social"]["simulated_matches"], 0)
        self.assertTrue(all(contact["played"] == 1 for contact in state["social"]["contacts"]))
        self.assertEqual(self.get(f"/match?id={identity}")["result"]["winner"], 2)
        self.stop_server()
        self.start_server()
        self.assertEqual(self.get("/state")["social"], state["social"])

    def test_calibrate_from_completed_matches_and_validate_input(self):
        self.assertEqual(self.request("/queue?role=6")[0], 400)
        self.assertEqual(self.request("/queue?role=3&role=4")[0], 400)
        self.assertEqual(self.request("/queue", headers={"Origin": "https://example.com"})[0], 403)
        for played in range(1, CALIBRATION_MATCHES + 1):
            match = self.get("/queue")["match"]
            status, finished = self.request("/finish", {"id": match["id"], "winner": 3, "duration": 500})
            self.assertEqual(status, 200)
            self.assertEqual(finished["profile"]["calibration_played"], played)
            self.assertEqual(finished["profile"]["calibrated"], played >= CALIBRATION_MATCHES)
        self.assertEqual(finished["profile"]["losses"], CALIBRATION_MATCHES)
        self.assertNotEqual(finished["profile"]["medal"], "Калибровка")
        self.assertLess(finished["profile"]["rating"], START_RATING)
        self.assertEqual(len(self.get("/state?offset=2")["history"]), CALIBRATION_MATCHES - 2)


class FileIPCTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name)
        self.role_file = path / "team_coach_league_roles.lua"
        self.league = League(path / "league.sqlite3")
        self.log = path / "console.log"
        self.replies = path / "league_ipc"

    def start_monitor(self):
        monitor = LeagueLogIPC(self.league, self.log, self.replies)
        monitor.start()
        self.addCleanup(monitor.close)
        return monitor

    def append(self, message):
        with self.log.open("ab") as target:
            target.write(message.encode("utf-8"))

    def reply(self, request_id):
        response = self.replies / f"{request_id}.txt"
        deadline = monotonic() + RESPONSE_TIMEOUT
        while not response.exists() and monotonic() < deadline:
            sleep(0.01)
        self.assertTrue(response.exists(), f"Missing file reply for {request_id}")
        document = response.read_text(encoding="ascii")
        self.assertTrue(document.startswith('"reply"\n{\n'))
        self.assertTrue(document.endswith("}\n"))
        status = re.search(r'"status" "([0-9]{3})"', document)
        self.assertIsNotNone(status)
        chunks = re.findall(r'"part_([0-9]+)" "([a-f0-9]+)"', document)
        self.assertTrue(chunks)
        self.assertEqual([int(index) for index, _ in chunks], list(range(1, len(chunks) + 1)))
        self.assertTrue(all(0 < len(part) <= 512 and len(part) % 2 == 0 for _, part in chunks))
        return int(status[1]), json.loads(b"".join(bytes.fromhex(part) for _, part in chunks).decode("utf-8"))

    def test_missing_log_at_startup_with_nested_utf8_and_duplicate_queue(self):
        monitor = self.start_monitor()
        self.assertTrue(monitor.running)
        with LeagueHTTPServer(self.league, port=0, ipc=monitor) as server:
            http_thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            http_thread.start()
            try:
                with self.subTest("health flag"):
                    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=RESPONSE_TIMEOUT)
                    try:
                        conn.request("GET", "/health")
                        payload = json.loads(conn.getresponse().read().decode("utf-8"))
                        self.assertEqual(payload, {"service": "dota_offline_league", "schema": 5, "file_ipc": True})
                    finally:
                        conn.close()
            finally:
                server.shutdown()
                http_thread.join(timeout=5)

        self.append("unrelated text\r\n")
        self.append("[Lua] [offline-league-ipc] abcd00000001 queue role=2\r\n")
        queued_status, queued = self.reply("abcd00000001")
        self.assertEqual(queued_status, 200)
        self.assertEqual(queued["profile"]["medal"], "Калибровка")
        self.assertEqual(queued["match"]["participants"][0]["name"], "Ты")
        match_id = queued["match"]["id"]
        self.append(f"[offline-league-ipc] abcd00000002 finish id={match_id}&winner=2&duration=900\n")
        self.append("[offline-league-ipc] abcd00000001 queue role=2\n")
        self.append("[offline-league-ipc] abcd00000003 state -\n")
        finish_status, finished = self.reply("abcd00000002")
        self.assertEqual(finish_status, 200)
        self.assertEqual(finished["result"]["id"], match_id)
        self.assertEqual(len(finished["result"]["participants"]), 10)
        state_status, state = self.reply("abcd00000003")
        self.assertEqual(state_status, 200)
        self.assertEqual(state["profile"]["wins"], 1)
        self.assertNotIn("active_match", state)
        self.assertEqual(self.reply("abcd00000001"), (queued_status, queued))

    def test_role_file_follows_queue_draft_cancel_and_finish(self):
        self.league = League(self.league.database, self.role_file)
        self.assertEqual(self.role_file.read_text(encoding="ascii"), "return nil\n")
        self.start_monitor()
        self.append("[offline-league-ipc] abcd00000011 queue role=2\n")
        queued_status, queued = self.reply("abcd00000011")
        self.assertEqual(queued_status, 200)
        match = queued["match"]
        identity = match["id"]
        selected_bot = match["participants"][1]
        selected_hero = selected_bot["hero"]
        role_lua = self.role_file.read_text(encoding="ascii")
        self.assertIn(f'id = "{identity}", human_role = 2', role_lua)
        self.assertIn("radiant = {\n        { role = 2 },\n", role_lua)
        self.assertIn(f'hero = "{selected_hero}"', role_lua)

        self.append(f"[offline-league-ipc] abcd00000012 draft id={identity}&hero={selected_hero}\n")
        draft_status, draft = self.reply("abcd00000012")
        self.assertEqual(draft_status, 200)
        participants = draft["match"]["participants"]
        self.assertEqual(participants[0]["hero"], selected_hero)
        self.assertNotEqual(participants[1]["hero"], selected_hero)
        self.assertEqual(participants[1]["bot_id"], selected_bot["bot_id"])
        self.assertEqual(participants[1]["difficulty"], selected_bot["difficulty"])
        self.assertEqual(participants[1]["role"], selected_bot["role"])
        self.assertEqual(participants[2:], match["participants"][2:])
        self.assertEqual(len({member["hero"] for member in participants}), 10)
        self.assertNotIn(f'hero = "{selected_hero}"', self.role_file.read_text(encoding="ascii"))
        self.assertIn(f'hero = "{participants[1]["hero"]}"', self.role_file.read_text(encoding="ascii"))

        self.append(f"[offline-league-ipc] abcd00000013 cancel id={identity}\n")
        self.assertEqual(self.reply("abcd00000013")[0], 200)
        self.assertEqual(self.role_file.read_text(encoding="ascii"), "return nil\n")
        self.append(f"[offline-league-ipc] abcd00000014 draft id={identity}&hero={selected_hero}\n")
        self.assertEqual(self.reply("abcd00000014")[0], 409)

        match = self.league.queue(4)["match"]
        self.assertIn("human_role = 4", self.role_file.read_text(encoding="ascii"))
        self.league.finish({"id": match["id"], "winner": 2})
        self.assertEqual(self.role_file.read_text(encoding="ascii"), "return nil\n")

    def test_fragmented_finish_persists_full_scoreboard_and_finds_the_replay(self):
        self.league = League(self.league.database, replay_dir=self.log.parent)
        monitor = self.start_monitor()
        match = self.league.queue(2)
        identity = match["match"]["id"]
        rows = ";".join(
            f"{slot},npc_dota_hero_crystal_maiden,1,2,3,400,500,20,2,15,12000,25000,1200"
            for slot in range(10)
        )
        payload = "finish:" + urlencode({"id": identity, "winner": 2, "stats": rows})
        parts = [payload[index:index + 760] for index in range(0, len(payload), 760)]
        for number, part in enumerate(parts, 1):
            line = f"[VScript] [offline-league-ipc] abcd00000030 part {number}/{len(parts)} {part}\n"
            self.append(line)
        status, saved = self.reply("abcd00000030")
        self.assertTrue(monitor.running)
        self.assertEqual(status, 200)
        result = saved["result"]
        self.assertEqual(len(result["participants"]), 10)
        self.assertEqual(result["participants"][9]["hero_damage"], 25000)
        self.assertEqual(result["participants"][9]["net_worth"], 12000)
        self.assertNotIn("replay", result)
        replay = self.log.parent / f"offline_league_{identity}.dem"
        replay.write_bytes(b"incomplete")
        self.assertNotIn("replay", self.league.match_details(identity)["result"])
        replay.write_bytes(b"PBDEMS2\0" + b"replay")
        self.append(f"[VScript] [offline-league-ipc] abcd00000031 match id={identity}\n")
        status, archived = self.reply("abcd00000031")
        self.assertEqual(status, 200)
        self.assertEqual(archived["result"]["replay"], replay.stem)

    def test_file_settings_reach_the_next_roster_and_survive_service_restart(self):
        self.league = League(self.league.database, self.role_file)
        self.start_monitor()
        self.append("[offline-league-ipc] abcd00000020 settings bot_difficulty=4&micro=80&macro=60&coach=100\n")
        status, saved = self.reply("abcd00000020")
        self.assertEqual(status, 200)
        self.assertEqual(saved["tuning"], {"bot_difficulty": 4, "micro": 80, "macro": 60, "coach": 100})
        match = self.league.queue(2)["match"]
        document = self.role_file.read_text(encoding="ascii")
        self.assertIn("tuning = {bot_difficulty = 4, micro = 80, macro = 60, coach = 100}", document)
        League(self.league.database, self.role_file)
        self.assertEqual(self.role_file.read_text(encoding="ascii"), document)
        self.assertEqual(match["tuning"], saved["tuning"])

    def test_skip_stale_and_malformed_lines_and_follow_log_truncation(self):
        self.append("[offline-league-ipc] abcd00000004 queue role=2\n")
        self.start_monitor()
        self.append("[offline-league-ipc] ../notvalid queue role=2\n")
        self.append("[offline-league-ipc] abcd00000005 other role=2\n")
        self.append("[offline-league-ipc] abcd00000006 queue role=2&role=3\n")
        self.assertEqual(self.reply("abcd00000006")[0], 400)
        self.assertFalse((self.replies / "abcd00000004.txt").exists())
        self.assertFalse((self.replies / "abcd00000005.txt").exists())

        # Replace the contents within one polling interval and regrow beyond the previous file size.
        self.log.write_bytes(b"new log\n" * 128)
        self.append("[some prefix] [offline-league-ipc] abcd00000007 state")
        sleep(0.25)
        self.assertFalse((self.replies / "abcd00000007.txt").exists())
        self.append(" -\n")
        status, state = self.reply("abcd00000007")
        self.assertEqual(status, 200)
        self.assertNotIn("active_match", state)


if __name__ == "__main__":
    unittest.main()
