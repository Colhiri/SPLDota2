"""Stable offline bot profiles and sparse, match-driven relationship history."""

from functools import lru_cache
from itertools import combinations
import json
import random


RADIANT, DIRE = 2, 3
ROLES = (1, 2, 3, 4, 5)
BOT_TIERS = (700, 1125, 1550, 2150, 2950)
BOT_COUNT = 10_000
SIMULATED_MATCHES_PER_RESULT = 8
PLAYER_ID = "@you"
BOT_NAMES = (
    "Aster", "Borey", "Vortex", "Granit", "Druid",
    "Eger", "Zefir", "Iskra", "Kobalt", "Lynx",
    "Mirage", "Nord", "Onyx", "Pulsar", "Runa",
    "Sable", "Tayga", "Umbra", "Fakel", "Haze",
    "Tsirkon", "Shoroh", "Yantar", "Quasar", "Yava",
)
HERO_POOLS = {
    1: ("juggernaut", "sven", "drow_ranger", "luna", "skeleton_king"),
    2: ("zuus", "lina", "viper", "sniper", "death_prophet"),
    3: ("axe", "tidehunter", "bristleback", "centaur", "slardar"),
    4: ("mirana", "windrunner", "sand_king", "spirit_breaker", "lion"),
    5: ("crystal_maiden", "witch_doctor", "dazzle", "omniknight", "ogre_magi"),
}
NICKNAME_ENDINGS = (
    "Akra", "Bay", "Coda", "Dawn", "Echo", "Fen", "Gale", "Halo", "Inlet", "Jade",
    "Kite", "Lime", "Moss", "Nova", "Owl", "Peak", "Quartz", "Reef", "Silk", "Tern",
)
PERSONALITIES = ("Спокойный", "Капитан", "Азартный", "Остряк", "Упрямый")
RELATION_LABELS = {"ally": "Союзник", "rival": "Соперник", "familiar": "Знакомый"}

UPDATES = """
    INSERT INTO social_edges (
        a, b, with_games, with_wins, against_games, a_wins, simulated,
        last_match
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (a, b) DO UPDATE SET
        with_games = with_games + excluded.with_games,
        with_wins = with_wins + excluded.with_wins,
        against_games = against_games + excluded.against_games,
        a_wins = a_wins + excluded.a_wins,
        simulated = simulated + excluded.simulated,
        last_match = excluded.last_match
"""


@lru_cache(maxsize=1)
def roster() -> dict[str, dict]:
    players = {}
    for role in ROLES:
        legacy_count = len(BOT_TIERS) * len(HERO_POOLS[role])
        for index in range(BOT_COUNT // len(ROLES)):
            rng = random.Random(f"league-bot-{role}-{index}")
            if index < legacy_count:
                difficulty, first_hero = divmod(index, len(HERO_POOLS[role]))
                identity = f"bot-{role}-{difficulty}-{first_hero}"
                name = f"{BOT_NAMES[(role - 1) * 5 + first_hero]}.{difficulty + 1}"
                deviation = (first_hero - 2) * 48 + (role - 3) * 12
                preferred = [HERO_POOLS[role][first_hero]]
                preferred.extend(rng.sample([hero for hero in HERO_POOLS[role] if hero not in preferred], 2))
            else:
                pool_index = (role - 1) * (BOT_COUNT // len(ROLES)) + index
                identity = f"bot-{pool_index + 1:05d}"
                group, nickname = divmod((pool_index * 8627 + 3941) % BOT_COUNT, len(BOT_NAMES))
                number, ending = divmod(group, len(NICKNAME_ENDINGS))
                name = f"{BOT_NAMES[nickname]}.{NICKNAME_ENDINGS[ending]}{number + 1}"
                difficulty = rng.randrange(len(BOT_TIERS))
                deviation = rng.randrange(-150, 151)
                preferred = rng.sample(HERO_POOLS[role], 3)
            players[identity] = {
                "bot_id": identity,
                "name": name,
                "role": role,
                "hero": f"npc_dota_hero_{preferred[0]}",
                "hero_pool": tuple(f"npc_dota_hero_{hero}" for hero in preferred),
                "difficulty": difficulty,
                "rating": BOT_TIERS[difficulty] + deviation,
                "personality": rng.choice(PERSONALITIES),
                "is_player": False,
            }
    return players


@lru_cache(maxsize=None)
def get_bot_candidates(role: int) -> tuple[dict, ...]:
    return tuple(bot for bot in roster().values() if bot["role"] == role)


def initialize(db) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS social_edges (
            a TEXT NOT NULL,
            b TEXT NOT NULL,
            with_games INTEGER NOT NULL,
            with_wins INTEGER NOT NULL,
            against_games INTEGER NOT NULL,
            a_wins INTEGER NOT NULL,
            simulated INTEGER NOT NULL,
            last_match TEXT NOT NULL,
            PRIMARY KEY (a, b),
            CHECK (a < b)
        ) WITHOUT ROWID
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS social_world (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            simulated_matches INTEGER NOT NULL DEFAULT 0
        )
    """)
    if db.execute("INSERT OR IGNORE INTO social_world (id) VALUES (1)").rowcount:
        prior_matches = db.execute("""
            SELECT lineup, result, completed_at FROM matches WHERE status = 'complete'
            ORDER BY completed_at, rowid
        """)
        for match in prior_matches:
            lineup = json.loads(match["lineup"])
            result = json.loads(match["result"])
            members = result.get("participants") or lineup["participants"]
            record_pairs(db, factions(members), result["winner"], match["completed_at"] or "")


def factions(members) -> dict[str, int]:
    return {
        PLAYER_ID if member["is_player"] else member["bot_id"]: member["team"]
        for member in members
        if member["is_player"] or member.get("bot_id") in roster()
    }


def record_pairs(db, teams: dict[str, int], winner: int, at: str, *, simulated: bool = False) -> None:
    db.executemany(UPDATES, (
        (
            first, second, int(teams[first] == teams[second]),
            int(teams[first] == teams[second] == winner),
            int(teams[first] != teams[second]),
            int(teams[first] != teams[second] and teams[first] == winner),
            int(simulated), at,
        )
        for first, second in combinations(sorted(teams), 2)
    ))


def relation_score(edge) -> int:
    with_wins = edge["with_wins"]
    against = edge["against_games"]
    close_series = min(edge["a_wins"], against - edge["a_wins"])
    return max(-100, min(100, 7 * with_wins - 3 * (edge["with_games"] - with_wins) - 5 * against - 2 * close_series))


def relation_kind(edge) -> str:
    score = relation_score(edge)
    if edge["with_games"] >= 2 and score >= 8:
        return "ally"
    if edge["against_games"] >= 2 and score <= -9:
        return "rival"
    return "familiar"


def player_edges(db, limit: int = 80) -> dict:
    rows = db.execute("""
        SELECT * FROM social_edges WHERE a = ?
        ORDER BY last_match DESC, with_games + against_games DESC, b LIMIT ?
    """, (PLAYER_ID, limit))
    return {edge["b"]: edge for edge in rows if edge["b"] in roster()}


def bot_edges(db, bot_ids) -> dict:
    ids = sorted(set(bot_ids))
    if len(ids) < 2:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = db.execute(f"""
        SELECT * FROM social_edges WHERE a IN ({placeholders}) AND b IN ({placeholders})
    """, (*ids, *ids))
    return {(edge["a"], edge["b"]): edge for edge in rows}


def compatible_pairs(db, returning: dict) -> list:
    """Keep eligible existing alliances or rivalries together for part of the search."""
    eligible = {}
    for slot, bots in returning.items():
        for bot in bots:
            eligible.setdefault(bot["bot_id"], []).append((slot, bot))
    candidates = []
    for pair, edge in bot_edges(db, eligible).items():
        kind = relation_kind(edge)
        if kind == "familiar":
            continue
        for first in eligible[pair[0]]:
            for second in eligible[pair[1]]:
                if first[0] == second[0]:
                    continue
                if (first[0][0] == second[0][0]) == (kind == "ally"):
                    candidates.append((abs(relation_score(edge)), (first, second)))
    candidates.sort(key=lambda item: -item[0])
    return [pair for _, pair in candidates[:18]]


def summary(db) -> dict:
    profiles = roster()
    remembered = player_edges(db, 32)
    closest = sorted(remembered.values(), key=lambda edge: (
        relation_kind(edge) == "familiar", -abs(relation_score(edge)), -(edge["with_games"] + edge["against_games"]),
    ))[:6]
    contacts = []
    for edge in closest:
        bot = profiles[edge["b"]]
        contacts.append({
            "name": bot["name"],
            "bot_id": bot["bot_id"],
            "kind": relation_kind(edge),
            "score": relation_score(edge),
            "played": edge["with_games"] + edge["against_games"],
            "with_games": edge["with_games"],
            "against_games": edge["against_games"],
            "personality": bot["personality"],
            "hero": bot["hero"],
        })
    strong_pairs = sorted(bot_edges(db, remembered).values(), key=lambda edge: (
        relation_kind(edge) == "familiar", -abs(relation_score(edge)), -(edge["with_games"] + edge["against_games"]),
    ))[:3]
    bot_ties = [
        {
            "first_name": profiles[edge["a"]]["name"],
            "second_name": profiles[edge["b"]]["name"],
            "kind": relation_kind(edge),
            "played": edge["with_games"] + edge["against_games"],
            "simulated_games": edge["simulated"],
        }
        for edge in strong_pairs
    ]
    return {
        "population": BOT_COUNT,
        "simulated_matches": db.execute("SELECT simulated_matches FROM social_world WHERE id = 1").fetchone()[0],
        "contacts": contacts,
        "bot_ties": bot_ties,
    }


def chance(radiant_rating: int, dire_rating: int) -> float:
    return 1 / (1 + 10 ** ((dire_rating - radiant_rating) / 2000))


def simulate_world(db, match_id: str, recent: dict[str, int], at: str) -> None:
    rng = random.Random(match_id + "/world")
    profiles = roster()
    recent_by_role = {
        role: [profiles[identity] for identity in recent if identity in profiles and profiles[identity]["role"] == role]
        for role in ROLES
    }
    for index in range(SIMULATED_MATCHES_PER_RESULT):
        teams = {}
        strength = {RADIANT: 0, DIRE: 0}
        for role in ROLES:
            lineup = list(recent_by_role[role]) if index < 4 else []
            rng.shuffle(lineup)
            for team in (RADIANT, DIRE):
                if lineup:
                    bot = lineup.pop()
                else:
                    bot = rng.choice(get_bot_candidates(role))
                    while bot["bot_id"] in teams:
                        bot = rng.choice(get_bot_candidates(role))
                teams[bot["bot_id"]] = team
                strength[team] += bot["rating"]
        winner = RADIANT if rng.random() < chance(strength[RADIANT], strength[DIRE]) else DIRE
        record_pairs(db, teams, winner, at, simulated=True)
    db.execute("""
        UPDATE social_world SET simulated_matches = simulated_matches + ? WHERE id = 1
    """, (SIMULATED_MATCHES_PER_RESULT,))


def complete(db, match: dict, winner: int, at: str) -> None:
    teams = factions(match["participants"])
    record_pairs(db, teams, winner, at)
    simulate_world(db, match["id"], teams, at)


def social_line(bot: dict, kind: str | None, effort: int, edge) -> str:
    if kind == "rival" and bot["team"] == DIRE:
        if edge["a_wins"] > edge["against_games"] - edge["a_wins"]:
            return "У тебя пока перевес. Пора брать реванш."
        return "Снова ты? Посмотрим, чем закончится на этот раз."
    if kind == "ally" and bot["team"] == RADIANT:
        return "Мы уже выигрывали вместе. Давай повторим."
    if effort:
        return "Этот матч хочется выиграть. Соберёмся."
    if bot["personality"] == "Остряк":
        return "За руну придётся поспорить."
    if kind == "familiar":
        return "О, знакомое лицо."
    return ""


def prepare_lineup(lineup: list[dict], player_history: dict, known_pairs: dict) -> list[dict]:
    """Apply the intensity actually used by matchmaking, then expose the strongest shared memories."""
    bots = lineup[1:]
    paired = []
    motivated = {}
    for first, second in combinations(bots, 2):
        key = tuple(sorted((first["bot_id"], second["bot_id"])))
        edge = known_pairs.get(key)
        if edge is None:
            continue
        same_team = first["team"] == second["team"]
        kind = relation_kind(edge)
        if (kind == "ally" and same_team) or (kind == "rival" and not same_team):
            weight = abs(relation_score(edge))
            paired.append((weight, first["slot"], second["slot"], kind))
            for bot in (first, second):
                motivated[bot["slot"]] = max(weight, motivated.get(bot["slot"], 0))

    for bot in bots:
        edge = player_history.get(bot["bot_id"])
        kind = relation_kind(edge) if edge else None
        bot["relationship"] = RELATION_LABELS.get(kind, "")
        bot["effort"] = 0
        if (kind == "ally" and bot["team"] == RADIANT) or (kind == "rival" and bot["team"] == DIRE):
            slot = bot["slot"]
            motivated[slot] = max(abs(relation_score(edge)) + 10, motivated.get(slot, 0))

    for team in (RADIANT, DIRE):
        ready = sorted(
            (bot for bot in bots if bot["team"] == team and bot["difficulty"] < len(BOT_TIERS) - 1
             and bot["slot"] in motivated),
            key=lambda bot: -motivated[bot["slot"]],
        )
        for bot in ready[:2]:
            previous = bot["difficulty"]
            bot["difficulty"] = previous + 1
            bot["rating"] += BOT_TIERS[previous + 1] - BOT_TIERS[previous]
            bot["effort"] = 1

    for bot in bots:
        edge = player_history.get(bot["bot_id"])
        kind = relation_kind(edge) if edge else None
        line = social_line(bot, kind, bot["effort"], edge)
        if line:
            bot["social_line"] = line

    return [
        {"left": left, "right": right, "kind": kind}
        for _, left, right, kind in sorted(paired, reverse=True)[:3]
    ]
