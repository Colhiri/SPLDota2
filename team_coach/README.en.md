<p align="center">
  <kbd><strong>English</strong></kbd> &nbsp; · &nbsp; <a href="README.md">Русский</a>
</p>

[Project home](../README.md) · [Gameplay](../docs/gameplay.md) · [League setup](../offline_league/README.en.md)

# Team coaches

Each coach reads the latest observations available to its own team and can submit structured plans for the bots to execute. Open Hyper AI continues to handle each hero's actions. The server validates each plan against the current match, eligible bots and targets, and the order's expiry.

## Choose a coaching mode

| Mode | How to use it |
| --- | --- |
| `observe` | The default league mode. Start [Играть.cmd](../Играть.cmd), then send the [master prompt](../README.md#master-prompt-for-both-coaches) to connect two visible chats, one for each team. The local API also accepts manual orders. |
| `rules` | Run `.\Играть.cmd -CoachMode rules`. Automatic local rules coach both sides and handle allied player calls. |
| `model` | Run `.\Играть.cmd -CoachMode model -Model <model-id>` with a configured CLI and an available model. The launcher starts background coaching sessions for both teams. |
| `shadow` | For telemetry-only diagnostics, run the standalone coach with `--mode shadow`. It records events and disables manual orders. |

The league endpoints listen on the game PC. For chats on that machine, each coach needs access to the project folder and its assigned endpoint. In `observe`, a ready server and a connected coach chat are separate steps.

## Check the connections

| Side | Status URL | Log filename |
| --- | --- | --- |
| Radiant | `http://127.0.0.1:32125/status` | `coach-events.jsonl` |
| Dire | `http://127.0.0.1:32126/status` | `dire-coach-events.jsonl` |

From the repository root, check each side and its current status.

```powershell
python -X utf8 -m team_coach.control --port 32125 status
python -X utf8 -m team_coach.control --port 32126 status
```

The logs are in `%LOCALAPPDATA%\Dota Offline League`. Use the log and port for the team you are following.

```powershell
python -X utf8 -m team_coach.watch "$env:LOCALAPPDATA\Dota Offline League\coach-events.jsonl" --port 32125 --follow
python -X utf8 -m team_coach.watch "$env:LOCALAPPDATA\Dota Offline League\dire-coach-events.jsonl" --port 32126 --follow
```

Before the match, each coach waits for fresh telemetry. Once the match starts, check the side and current `session_id` before sending orders. Review acceptance and in-game execution in each team's log. The live command catalog lists available actions and conditions.

```powershell
python -X utf8 -m team_coach.control --port 32125 list
python -X utf8 -m team_coach.control --port 32125 playbook
```

## Use a coach in a standalone lobby

Run `python -m team_coach --install` from the repository root. In Dota, create a lobby with **Local Host**, enable bot fill, and select **Local Dev Script** on Radiant. Choose regular bots for the other team. To coach Dire, run `python -m team_coach --install --team dire` and select **Local Dev Script** for Dire.

For subsequent games using the same side and port, run `python -m team_coach`. Rerun installation when the side, port, or adapter changes. Run `python -m team_coach --help` for path overrides and runtime options.
