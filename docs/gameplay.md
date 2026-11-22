<p align="center">
  <kbd><strong>English</strong></kbd> &nbsp; · &nbsp; <a href="gameplay.ru.md">Русский</a>
</p>

[Project home](../README.md) · [Setup guide](../offline_league/README.en.md) · [Coach setup](../team_coach/README.en.md)

# Gameplay and bot behavior

Dota Offline League adds a league menu to the local Dota 2 client. You play for Radiant in a 5v5 with four allied bots and five Dire bots on the standard map. The league adds role-based matchmaking, a persistent local rating, two team coaches, and match history.

## How a match works

| Stage | What happens |
| --- | --- |
| Start | The launcher opens the league and starts its local services. The league menu shows whether each coach server is ready. In `observe` mode, the master prompt connects the two coach chats before the queue. |
| Queue and draft | Choose a position from 1 to 5. The league reserves it and fills the other nine slots. Pick any available hero in the Dota selector. If your choice overlaps a planned bot pick, the league gives that bot another hero for the same position. |
| Play | Open Hyper AI controls each bot. Each coach works from its own team's observations and can send plans as objectives or threats change. Your team chat can also send structured calls to the Radiant coach. |
| Finish | The league saves the outcome and rating change, then opens the postgame scoreboard. Review it and choose **Вернуться в лигу** (Return to League). |
| Review | Open the match row to see the result, duration, and stats for both teams. If Dota's local recording is available, **Смотреть повтор** (Watch Replay) opens it in Dota's replay viewer. |

The scoreboard includes kills, deaths, assists, last hits, denies, gold and XP per minute, net worth, hero damage, and healing. The league starts at 1500 rating and uses the first five completed matches for calibration. Your rating influences future lineups. Leaving an unfinished match preserves your current rating.

## What the bots can do

Open Hyper AI handles individual play such as laning, farming, movement, item use, and spellcasting. A coach can assign a team objective, split the bots into groups, or coordinate an initiation. Each side gets its own game feed. Plans are checked against current vision, available heroes and abilities, deadlines, and target eligibility.

| Situation | Example behavior |
| --- | --- |
| A tower is vulnerable | Ask a group to push a lane while the rest farm or hold another part of the map. Redirect the push if defenders arrive. |
| An enemy is isolated | Choose an available initiator and follow-up group for a pickoff. A separate group can pressure a tower during the fight. |
| A fight changes | Ask a frontline to bait out BKB or a major ultimate, then counterattack if the cooldown is observed and the team can still fight. Order a retreat when conditions for the plan fail. |
| The team has a window for Roshan | Route eligible bots to Roshan and plan which objective to take afterward. Change the next step if the window closes. |
| A big spell matters later | Ask an eligible bot to hold an ultimate for a planned engagement, with an expiry or an explicit cancellation. |

In allied team chat, `!push top`, `!push mid`, `!push bot`, and `!rosh` request a team objective. `!cancel` cancels your call. Player calls take priority within the currently feasible choices. The team keeps executing valid orders while a coach considers its next move. Each new telemetry update can also trigger a fallback from a coordinated plan. In the coach log you can check whether a call was accepted, executed, canceled, or expired.

## What you can practice

- **Specific roles and matchups.** Reserve the same position for several games, try different heroes, and adjust bot difficulty, micro, macro, and coaching strength from the league menu. Changes apply at the next queue.
- **Calls and team responses.** Practice timing a push, grouping after a pickoff, or pivoting to Roshan. Separate coaches allow each team to respond to what it can see.
- **Progress over several matches.** Use the local rating and match history to track results. The league's 10,000 persistent bot profiles give it a pool of recurring teammates and opponents. They can also appear in simulated bot-only matches.
- **Review decisions.** Compare both teams' stats and use the replay to revisit rotations, fights, and objective timing. In a coached match, the coach logs add the order, acknowledgments, and any fallback that fired.

The coach services listen only on the PC running the league. The setup guide covers coaching in visible chats, automatic local rules, and background coaching. [Coach modes and connection checks](../team_coach/README.en.md)
