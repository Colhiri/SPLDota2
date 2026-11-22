"""Choose a brief, feasible team objective from the current Lua reports."""

from math import hypot


_ACTION_TTL_S = 30
_SIGHTING_AGE_S = 10


def _matches(plan, objective, lane):
    return plan.get("objective") == objective and plan.get("lane") == lane


def base_threat(lane):
    hp = lane.get("own_tower_hp")
    pressure = lane["enemy_near_own"]
    return (
        lane["own_tower"] >= 3 and pressure > 0
        and (pressure >= 2 or lane.get("enemy_wave_near_own", 0) > 0 or hp is not None and hp <= 0.55)
    )


def base_under_attack(context):
    return context.get("ancient_threat", False) or any(base_threat(lane) for lane in context["lanes"].values())


def _responders(allies, lane, objective, board, previous, hint, *, critical, pressure):
    target_mode = f"{objective}_{lane}"
    minimum_hp = 0.70 if objective == "push" else 0.65
    tower_hp = board.get("own_tower_hp")
    can_teleport = objective == "defend" and (tower_hp is None or tower_hp > 0.14)
    prior_ids = previous.get("bot_ids", ()) if _matches(previous, objective, lane) else ()
    hinted_ids = hint.get("bot_ids", ()) if _matches(hint, objective, lane) else ()

    def native_desire(bot):
        return bot.get("desires", {}).get(target_mode)

    tower = board.get("own_tower_pos" if objective == "defend" else "enemy_tower_pos")

    def reachable(bot):
        if not pressure or not tower:
            return bot["zone"] == lane or can_teleport and bot["tp_ready"]
        if "fight" not in bot:
            return False
        distance = hypot(*(bot["fight"][axis] - tower[axis] for axis in ("x", "y")))
        return distance <= (2300 if objective == "defend" else 1800) or (
            can_teleport and bot["tp_ready"] and not bot.get("under_fire") and bot.get("nearby_enemies") == 0
        )

    def priority(bot):
        # Same-lane reports are a coarse distance proxy. Teleports only count toward defense.
        nearby = bot["zone"] == lane
        preserve_carry = (1.2 if critical else 3.0) if objective == "defend" else -1.0
        return (
            2.0 * nearby + 0.8 * (bot["mode"] == target_mode)
            + 0.8 * (native_desire(bot) or 0)
            + 0.45 * (bot["id"] in prior_ids) + 0.15 * (bot["id"] in hinted_ids)
            + 0.35 * (bot["role"] in {2, 3}) + bot["hp"]
            - preserve_carry * (bot["role"] == 1)
        )

    return sorted(
        (
            bot for bot in allies
            if bot["alive"] and bot["hp"] >= minimum_hp and bot["mode"] != "retreat"
            and reachable(bot)
            and (critical or bot["mode"] != "roshan")
            and (bot["zone"] == lane or bot["mode"] != "attack")
            and (pressure and objective == "defend" and tower is not None or native_desire(bot) != 0)
        ),
        key=priority,
        reverse=True,
    )


def _lane_risk(context, lane, known_near_tower):
    sighted = [
        enemy for enemy in context["enemies"]
        if enemy["alive"] and enemy.get("seen_s") is not None and enemy["seen_s"] <= _SIGHTING_AGE_S
    ]
    on_lane = sum(enemy["zone"] == lane for enemy in sighted)
    fog = 5 - len(sighted) - sum(not enemy["alive"] for enemy in context["enemies"])
    # A sighting on the lane only raises risk by one. It is coarser than a tower count.
    return max(known_near_tower, min(on_lane, known_near_tower + 1)), fog


def _defense(context, lane, board, responders, critical, *, pressure):
    tower = board["own_tower"]
    attackers_near_tower = board["enemy_near_own"]
    wave = board.get("enemy_wave_near_own")
    hp = board.get("own_tower_hp")
    damaged = hp is not None and hp < 0.55
    if not tower or not attackers_near_tower or (wave == 0 and not damaged and not critical):
        return None

    attackers, fog = _lane_risk(context, lane, attackers_near_tower)
    if not responders:
        return None
    if len(responders) < attackers and not critical:
        # Keep a healthy hero close enough to cover the wave from the tower side.
        cover = next((bot for bot in responders if bot["zone"] == lane and bot["hp"] >= 0.75), None)
        if pressure and cover and board.get("own_tower_pos") and wave:
            return 1.5, [cover["id"]], f"Cover {lane} from behind T{tower} against superior numbers."
        return None
    if attackers > 1 and fog >= 2 and len(responders) == attackers and not critical:
        return None

    count = min(3, len(responders), attackers + (pressure and len(responders) > 1))
    ids = [bot["id"] for bot in responders[:count]]
    score = 2.0 + 1.2 * tower + 0.8 * attackers + bool(wave) + 1.4 * damaged + 6.0 * critical
    score -= 0.4 * sum(bot["zone"] != lane for bot in responders[:count])
    return score, ids, f"Defend T{tower} {lane} against {attackers} likely attackers with {count} reachable bots."


def _push(context, lane, board, responders):
    tower = board["enemy_tower"]
    wave = board.get("ally_wave_near_enemy", 0)
    if not tower or not wave:
        return None

    defenders, fog = _lane_risk(context, lane, board.get("enemy_near_enemy", 0))
    nearby = min(board["ally_near_enemy"], len(responders))
    reserve = 1 if fog >= 2 and tower >= 2 else 0
    if nearby <= defenders + reserve or (fog >= 3 and nearby < 2) or (tower >= 3 and nearby < 2):
        return None

    objective_window = context.get("objective_window", False)
    count = min(3, nearby, 1 + defenders + (tower >= 3) + objective_window)
    ids = [bot["id"] for bot in responders[:count]]
    hp = board.get("enemy_tower_hp")
    finishable = hp is not None and hp < 0.45
    score = 2.0 + 0.6 * tower + 1.2 * (nearby - defenders) + 1.7 * finishable - 0.4 * fog + 2 * objective_window
    return score, ids, f"Push T{tower} {lane} with an allied wave and {nearby} nearby healthy bots."


def decide(context, *, pressure=False):
    """Return one short plan or hold when the map shows no favorable call."""
    previous = context.get("previous_plan") or {}
    hint = context.get("strategy_hint") or {}
    allies = {bot["id"]: bot for bot in context["allies"]}
    defending_base = base_under_attack(context)
    best_score = float("-inf")
    best = {
        "objective": "hold", "lane": "none", "bot_ids": [], "ttl_s": 0,
        "reason": "No favorable push or defense with the current reports.",
    }

    for choice in context["choices"]:
        objective, lane = choice["objective"], choice["lane"]
        board = context["lanes"][lane]
        critical = objective == "defend" and base_threat(board)
        if objective == "push" and defending_base:
            continue
        eligible = [allies[bot_id] for bot_id in choice["bot_ids"] if bot_id in allies]
        responders = _responders(eligible, lane, objective, board, previous, hint, critical=critical, pressure=pressure)
        candidate = (
            _defense(context, lane, board, responders, critical, pressure=pressure)
            if objective == "defend" else _push(context, lane, board, responders)
        )
        if candidate is None:
            continue
        score, ids, reason = candidate
        coach_strength = context.get("tuning", {}).get("coach", 100) / 100
        score += 0.55 * _matches(previous, objective, lane) + 1.5 * coach_strength * _matches(hint, objective, lane)
        if score > best_score:
            best_score = score
            best = {"objective": objective, "lane": lane, "bot_ids": ids, "ttl_s": _ACTION_TTL_S, "reason": reason}
    return best
