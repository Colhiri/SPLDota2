"""Local profile, matchmaking, and match history for Dota Offline League."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
import re
import sqlite3
from tempfile import NamedTemporaryFile
from threading import Event, Thread
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

import league_social as social
from league_social import DIRE, HERO_POOLS, RADIANT, ROLES


HOST = "127.0.0.1"
PORT = 32157
CALIBRATION_MATCHES = 5
CALIBRATION_OFFSETS = (0, 340, -260, 520, -400)
START_RATING = 1500
HISTORY_LIMIT = 30
HUMAN_SLOT = 0
BODY_LIMIT = 16_384
HERO_NAME = re.compile(r"npc_dota_hero_[a-z0-9_]{1,50}\Z")
MATCH_ID = re.compile(r"[a-f0-9]{32}\Z")
IPC_REQUEST = re.compile(
    rb"\[offline-league-ipc\][ \t]+([a-f0-9_]{10,64})[ \t]+(state|settings|queue|draft|finish|cancel)"
    rb"(?:[ \t]+([!-~]+))?[ \t]*\Z",
)
IPC_PART_BYTES = 256
IPC_POLL_SECONDS = 0.1
LOG_READ_BYTES = 65_536
DEFAULT_TUNING = {"bot_difficulty": 4, "micro": 100, "macro": 100, "coach": 100}

MEDALS = (
    (4100, "Титан"),
    (3450, "Божество"),
    (2900, "Властелин"),
    (2350, "Легенда"),
    (1800, "Герой"),
    (1250, "Рыцарь"),
    (800, "Страж"),
    (0, "Рекрут"),
)
STAT_FIELDS = ("kills", "deaths", "assists", "gpm", "xpm", "last_hits", "denies")


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def integer(value, label: str, low: int, high: int) -> int:
    if type(value) is str and re.fullmatch(r"[0-9]{1,9}", value):
        value = int(value)
    if type(value) is not int or not low <= value <= high:
        raise RequestError(400, f"Некорректное значение {label}")
    return value


def tuning_values(values: dict | None = None) -> dict:
    values = values or {}
    return {
        key: integer(values.get(key, default), key, 0, 4 if key == "bot_difficulty" else 100)
        for key, default in DEFAULT_TUNING.items()
    }


def match_id(value) -> str:
    if not isinstance(value, str) or MATCH_ID.fullmatch(value) is None:
        raise RequestError(400, "Некорректный ID матча")
    return value


def hero_name(value) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or HERO_NAME.fullmatch(value) is None:
        raise RequestError(400, "Некорректное имя героя")
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def query_payload(query: str) -> dict:
    pairs = parse_qsl(query, keep_blank_values=True, max_num_fields=48)
    payload = dict(pairs)
    if len(payload) != len(pairs):
        raise RequestError(400, "Параметры запроса дублируются")
    return payload


def response_bytes(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def request_result(action) -> tuple[int, dict]:
    try:
        return 200, action()
    except RequestError as error:
        return error.status, {"error": str(error)}
    except (ValueError, RecursionError):
        return 400, {"error": "Некорректный запрос"}
    except sqlite3.OperationalError as error:
        print(f"Database error: {error}", flush=True)
        return 503, {"error": "База профиля временно недоступна"}


def match_lineup(identifier: str, preferred_role: int, profile: dict, db) -> dict:
    rng = random.Random(identifier)
    player_role = preferred_role or rng.choice(ROLES)
    rating = profile["rating"]
    target = rating
    if not profile["calibrated"]:
        target += CALIBRATION_OFFSETS[profile["calibration_played"]]

    human = {
        "slot": HUMAN_SLOT,
        "team": RADIANT,
        "role": player_role,
        "name": "Ты",
        "hero": "",
        "difficulty": 0,
        "rating": rating,
        "bot_id": "",
        "is_player": True,
    }
    slots = [
        (side, role)
        for side in (RADIANT, DIRE)
        for role in ROLES
        if side != RADIANT or role != player_role
    ]
    profiles = social.roster()
    contacts = social.player_edges(db)
    pools = {}
    flex = {}
    for role in ROLES:
        ordered = sorted(social.get_bot_candidates(role), key=lambda bot: abs(bot["rating"] - target))
        pools[role] = ordered[:50]
        main_tier = ordered[0]["difficulty"]
        flex[role] = [
            bot for bot in ordered if abs(bot["difficulty"] - main_tier) == 1
            and abs(bot["rating"] - target) <= abs(ordered[0]["rating"] - target) + 700
        ][:24]

    returning = {}
    for side, role in slots:
        nearest = abs(pools[role][0]["rating"] - target)
        eligible = [
            profiles[identity] for identity, edge in contacts.items()
            if profiles[identity]["role"] == role
            and abs(profiles[identity]["rating"] - target) <= nearest + 390
            and (social.relation_kind(edge) != "ally" or side == RADIANT)
            and (social.relation_kind(edge) != "rival" or side == DIRE)
        ]
        def priority(bot):
            edge = contacts[bot["bot_id"]]
            played = edge["with_games"] + edge["against_games"]
            return (-min(played, 4), abs(bot["rating"] - target))
        returning[side, role] = sorted(eligible, key=priority)[:6]

    returning_pairs = social.compatible_pairs(db, returning)
    returning_slots = [slot for slot in slots if returning[slot]]
    candidates = []
    for attempt in range(80):
        chosen = set()
        ordered_slots = slots.copy()
        # Keep the team's slots contiguous for the in-game bot assignment.
        rng.shuffle(ordered_slots)
        ordered_slots.sort(key=lambda slot: slot[0])
        selected_pairs = returning_pairs and attempt % 4 != 0
        if selected_pairs:
            reserved = dict(rng.choice(returning_pairs))
        else:
            reserved_slots = rng.sample(returning_slots, min(len(returning_slots), rng.choice((1, 2, 2, 3))))
            reserved = {slot: rng.choice(returning[slot]) for slot in reserved_slots}
        draft = {}
        occupied_heroes = set()
        for side, role in sorted(ordered_slots, key=lambda slot: slot not in reserved):
            player = reserved.get((side, role))
            if player is None or player["bot_id"] in chosen:
                pool = flex[role] if flex[role] and attempt % 4 != 0 and rng.random() < 0.15 else pools[role]
                player = rng.choice([bot for bot in pool if bot["bot_id"] not in chosen])
            hero = next(hero for hero in player["hero_pool"] if hero not in occupied_heroes)
            occupied_heroes.add(hero)
            chosen.add(player["bot_id"])
            draft[side, role] = (player, hero)
        lineup = [human.copy()]
        for side, role in ordered_slots:
            member, hero = draft[side, role]
            lineup.append({
                **{key: value for key, value in member.items() if key != "hero_pool"},
                "hero": hero,
                "team": side,
                "slot": len(lineup),
            })
        candidates.append(lineup)

    known_pairs = social.bot_edges(
        db, (bot["bot_id"] for candidate in candidates for bot in candidate[1:]),
    )
    scored = []
    for lineup in candidates:
        pairs = social.prepare_lineup(lineup, contacts, known_pairs)
        strengths = Counter()
        for participant in lineup:
            strengths[participant["team"]] += participant["rating"]
        difference = (strengths[RADIANT] - strengths[DIRE]) / 5
        skill_distance = sum(abs(bot["rating"] - target) for bot in lineup[1:]) / 9
        familiar = sum(bot["bot_id"] in contacts for bot in lineup[1:])
        rematch_cost = max(0, familiar - 3) * 30
        efforts = sum(bot["effort"] for bot in lineup[1:])
        strong_contacts = sum(
            bot["relationship"] in ("Союзник", "Соперник") for bot in lineup[1:]
        )
        social_value = 15 * min(efforts, 2) + 13 * min(strong_contacts, 2) + 28 * min(len(pairs), 2)
        score = abs(difference) + skill_distance * 0.15 + rematch_cost - social_value
        scored.append((score, lineup, pairs, strengths))

    _, lineup, pairs, strengths = min(scored, key=lambda candidate: candidate[0])
    radiant_difficulty = sorted(bot["difficulty"] for bot in lineup[1:5])[2]
    dire_difficulty = sorted(bot["difficulty"] for bot in lineup[5:])[2]
    return {
        "id": identifier,
        "player_team": RADIANT,
        "player_slot": HUMAN_SLOT,
        "role": player_role,
        "expected_win": round(social.chance(strengths[RADIANT], strengths[DIRE]), 4),
        "bot_difficulty": sorted(bot["difficulty"] for bot in lineup[1:])[4],
        "team_difficulties": {"radiant": radiant_difficulty, "dire": dire_difficulty},
        "participants": lineup,
        "social_pairs": pairs,
    }


def draft_lineup(match: dict, selected_hero: str) -> None:
    """Reserve the human's hero while preserving the bot and its relationships."""
    occupied = {member["hero"] for member in match["participants"] if not member["is_player"]}
    for member in match["participants"]:
        if member["is_player"]:
            member["hero"] = selected_hero
        elif member["hero"] == selected_hero:
            role_pool = tuple(f"npc_dota_hero_{hero}" for hero in HERO_POOLS[member["role"]])
            profile = social.roster().get(member.get("bot_id"), {})
            choices = (*profile.get("hero_pool", ()), *role_pool)
            replacement = next((hero for hero in choices if hero not in occupied), None)
            if replacement is None:
                raise RequestError(409, "Не удалось подобрать второго героя на роль бота")
            member["hero"] = replacement
            occupied.add(replacement)


def role_document(match: dict | None) -> str:
    if match is None:
        return "return nil\n"
    lines = [f'return {{ id = "{match_id(match["id"])}", human_role = {integer(match["role"], "role", 1, 5)},']
    tuning = tuning_values(match.get("tuning"))
    lines.append("    tuning = {" + ", ".join(f"{key} = {value}" for key, value in tuning.items()) + "},")
    for team, label in ((RADIANT, "radiant"), (DIRE, "dire")):
        lines.append(f"    {label} = {{")
        members = sorted(
            (member for member in match["participants"] if member["team"] == team),
            key=lambda member: member["slot"],
        )
        for member in members:
            role = integer(member["role"], "role", 1, 5)
            hero = hero_name(member["hero"]) if not member["is_player"] else None
            choice = f', hero = "{hero}"' if hero else ""
            lines.append(f"        {{ role = {role}{choice} }},")
        lines.append("    },")
    lines.append("}\n")
    return "\n".join(lines)


def participant_stats(payload: dict) -> dict[int, dict]:
    provided = payload.get("stats", [])
    if isinstance(provided, str):
        rows = [row.split(",") for row in provided.split(";") if row]
        columns = ("slot", "hero", *STAT_FIELDS)
        if any(not 2 <= len(row) <= len(columns) for row in rows):
            raise RequestError(400, "Некорректная статистика участников")
        provided = [dict(zip(columns, row)) for row in rows]
    if not isinstance(provided, list) or len(provided) > 10:
        raise RequestError(400, "Некорректная статистика участников")
    stats = {}
    human = {
        key: payload[key] for key in ("hero", *STAT_FIELDS)
        if key in payload and payload[key] is not None and payload[key] != ""
    }
    for row in provided:
        if not isinstance(row, dict):
            raise RequestError(400, "Некорректная статистика участника")
        slot = integer(row.get("slot"), "slot", 0, 9)
        if slot in stats:
            raise RequestError(400, "Статистика слота дублируется")
        stats[slot] = {"hero": hero_name(row.get("hero"))}
        for stat in STAT_FIELDS:
            value = row.get(stat)
            if value is not None and value != "":
                stats[slot][stat] = integer(value, stat, 0, 1_000_000)
    if human:
        row = stats.setdefault(HUMAN_SLOT, {})
        if "hero" in human:
            row["hero"] = hero_name(human.pop("hero"))
        for stat, value in human.items():
            row[stat] = integer(value, stat, 0, 1_000_000)
    return stats


class League:
    def __init__(self, database: Path, role_file: Path | None = None):
        self.database = database
        self.role_file = role_file
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    rating INTEGER NOT NULL,
                    wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS matches (
                    id TEXT PRIMARY KEY,
                    queued_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('queued', 'complete', 'cancelled')),
                    lineup TEXT NOT NULL,
                    completed_at TEXT,
                    result TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_match
                ON matches(status) WHERE status = 'queued';
                CREATE INDEX IF NOT EXISTS completed_match ON matches(completed_at DESC);
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    tuning TEXT NOT NULL
                );
            """)
            db.execute("INSERT OR IGNORE INTO profile (id, rating) VALUES (1, ?)", (START_RATING,))
            db.execute("INSERT OR IGNORE INTO settings (id, tuning) VALUES (1, ?)", (json.dumps(DEFAULT_TUNING),))
        with self.connection(write=True) as db:
            social.initialize(db)
        with self.connection() as db:
            pending = self.pending(db)
        self.publish_roles(pending)

    def publish_roles(self, match: dict | None):
        if self.role_file is None or not self.role_file.parent.is_dir():
            return
        temporary = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="ascii", newline="\n", delete=False, dir=self.role_file.parent,
                prefix=".offline-league-roles-", suffix=".tmp",
            ) as stream:
                temporary = Path(stream.name)
                stream.write(role_document(match))
            temporary.replace(self.role_file)
        except OSError as error:
            print(f"Role file error: {error}", flush=True)
            raise RequestError(503, "Не удалось передать роли ботам. Попробуй ещё раз.") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def clear_roles(self):
        try:
            self.publish_roles(None)
        except RequestError:
            # The result and profile still need to be saved if the game holds the file open.
            pass

    @contextmanager
    def connection(self, *, write: bool = False):
        db = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def profile(db: sqlite3.Connection) -> dict:
        profile = dict(db.execute("SELECT rating, wins, losses FROM profile WHERE id = 1").fetchone())
        played = min(CALIBRATION_MATCHES, profile["wins"] + profile["losses"])
        calibrated = played >= CALIBRATION_MATCHES
        medal = next(name for minimum, name in MEDALS if profile["rating"] >= minimum)
        return {
            **profile,
            "medal": medal if calibrated else "Калибровка",
            "calibrated": calibrated,
            "calibration_played": played,
            "calibration_required": CALIBRATION_MATCHES,
        }

    @staticmethod
    def pending(db: sqlite3.Connection) -> dict | None:
        row = db.execute("SELECT lineup FROM matches WHERE status = 'queued'").fetchone()
        return json.loads(row["lineup"]) if row else None

    @staticmethod
    def tuning(db: sqlite3.Connection) -> dict:
        row = db.execute("SELECT tuning FROM settings WHERE id = 1").fetchone()
        return tuning_values(json.loads(row["tuning"]) if row else None)

    def settings(self, payload: dict) -> dict:
        unknown = set(payload) - DEFAULT_TUNING.keys()
        if unknown:
            raise RequestError(400, "Неизвестная настройка игры")
        with self.connection(write=True) as db:
            tuning = tuning_values({**self.tuning(db), **payload})
            db.execute("UPDATE settings SET tuning = ? WHERE id = 1", (json.dumps(tuning),))
            return self.state(db)

    def state(self, db: sqlite3.Connection, offset: int = 0) -> dict:
        history = db.execute("""
            SELECT result FROM matches WHERE status = 'complete'
            ORDER BY completed_at DESC, rowid DESC LIMIT ? OFFSET ?
        """, (HISTORY_LIMIT, offset))
        summary = []
        for row in history:
            match = json.loads(row["result"])
            match.pop("participants", None)
            summary.append(match)
        result = {
            "profile": self.profile(db), "history": summary, "social": social.summary(db),
            "tuning": self.tuning(db),
        }
        active_match = self.pending(db)
        if active_match is not None:
            result["active_match"] = active_match
        return result

    def match_details(self, identity: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT result FROM matches WHERE id = ? AND status = 'complete'", (identity,),
            ).fetchone()
            if row is None:
                raise RequestError(404, "Завершённый матч не найден")
            return {"result": json.loads(row["result"])}

    def queue(self, preferred_role: int) -> dict:
        with self.connection(write=True) as db:
            profile = self.profile(db)
            pending = self.pending(db)
            if pending is not None:
                if preferred_role not in (0, pending["role"]):
                    raise RequestError(409, "Сначала отмени предыдущий незавершённый матч")
                self.publish_roles(pending)
                return {"match": pending, "profile": profile}

            identity = uuid4().hex
            match = match_lineup(identity, preferred_role, profile, db)
            match["tuning"] = self.tuning(db)
            match["bot_difficulty"] = match["tuning"]["bot_difficulty"]
            match["team_difficulties"] = {
                "radiant": match["bot_difficulty"], "dire": match["bot_difficulty"],
            }
            db.execute(
                "INSERT INTO matches (id, queued_at, status, lineup) VALUES (?, ?, 'queued', ?)",
                (identity, utc_now(), json.dumps(match, ensure_ascii=False)),
            )
            self.publish_roles(match)
            return {"match": match, "profile": profile}

    def draft(self, identity: str, selected_hero: str) -> dict:
        with self.connection(write=True) as db:
            match = self.pending(db)
            if match is None or match["id"] != identity:
                raise RequestError(409, "Матч уже закончился или отменён")
            draft_lineup(match, selected_hero)
            db.execute("UPDATE matches SET lineup = ? WHERE id = ?", (json.dumps(match, ensure_ascii=False), identity))
            self.publish_roles(match)
            return {"match": match}

    def finish(self, payload: dict) -> dict:
        identity = match_id(payload.get("id"))
        winner = integer(payload.get("winner"), "winner", RADIANT, DIRE)
        duration = integer(payload.get("duration", 0), "duration", 0, 86_400)
        statistics = participant_stats(payload)
        scores = {
            key: integer(payload[key], key, 0, 1_000_000)
            for key in ("radiant_score", "dire_score") if payload.get(key) not in (None, "")
        }

        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM matches WHERE id = ?", (identity,)).fetchone()
            if row is None:
                raise RequestError(404, "Матч не найден")
            if row["status"] == "cancelled":
                raise RequestError(409, "Матч был отменён")
            if row["status"] == "complete":
                result = json.loads(row["result"])
                if result["winner"] != winner:
                    raise RequestError(409, "Другой результат матча уже сохранён")
                return {**self.state(db), "result": result}

            match = json.loads(row["lineup"])
            profile = self.profile(db)
            before = profile["rating"]
            won = winner == match["player_team"]
            coefficient = 44 if profile["calibrated"] else 112 - 12 * profile["calibration_played"]
            after = max(0, min(8000, before + round(coefficient * (int(won) - match["expected_win"]))))
            completed = utc_now()
            player = statistics.get(HUMAN_SLOT, {})
            participants = []
            for member in match["participants"]:
                actual = statistics.get(member["slot"], {})
                participants.append({
                    **member,
                    "planned_hero": member["hero"],
                    "hero": actual.get("hero"),
                    **{stat: actual.get(stat) for stat in STAT_FIELDS},
                })
            result = {
                "id": identity,
                "completed_at": completed,
                "winner": winner,
                "won": won,
                "rating_before": before,
                "rating_after": after,
                "rating_delta": after - before,
                "duration_seconds": duration,
                "role": match["role"],
                "tuning": tuning_values(match.get("tuning")),
                "hero": player.get("hero"),
                "kills": player.get("kills"),
                "deaths": player.get("deaths"),
                "assists": player.get("assists"),
                "participants": participants,
                **scores,
            }
            db.execute(
                "UPDATE profile SET rating = ?, wins = wins + ?, losses = losses + ? WHERE id = 1",
                (after, int(won), int(not won)),
            )
            db.execute(
                "UPDATE matches SET status = 'complete', completed_at = ?, result = ? WHERE id = ?",
                (completed, json.dumps(result, ensure_ascii=False), identity),
            )
            social.complete(db, match, winner, completed)
            self.clear_roles()
            return {**self.state(db), "result": result}

    def cancel(self, identity: str) -> dict:
        with self.connection(write=True) as db:
            row = db.execute("SELECT status FROM matches WHERE id = ?", (identity,)).fetchone()
            if row is None:
                raise RequestError(404, "Матч не найден")
            if row["status"] == "queued":
                db.execute("UPDATE matches SET status = 'cancelled' WHERE id = ?", (identity,))
                self.clear_roles()
            return self.state(db)

    def dispatch(self, operation: str, payload: dict) -> dict:
        if operation == "settings":
            return self.settings(payload)
        if operation == "state":
            offset = integer(payload.get("offset", 0), "offset", 0, 1_000_000)
            with self.connection() as db:
                db.execute("BEGIN")
                return self.state(db, offset)
        if operation == "queue":
            role = integer(payload.get("role", 0), "role", 0, 5)
            return self.queue(role)
        if operation == "match":
            return self.match_details(match_id(payload.get("id")))
        if operation == "draft":
            hero = hero_name(payload.get("hero"))
            if hero is None:
                raise RequestError(400, "Герой игрока не указан")
            return self.draft(match_id(payload.get("id")), hero)
        if operation == "finish":
            return self.finish(payload)
        if operation == "cancel":
            return self.cancel(match_id(payload.get("id")))
        raise RequestError(404, "Команда неизвестна")


class LeagueLogIPC:
    """Exchange requests and replies using the private local match log and addon directory."""

    def __init__(self, league: League, log: Path, directory: Path):
        self.league = league
        self.log = log
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.identity = None
        self.offset = 0
        self.anchor = b""
        self.partial = b""
        self.skip_line = False
        self.pending: dict[str, bytes] = {}
        self.stop_event = Event()
        self.thread = Thread(target=self.run, name="league-file-ipc", daemon=True)

    def start(self):
        try:
            with self.log.open("rb") as source:
                info = os.fstat(source.fileno())
                self.identity = info.st_dev, info.st_ino
                self.offset = source.seek(0, os.SEEK_END)
                self.anchor = self.read_anchor(source)
                self.skip_line = bool(self.anchor and not self.anchor.endswith(b"\n"))
        except FileNotFoundError:
            pass
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=10)

    @property
    def running(self) -> bool:
        return self.thread.is_alive() and not self.stop_event.is_set()

    def read_anchor(self, source) -> bytes:
        start = max(0, self.offset - 64)
        source.seek(start)
        return source.read(self.offset - start)

    def read_appended(self) -> bytes:
        try:
            with self.log.open("rb") as source:
                info = os.fstat(source.fileno())
                identity = info.st_dev, info.st_ino
                if identity != self.identity or info.st_size < self.offset or self.read_anchor(source) != self.anchor:
                    self.identity = identity
                    self.offset, self.anchor, self.partial, self.skip_line = 0, b"", b"", False
                source.seek(self.offset)
                appended = source.read(LOG_READ_BYTES)
                if appended:
                    self.offset = source.tell()
                    self.anchor = self.read_anchor(source)
                return appended
        except FileNotFoundError:
            return b""

    def run(self):
        last_error = None
        while not self.stop_event.is_set():
            try:
                self.poll()
                last_error = None
            except OSError as error:
                if str(error) != last_error:
                    print(f"File IPC error: {error}", flush=True)
                    last_error = str(error)
            self.stop_event.wait(IPC_POLL_SECONDS)

    def poll(self):
        appended = self.read_appended()
        if appended:
            pieces = (self.partial + appended).split(b"\n")
            self.partial = pieces.pop()
            for line in pieces:
                if self.skip_line:
                    self.skip_line = False
                else:
                    self.handle_line(line.rstrip(b"\r"))
            if self.skip_line or len(self.partial) > BODY_LIMIT * 2:
                self.partial = b""
                self.skip_line = True

        for identity in list(self.pending):
            reply = self.directory / f"{identity}.txt"
            temporary = self.directory / f".{identity}.tmp"
            temporary.write_bytes(self.pending[identity])
            temporary.replace(reply)
            del self.pending[identity]

    def handle_line(self, line: bytes):
        match = IPC_REQUEST.search(line)
        if match is None:
            return
        identifier, operation, raw_query = match.groups()
        if raw_query is not None and len(raw_query) > BODY_LIMIT:
            return
        identity = identifier.decode("ascii")
        if identity in self.pending or (self.directory / f"{identity}.txt").exists():
            return
        query = "" if raw_query in (None, b"-") else raw_query.decode("ascii")
        status, data = request_result(lambda: self.league.dispatch(operation.decode("ascii"), query_payload(query)))
        body = response_bytes(data)
        parts = ['"reply"', '{', f'\t"status" "{status}"']
        for number, offset in enumerate(range(0, len(body), IPC_PART_BYTES), 1):
            contents = body[offset:offset + IPC_PART_BYTES].hex()
            parts.append(f'\t"part_{number}" "{contents}"')
        parts.extend(('}', ''))
        self.pending[identity] = '\n'.join(parts).encode("ascii")


class LeagueHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, league: League, port: int = PORT, ipc: LeagueLogIPC | None = None):
        self.league = league
        self.ipc = ipc
        super().__init__((HOST, port), LeagueRequestHandler)


class LeagueRequestHandler(BaseHTTPRequestHandler):
    server: LeagueHTTPServer

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, format, *args):
        # Match data belongs in the profile database, not in an HTTP access log.
        pass

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        self.send_json(*request_result(self.route))

    def route(self) -> dict:
        host = self.headers.get("Host")
        port = self.server.server_port
        local_hosts = (f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost")
        if host not in local_hosts or "Origin" in self.headers or "Sec-Fetch-Site" in self.headers:
            raise RequestError(403, "Доступ открыт только локальной игре")

        if len(self.path) > BODY_LIMIT:
            raise RequestError(414, "Слишком длинный запрос")
        address = urlsplit(self.path)
        payload = self.payload(address.query)
        if address.path == "/health":
            return {
                "service": "dota_offline_league",
                "schema": 4,
                "file_ipc": self.server.ipc is not None and self.server.ipc.running,
            }
        return self.server.league.dispatch(address.path.removeprefix("/"), payload)

    def payload(self, query: str) -> dict:
        if self.command == "GET":
            return query_payload(query)
        if query or self.headers.get_content_type() != "application/json":
            raise RequestError(400, "Ожидается JSON без параметров в адресе")
        length = integer(self.headers.get("Content-Length"), "Content-Length", 1, BODY_LIMIT)
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise RequestError(400, "Ожидается JSON-объект")
        return data

    def send_json(self, status: int, data: dict):
        body = response_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def default_database() -> Path:
    user_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return user_data / "Dota Offline League" / "league.sqlite3"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_database())
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--ipc-log", type=Path)
    parser.add_argument("--ipc-dir", type=Path)
    parser.add_argument("--role-file", type=Path)
    args = parser.parse_args()
    if (args.ipc_log is None) != (args.ipc_dir is None):
        parser.error("--ipc-log and --ipc-dir must be supplied together")
    if args.ipc_log is not None and not (args.ipc_log.is_absolute() and args.ipc_dir.is_absolute()):
        parser.error("--ipc-log and --ipc-dir must be absolute paths")
    if args.role_file is not None and not args.role_file.is_absolute():
        parser.error("--role-file must be an absolute path")

    league = League(args.db, args.role_file)
    ipc = LeagueLogIPC(league, args.ipc_log, args.ipc_dir) if args.ipc_log is not None else None
    with LeagueHTTPServer(league, args.port, ipc) as server:
        if ipc is not None:
            ipc.start()
        print(f"Dota Offline League: http://{HOST}:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if ipc is not None:
                ipc.close()


if __name__ == "__main__":
    main()
