<p align="center">
  <kbd><strong>English</strong></kbd> &nbsp; · &nbsp; <a href="README.md">Русский</a>
</p>

[Project home](../README.md) · [Gameplay](../docs/gameplay.md) · [Coach setup](../team_coach/README.en.md)

# League setup and troubleshooting

## Install and launch

Install Dota 2 through Steam, subscribe to Open Hyper AI in the Workshop, and install Python 3.11 or later. Close Dota and double-click [Играть.cmd](../Играть.cmd) in the repository root. The launcher installs the add-on and bot adapter, starts the league service and both coach servers, and opens the league in Dota. Repeat this after updating the repository to load changed scripts.

The default coach mode is `observe`. The server status at the bottom of the menu confirms that each local endpoint is ready. Send the [master prompt](../README.md#master-prompt-for-both-coaches) to connect the two visible coach chats, or start the league with `.\Играть.cmd -CoachMode rules` for automatic coaching.

## Enter from the Dota menu

1. Run `.\Играть.cmd -FromDotaMenu` in PowerShell. This starts the services and opens Dota's main menu.
2. Open the game console and enter `offline_league`.
3. To use a shortcut, enter `bind F8 offline_league` after the alias has loaded.

For repeated launches from Steam, prepare the add-on and services from the repository root. Run this after an update and again if the services have stopped.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\offline_league\Launch-League.ps1 -Action Prepare
```

Set the following Dota properties in Steam's launch options, then enter `offline_league` in the game console or use the bound key.

```text
-console -condebug -con_logfile offline_league.log -offline_league_file_ipc +exec offline_league.cfg
```

## Menus, difficulty, and saves

- **Сложность** (Difficulty) controls bot difficulty, micro, macro, and coaching strength for both teams. Settings apply to the next queue. Micro affects additional lane, spell, and item decisions. Macro controls the strength of automatic team plans. Coaching strength controls how often coaches can reconsider a plan.
- **Вернуться в лигу** (Return to League) closes the saved result screen and returns to your profile. During a draft or an active game, the top-left back arrow asks for confirmation and cancels the unfinished match without a rating change.
- **История матчей** (Match History) opens saved match details. **Смотреть повтор** (Watch Replay) appears when the local Dota `.dem` is available. Use Dota's replay exit control, then enter `offline_league` to return. Earlier matches display the stats that were saved at the time.
- **Меню Dota** (Dota Menu) exits the league itself.

The local profile and history live in `%LOCALAPPDATA%\Dota Offline League\league.sqlite3`. The league uses Dota's local replay files for playback. Keep both the database and the available `.dem` files if you move your installation.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Steam, Dota, or the bot scripts are missing | Run the launcher with `-Action Check`. Make sure Steam has downloaded Open Hyper AI. If needed, supply absolute paths with `-SteamPath`, `-DotaPath`, and `-WorkshopPath`. |
| The console command is unavailable | Start with `-FromDotaMenu`, or make sure the Steam launch options load `offline_league.cfg`. |
| The profile or history does not load | Check `http://127.0.0.1:32157/health` and the local `server.log` and `server.err.log` in the profile folder. |
| The coach services are ready but the chats show no match | Start the two chats with the master prompt. Match telemetry arrives after the game starts. [Check each coach connection](../team_coach/README.en.md#check-the-connections). |
| Watch Replay is missing | The button appears when Dota's match recording becomes available. The match row still opens any saved stats. |

`-Action Install` installs the add-on and bot adapter. `-Action Check` checks the detected paths and packaged UI. The packaged Panorama resources are included in the repository and checked against their manifest when installed.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\offline_league\Launch-League.ps1 -Action Check
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\offline_league\Launch-League.ps1 -Action Install
Invoke-RestMethod http://127.0.0.1:32157/health
```
