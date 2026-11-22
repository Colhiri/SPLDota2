<p align="center">
  <kbd><strong>English</strong></kbd> &nbsp; · &nbsp; <a href="README.ru.md">Русский</a>
</p>

# SPLDota2 — Single Player League

A local Dota 2 league for Windows. Pick your role, draft any available hero, and play a full 5v5 with four allied bots and five opponents. Radiant and Dire can each have their own coach. The league keeps your rating, scoreboards, and available replays in one place.

**[Gameplay and bot behavior](docs/gameplay.md)** · **[Setup and troubleshooting](offline_league/README.en.md)** · **[Coach setup](team_coach/README.en.md)**

## Start playing

You need Windows, Steam with Dota 2 installed, Python **3.11 or later**, and a subscription to [Open Hyper AI in Steam Workshop](https://steamcommunity.com/sharedfiles/filedetails/?id=3246316298). Let Steam finish downloading its bot scripts.

1. Close Dota and double-click [Играть.cmd](Играть.cmd). The launcher installs or updates the add-on and bot adapter, starts the league and both coach services, then opens the league inside Dota.
2. For live coaching in two visible chats, send the [master prompt](#master-prompt-for-both-coaches) once in a local project chat. Wait until both coaches are listening for the next match. For automatic coaching with local rules, use `.\Играть.cmd -CoachMode rules` instead.
3. Set your difficulty, choose a position, and select **Найти матч** (Find Match). Pick your hero in Dota's hero selector.
4. After the match, review the saved scoreboard and choose **Вернуться в лигу** (Return to League). Open a match in **История матчей** (Match History) for the full scoreboard and replay when the recording is available.

To enter from Dota's main menu, run `.\Играть.cmd -FromDotaMenu` and enter `spldota2` in the game console. [Setup and menu entry](offline_league/README.en.md#enter-from-the-dota-menu)

## Master prompt for both coaches

Use a project chat on the machine running the league. The project needs local terminal access and the ability to create or resume two visible chats.

```text
Set up both SPLDota2 coaches in two separate visible chats in this project.
Create or resume “Radiant Coach” and “Dire Coach”. Use this repository as the
working directory for both chats.

Radiant Coach reads team_coach/player-team-session.txt and connects to
127.0.0.1:32125. Its log is %LOCALAPPDATA%\Dota Offline League\coach-events.jsonl.
Dire Coach reads team_coach/opponent-team-session.txt and connects to
127.0.0.1:32126. Its log is %LOCALAPPDATA%\Dota Offline League\dire-coach-events.jsonl.

In each chat, verify the team and observe mode via /status. Resolve the log path
and use python -X utf8 -m team_coach.watch "<log path>" --port <team port> --follow
to wait for a fresh match session_id. Coach only the assigned team using its
telemetry and the current coaching settings. Stay connected through the end
of the match and summarize acknowledged orders, their observed outcomes, and
any changes of plan.

Confirm readiness here after both chats are waiting for a match.
```

Try `!push top`, `!push mid`, `!push bot`, `!rosh`, or `!cancel` in the allied team chat. [Player calls and tactical examples](docs/gameplay.md#what-the-bots-can-do)

## Run the checks

From the repository root, run the Python tests and use Node.js for the result screen checks.

```powershell
python -B -m unittest discover -s offline_league -p 'test_league_server.py'
python -B -m unittest discover -s tests -p 'test_team_coach*.py'
node offline_league/test_league_result.js
```
