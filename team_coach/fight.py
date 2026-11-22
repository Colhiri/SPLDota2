"""Plan short fights from positions and consecutive sightings."""

from dataclasses import dataclass, replace
from math import hypot
from uuid import uuid4

from .policy import base_under_attack


TACTICS = ("pickoff", "flank", "bait", "counterattack", "gank")
SIGHT_TTL = 3.2
TACTIC_TTL = 18
GANK_TTL = 24
GANK_START = 240
GANK_ROTATION_RANGE = 2600
TEAMFIGHT_ULTIMATES = {
    "enigma_black_hole", "tidehunter_ravage", "faceless_void_chronosphere",
    "magnataur_reverse_polarity", "earthshaker_echo_slam", "warlock_rain_of_chaos",
    "winter_wyvern_winters_curse", "mars_arena_of_blood", "phoenix_supernova",
    "disruptor_static_storm", "treant_overgrowth", "elder_titan_earth_splitter",
}


def _distance(a, b):
    return hypot(a["x"] - b["x"], a["y"] - b["y"])


def _cross_angle(anchor, other, target):
    distance = _distance(anchor, target) * _distance(other, target)
    if distance < 1:
        return False
    dot = (
        (anchor["x"] - target["x"]) * (other["x"] - target["x"])
        + (anchor["y"] - target["y"]) * (other["y"] - target["y"])
    )
    return dot <= distance * 0.78


def visible_enemies(reports, clock):
    latest = {}
    for _, sampled_at, bot in reports:
        if clock - sampled_at > SIGHT_TTL or not bot["alive"] or "fight" not in bot:
            continue
        for enemy in bot["fight"]["enemies"]:
            previous = latest.get(enemy["id"])
            if previous is None or sampled_at > previous["seen_clock_s"]:
                latest[enemy["id"]] = {**enemy, "seen_clock_s": sampled_at}
    return [latest[enemy_id] for enemy_id in sorted(latest)]


def _busy(bot):
    return bot["mode"] in ("retreat", "roshan") or bot["mode"].startswith(("push_", "defend_"))


def _responders(context, target, reserved, *, gank=False):
    return [
        bot for bot in context["allies"]
        if bot["id"] not in reserved and bot["alive"] and bot["hp"] >= 0.65 and not _busy(bot)
        and "fight" in bot and _distance(bot["fight"], target) <= (GANK_ROTATION_RANGE if gank else 1700)
    ]


def _enemy_backup(target, enemies):
    return sum(enemy["id"] != target["id"] and _distance(target, enemy) <= 1100 for enemy in enemies)


def _exposed(target, enemies):
    levels = [enemy["level"] for enemy in enemies if "level" in enemy]
    low_level = "level" in target and levels and max(levels) - target["level"] >= 3
    return target["hp"] <= 0.70 or target.get("support") is True or low_level


def _baitable(target):
    return target.get("bkb_ready") is True or any(
        spell["name"] in TEAMFIGHT_ULTIMATES and spell["cooldown_s"] <= 0.25
        for spell in target.get("ultimate_cooldowns", ())
    )


def _gank_choice(context, target, nearby, backup):
    now = context["clock_s"]
    if now < GANK_START or target.get("under_tower") is not False:
        return
    core_levels = [bot["level"] for bot in context["allies"] if bot["role"] in (1, 2) and "level" in bot]
    level_lead = max(core_levels, default=target["level"]) + 2 <= target["level"]
    kills = target.get("kills", 0)
    dangerous = level_lead or (kills >= 4 and kills - target.get("deaths", kills) >= 3)
    if target.get("role") not in (1, 2) and not dangerous:
        return

    ready = [
        bot for bot in nearby if bot["hp"] >= 0.72 and bot.get("mana", 1) >= 0.25
        and (now >= 600 or bot["role"] != 1)
    ]
    anchor = next((bot for bot in ready if _distance(bot["fight"], target) <= 1350), None)
    if not anchor:
        return
    # Bring a third hero against a fed target or a nearby countergank.
    needed = 3 if dangerous or backup else 2
    if backup > 1 or len(ready) < needed:
        return
    rotations = [bot for bot in ready if bot != anchor and not bot.get("under_fire") and bot["mode"] != "attack"]
    rotations.sort(key=lambda bot: (bot["role"] not in (4, 5), _distance(bot["fight"], target), bot["id"]))
    if len(rotations) < needed - 1:
        return
    team = [anchor, *rotations[:needed - 1]]
    # At least one ally should be close enough to follow the opener.
    if all(_distance(bot["fight"], target) > 2000 for bot in team[1:]):
        return
    return {
        "tactic": "gank", "target_id": target["id"], "bot_ids": [bot["id"] for bot in team],
        "anchor_id": anchor["id"], "signal": "snowballing_core" if dangerous else "exposed_core",
        "priority_target": dangerous,
    }


@dataclass(frozen=True, slots=True)
class FightPlan:
    id: str
    tactic: str
    phase: str
    target_id: int
    bot_ids: tuple[int, ...]
    anchor_id: int
    expires_clock_s: float
    phase_clock_s: float
    source: str = "local"
    priority_target: bool = False
    opener: tuple[int, str] | None = None

    def wire(self):
        return {
            "id": self.id, "tactic": self.tactic, "phase": self.phase, "target_id": self.target_id,
            "bot_ids": list(self.bot_ids), "anchor_id": self.anchor_id, "expires_clock_s": self.expires_clock_s,
            "source": self.source, **({"priority_target": True} if self.priority_target else {}),
            **({"opener": {"bot_id": self.opener[0], "ability": self.opener[1]}} if self.opener else {}),
        }

    def command(self):
        return {
            "id": self.id, "action": self.tactic, "target": {"kind": "enemy", "id": self.target_id},
            "bot_ids": list(self.bot_ids), "expires_clock_s": self.expires_clock_s, "phase": self.phase,
            "source": self.source,
            **({"opener": {"bot_id": self.opener[0], "ability": self.opener[1]}} if self.opener else {}),
        }


class FightPlanner:
    def __init__(self, record, *, gank_enabled=False):
        self.record = record
        self.gank_enabled = gank_enabled
        self.plan = None
        self.applied = set()
        self.ready = {}
        self.sightings = {}
        self.windows = {}
        self.next_auto = 0
        self.next_gank = {}
        self.last_success = None
        self.last_resolution = None
        self.opener_applied = None

    def observe(self, context):
        now = context["clock_s"]
        targets = context.get("fight_enemies", ())
        visible = {target["id"] for target in targets}
        self.sightings = {enemy_id: data for enemy_id, data in self.sightings.items() if enemy_id in visible}
        self.windows = {
            enemy_id: window for enemy_id, window in self.windows.items()
            if enemy_id in visible and now - window[0] <= 7
        }
        for target in targets:
            enemy_id, seen = target["id"], target["seen_clock_s"]
            previous = self.sightings.get(enemy_id)
            if previous and seen <= previous["seen_clock_s"]:
                continue
            self.sightings[enemy_id] = target
            if not previous or seen - previous["seen_clock_s"] > SIGHT_TTL:
                continue
            if previous.get("bkb") is True and target.get("bkb") is False:
                self.windows[enemy_id] = (seen, "bkb_ended")
            old_cooldowns = {
                spell["name"]: spell["cooldown_s"] for spell in previous.get("ultimate_cooldowns", ())
            }
            for spell in target.get("ultimate_cooldowns", ()):
                before = old_cooldowns.get(spell["name"])
                if before is not None and before <= 0.25 and spell["cooldown_s"] >= 15:
                    self.windows[enemy_id] = (seen, spell["name"])
                    break

    def choices(self, context, reserved=(), *, bot_ids=None):
        if context is None or context["clock_s"] < 0 or base_under_attack(context):
            return []
        enemies = context.get("fight_enemies", ())
        candidates = []
        for target in enemies:
            if target.get("bkb") or target.get("ultimate"):
                continue
            backup = _enemy_backup(target, enemies)
            if self.gank_enabled and "level" in target:
                gank_bots = _responders(context, target, reserved, gank=True)
                if bot_ids is not None:
                    gank_bots = [bot for bot in gank_bots if bot["id"] in bot_ids]
                gank_bots.sort(key=lambda bot: (_distance(bot["fight"], target), bot["id"]))
                gank = _gank_choice(context, target, gank_bots, backup)
                if gank and (bot_ids is None or set(gank["bot_ids"]) == set(bot_ids)):
                    candidates.append((6 if gank["priority_target"] else 4.8, gank))
            nearby = _responders(context, target, reserved)
            if bot_ids is not None:
                eligible = {bot["id"]: bot for bot in nearby}
                if any(bot_id not in eligible for bot_id in bot_ids):
                    continue
                nearby = [eligible[bot_id] for bot_id in bot_ids]
            if len(nearby) < 2:
                continue
            nearby.sort(key=lambda bot: (_distance(bot["fight"], target), bot["id"]))
            team = nearby[:3]
            if backup >= len(team) - 1:
                continue
            tactical_options = []
            window = self.windows.get(target["id"])
            if window and context["clock_s"] - window[0] <= 7:
                if sum(_distance(bot["fight"], target) <= 1000 for bot in team) >= 2:
                    tactical_options.append((5, "counterattack", team[0], window[1]))
            if _exposed(target, enemies) and backup == 0:
                hint = target.get("support") is True
                tactical_options.append((4 + 0.5 * (1 - target["hp"]) + 0.2 * hint,
                                         "pickoff", team[0], "support_archetype" if hint else "exposed"))
            if len(team) == 3:
                tactical_options.append((3, "flank", team[0], "three_nearby"))
            anchor = next((
                bot for bot in nearby if bot["hp"] >= 0.82 and bot["role"] in (2, 3)
                and _distance(bot["fight"], target) <= 1150
            ), None)
            if anchor and _baitable(target):
                tactical_options.append((3.4, "bait", anchor, "defensive_resource_ready"))
            for score, tactic, lead, signal in tactical_options:
                ids = [bot["id"] for bot in team]
                if lead["id"] not in ids:
                    ids[-1] = lead["id"]
                candidates.append((score, {
                    "tactic": tactic, "target_id": target["id"], "bot_ids": ids,
                    "anchor_id": lead["id"], "signal": signal,
                    "commit_now": tactic == "pickoff" and sum(
                        _distance(bot["fight"], target) <= 900 for bot in team
                    ) >= 2,
                }))
        return [choice for _, choice in sorted(candidates, key=lambda entry: -entry[0])]

    def start(self, choice, clock, expires, *, source, reason):
        self.end(clock, "replaced")
        phase = "commit" if choice["tactic"] == "counterattack" or choice.get("commit_now") else "stage"
        self.plan = FightPlan(
            uuid4().hex[:12], choice["tactic"], phase, choice["target_id"],
            tuple(choice["bot_ids"]), choice["anchor_id"], expires, clock, source, choice.get("priority_target", False),
            choice.get("opener"),
        )
        self.opener_applied = None
        self.record("fight_planned", clock_s=clock, fight=self.plan.wire(),
                    evidence=choice["signal"], source=source, reason=reason)
        return self.plan.command()

    def end(self, clock, reason):
        if self.plan is not None:
            self.last_resolution = (self.plan, clock, reason)
            if reason == "target_down" and self.plan.phase == "commit" and self.applied:
                self.last_success = clock
            self.record("fight_ended", clock_s=clock, fight=self.plan.wire(), reason=reason)
            if clock is not None:
                self.next_auto = clock + 8
                if self.plan.tactic == "gank":
                    self.next_gank[self.plan.target_id] = clock + 35
            self.plan = None
            self.applied.clear()
            self.ready.clear()

    def accept_report(self, bot, report, clock, *, ready=False):
        plan = self.plan
        bot_id = bot["id"]
        accepted = (
            plan and bot["alive"] and bot_id in plan.bot_ids
            and plan.expires_clock_s > clock and isinstance(report, dict)
            and report.get("id") == plan.id and report.get("phase") == plan.phase
        )
        if ready:
            already_ready = bot_id in self.ready
            if accepted:
                self.ready[bot_id] = clock
                if not already_ready:
                    self.record("fight_ready", clock_s=clock, fight_id=plan.id, phase=plan.phase, bot_id=bot_id)
            else:
                self.ready.pop(bot_id, None)
        elif accepted and bot_id not in self.applied:
            self.applied.add(bot_id)
            self.record("fight_applied", clock_s=clock, fight_id=plan.id, phase=plan.phase, bot_id=bot_id)

    def _advance(self, clock, phase, signal):
        self.plan = replace(self.plan, phase=phase, phase_clock_s=clock)
        self.applied.clear()
        self.ready.clear()
        self.record("fight_phase", clock_s=clock, fight=self.plan.wire(), evidence=signal)

    def tick(self, context, reserved):
        plan = self.plan
        if plan is None:
            return
        if context is None:
            self.end(None, "telemetry_missing")
            return
        now = context["clock_s"]
        self.ready = {bot_id: clock for bot_id, clock in self.ready.items() if now - clock <= SIGHT_TTL}
        if now >= plan.expires_clock_s:
            self.end(now, "expired")
            return
        if base_under_attack(context) or reserved.intersection(plan.bot_ids):
            self.end(now, "defense_or_retreat")
            return
        enemies = context.get("fight_enemies", ())
        target = next((enemy for enemy in enemies if enemy["id"] == plan.target_id), None)
        if target is None:
            defeated = any(enemy["id"] == plan.target_id and not enemy["alive"] for enemy in context["enemies"])
            self.end(now, "target_down" if defeated else "target_not_visible")
            return
        gank = plan.tactic == "gank"
        ready = {bot["id"]: bot for bot in _responders(context, target, reserved, gank=gank)}
        if any(bot_id not in ready for bot_id in plan.bot_ids) or _enemy_backup(target, enemies) >= len(plan.bot_ids) - 1:
            self.end(now, "numbers_or_health_changed")
            return
        if gank and target.get("under_tower") is not False:
            self.end(now, "target_under_tower")
            return
        elapsed = now - plan.phase_clock_s
        near_target = [ready[bot_id] for bot_id in plan.bot_ids if _distance(ready[bot_id]["fight"], target) <= 1000]
        anchor = ready[plan.anchor_id]
        exposed = target.get("bkb") is True or bool(target.get("ultimate"))
        window = self.windows.get(plan.target_id)
        confirmed = window and 0 <= now - window[0] <= 7 and window[0] >= plan.phase_clock_s - SIGHT_TTL

        if plan.phase == "stage":
            if plan.opener and plan.opener[1] not in ready[plan.opener[0]].get("tactical_openers", ()):
                self.end(now, "opener_unavailable")
                return
            staged = [bot for bot in near_target if bot["id"] in self.ready]
            lead_ready = any(bot["id"] == plan.anchor_id for bot in staged)
            if exposed:
                self._advance(now, "reset", "enemy_defensive_resource_active")
            elif elapsed > (12 if gank else 7):
                self.end(now, "setup_timed_out")
            elif elapsed >= 1.5 and lead_ready and len(staged) >= (len(plan.bot_ids) if gank else 2):
                if plan.tactic == "bait":
                    self._advance(now, "probe", "allies_in_position")
                elif plan.tactic != "flank" or any(
                    bot["id"] != plan.anchor_id and _cross_angle(anchor["fight"], bot["fight"], target)
                    for bot in staged
                ):
                    self._advance(now, "commit", "allies_in_position")
        elif plan.phase == "probe":
            if exposed or confirmed:
                self._advance(now, "reset", window[1] if confirmed else "enemy_defensive_resource_active")
            elif elapsed > 6:
                self.end(now, "no_observed_reaction")
        elif plan.phase == "reset":
            close_enough = sum(_distance(ready[bot_id]["fight"], target) <= 1250 for bot_id in plan.bot_ids)
            if not exposed and confirmed and elapsed >= 1.5 and close_enough >= 2:
                self._advance(now, "commit", window[1])
            elif elapsed > 11:
                self.end(now, "no_safe_reentry")
        elif exposed:
            self._advance(now, "reset", "enemy_defensive_resource_active")
