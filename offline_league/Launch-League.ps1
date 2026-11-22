[CmdletBinding()]
param(
    [ValidateSet('Play', 'Prepare', 'Install', 'Server', 'Coaches', 'Tools', 'Check')]
    [string]$Action = 'Play',
    [string]$SteamPath,
    [string]$DotaPath,
    [string]$WorkshopPath,
    [ValidateSet('observe', 'rules', 'model')]
    [string]$CoachMode = 'observe',
    [ValidatePattern('^[a-zA-Z0-9_.-]+$')]
    [string]$Model,
    [switch]$FromDotaMenu
)

$ErrorActionPreference = 'Stop'
if (!(Get-Command Get-FileHash -ErrorAction SilentlyContinue)) {
    Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Utility/Microsoft.PowerShell.Utility.psd1')
}
$addonName = 'offline_league'
$addonSource = Join-Path $PSScriptRoot 'addon'
$serverScript = Join-Path $PSScriptRoot 'league_server.py'
$workspace = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $env:LOCALAPPDATA 'Dota Offline League'
$healthUrl = 'http://127.0.0.1:32157/health'
$serviceSchema = 5
$resourceTypes = @{'.xml' = '.vxml_c'; '.js' = '.vjs_c'; '.css' = '.vcss_c'}

function Get-DotaInstall {
    param([string]$SteamRoot, [string]$DotaRoot, [string]$WorkshopRoot)

    if (!$SteamRoot) {
        $SteamRoot = (Get-ItemProperty -LiteralPath 'HKCU:\Software\Valve\Steam' -Name SteamPath -ErrorAction SilentlyContinue).SteamPath
    }
    if (!$SteamRoot) {
        throw 'Steam was not found. Pass the Steam directory as -SteamPath.'
    }
    $steamExe = Join-Path $SteamRoot 'steam.exe'
    if (!(Test-Path -LiteralPath $steamExe -PathType Leaf)) {
        throw "Steam executable was not found in $SteamRoot."
    }

    $libraries = @($SteamRoot)
    $libraryConfig = Join-Path $SteamRoot 'steamapps\libraryfolders.vdf'
    if (Test-Path -LiteralPath $libraryConfig -PathType Leaf) {
        $libraries += Select-String -LiteralPath $libraryConfig -Pattern '"path"\s+"([^"]+)"' |
            ForEach-Object { $_.Matches[0].Groups[1].Value.Replace('\\', '\') }
    }
    if (!$DotaRoot) {
        foreach ($library in ($libraries | Select-Object -Unique)) {
            $candidate = Join-Path $library 'steamapps\common\dota 2 beta'
            if (Test-Path -LiteralPath (Join-Path $candidate 'game\bin\win64\dota2.exe') -PathType Leaf) {
                $DotaRoot = $candidate
                break
            }
        }
    }
    if (!$DotaRoot -or !(Test-Path -LiteralPath (Join-Path $DotaRoot 'game\bin\win64\dota2.exe') -PathType Leaf)) {
        throw 'Dota 2 was not found. Pass its install directory as -DotaPath.'
    }
    $DotaRoot = (Resolve-Path -LiteralPath $DotaRoot).Path
    if (!$WorkshopRoot) {
        $dotaLibrary = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $DotaRoot))
        foreach ($library in (@($dotaLibrary) + $libraries | Select-Object -Unique)) {
            if (!$library) { continue }
            $candidate = Join-Path $library 'steamapps\workshop\content\570\3246316298'
            if (Test-Path -LiteralPath (Join-Path $candidate 'ability_item_usage_generic.lua') -PathType Leaf) {
                $WorkshopRoot = $candidate
                break
            }
        }
    }
    if ($WorkshopRoot) { $WorkshopRoot = (Resolve-Path -LiteralPath $WorkshopRoot).Path }
    $gameRoot = Join-Path $DotaRoot 'game'
    [pscustomobject]@{
        Steam = $steamExe
        Workshop = $WorkshopRoot
        Game = $gameRoot
        Runtime = Join-Path $gameRoot "dota_addons\$addonName"
        Content = Join-Path $DotaRoot "content\dota_addons\$addonName"
        Compiler = Join-Path $gameRoot 'bin\win64\resourcecompiler.exe'
        ConsoleLog = Join-Path $gameRoot 'dota\offline_league.log'
        IpcDirectory = Join-Path $gameRoot "dota_addons\$addonName\scripts\league_ipc"
        RoleFile = Join-Path $gameRoot 'dota\scripts\vscripts\bots\team_coach_league_roles.lua'
    }
}

function Copy-LeagueAddon {
    param($Install)

    $addonInfo = Join-Path $addonSource 'addoninfo.txt'
    if (!(Test-Path -LiteralPath $addonInfo -PathType Leaf)) {
        throw 'The addon source directory is incomplete.'
    }
    New-Item -ItemType Directory -Path $Install.Runtime -Force | Out-Null
    New-Item -ItemType Directory -Path $Install.Content -Force | Out-Null
    Get-ChildItem -LiteralPath $addonSource | Where-Object Name -ne 'panorama' |
        Copy-Item -Destination $Install.Runtime -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $addonSource 'panorama') -Destination $Install.Content -Recurse -Force
    Copy-Item -LiteralPath $addonInfo -Destination $Install.Content -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'offline_league.cfg') -Destination (Join-Path $Install.Game 'dota/cfg') -Force
}

function Get-LeagueResources {
    param($Install)

    $sourcePanorama = Join-Path $addonSource 'panorama'
    Get-ChildItem -LiteralPath $sourcePanorama -File -Recurse | ForEach-Object {
        if ($resourceTypes.ContainsKey($_.Extension)) {
            $relative = $_.FullName.Substring($addonSource.Length + 1)
            $builtRelative = [IO.Path]::ChangeExtension($relative, $resourceTypes[$_.Extension])
            $built = Join-Path $Install.Runtime $builtRelative
            $isCurrent = (Test-Path -LiteralPath $built -PathType Leaf) -and
                ((Get-Item -LiteralPath $built).LastWriteTimeUtc -ge $_.LastWriteTimeUtc)
            [pscustomobject]@{
                Source = Join-Path $Install.Content $relative
                Built = $built
                Current = $isCurrent
            }
        }
    }
}

function Get-LeaguePackage {
    param($Install, $Resources)

    $buildRoot = Join-Path $PSScriptRoot 'build'
    $manifestPath = Join-Path $buildRoot 'panorama-manifest.json'
    if (!(Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        return $null
    }
    $package = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if (@($package.source.PSObject.Properties).Count -ne $Resources.Count) {
        return $null
    }
    foreach ($resource in $Resources) {
        $relative = $resource.Source.Substring($Install.Content.Length + 1).Replace('\', '/')
        $hash = $package.source.PSObject.Properties[$relative].Value
        if (!$hash -or (Get-FileHash -LiteralPath (Join-Path $addonSource $relative)).Hash -ne $hash) {
            return $null
        }
    }
    $compiled = @($package.resources.PSObject.Properties)
    if (!$compiled.Count) {
        return $null
    }
    foreach ($item in $compiled) {
        if ($item.Name -notmatch '^panorama/(layout|scripts|styles)/[a-z0-9_./-]+\.(vxml|vjs|vcss)_c$' -or $item.Name -match '(^|/)\.\.(/|$)') {
            throw 'Unexpected path in the compiled Panorama package.'
        }
        $file = Join-Path $buildRoot $item.Name
        if (!(Test-Path -LiteralPath $file -PathType Leaf) -or (Get-FileHash -LiteralPath $file).Hash -ne $item.Value) {
            return $null
        }
    }
    return $compiled
}

function Install-LeaguePackage {
    param($Install, $Package)

    foreach ($item in $Package) {
        $destination = Join-Path $Install.Runtime $item.Name
        if ((Test-Path -LiteralPath $destination -PathType Leaf) -and (Get-FileHash -LiteralPath $destination).Hash -eq $item.Value) {
            continue
        }
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        $built = Join-Path $PSScriptRoot ('build/' + $item.Name)
        Copy-Item -LiteralPath $built -Destination $destination -Force
    }
}

function Build-LeaguePanorama {
    param($Install)

    $resources = @(Get-LeagueResources $Install)
    if (!$resources.Count) {
        throw 'The Panorama source directory is empty.'
    }
    $package = Get-LeaguePackage $Install $resources
    if ($package) {
        Install-LeaguePackage $Install $package
        return
    }
    $needsBuild = @($resources | Where-Object { !$_.Current })
    if (!$needsBuild.Count) {
        return
    }
    if (!(Test-Path -LiteralPath $Install.Compiler -PathType Leaf)) {
        throw 'Собранные ресурсы интерфейса отсутствуют или устарели. Пересобери Panorama по README либо установи Dota 2 Workshop Tools в Steam.'
    }

    $baseGame = Join-Path $Install.Game 'dota'
    foreach ($resource in $needsBuild) {
        & $Install.Compiler '-game' $baseGame '-addon' $addonName '-i' $resource.Source
        if ($LASTEXITCODE -ne 0 -or !(Test-Path -LiteralPath $resource.Built -PathType Leaf)) {
            throw "Panorama compilation failed: $($resource.Source)"
        }
    }
    if (@(Get-LeagueResources $Install | Where-Object { !$_.Current }).Count) {
        throw 'Some Panorama resources were not rebuilt.'
    }
}

function Get-LeagueHealth {
    try {
        return Invoke-RestMethod -Uri $healthUrl -TimeoutSec 1
    } catch {
        return $null
    }
}

function Get-LeaguePython {
    $localPython = Join-Path $workspace '.venv/Scripts/python.exe'
    $python = Get-Command $localPython -ErrorAction SilentlyContinue
    if (!$python) { $python = Get-Command python.exe -ErrorAction SilentlyContinue }
    if (!$python -or $python.Source -like '*\WindowsApps\*') {
        $python = Get-Command py.exe -ErrorAction SilentlyContinue
    }
    if (!$python) {
        throw 'Python 3.11 or later is required.'
    }
    return $python
}

function Start-LeagueServer {
    param($Install)
    $status = Get-LeagueHealth
    if ($status.service -eq 'dota_offline_league') {
        $compatible = $status.schema -eq $serviceSchema -and $status.file_ipc -eq $true
        $owner = Get-NetTCPConnection -State Listen -LocalPort 32157 | Select-Object -ExpandProperty OwningProcess -Unique
        $running = Get-CimInstance Win32_Process -Filter "ProcessId=$owner"
        $latestSource = Get-Item -LiteralPath $serverScript, (Join-Path $PSScriptRoot 'league_social.py') |
            Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
        $managed = $running.Name -eq 'python.exe' -and $running.CommandLine -like ('*' + $serverScript + '*')
        $dotaRunning = Get-Process -Name dota2 -ErrorAction SilentlyContinue
        if ($compatible -and (!$managed -or $latestSource.LastWriteTimeUtc -le $running.CreationDate.ToUniversalTime() -or $dotaRunning)) {
            return
        }
        if ($managed -and !$dotaRunning) {
            Stop-Process -Id $owner
            Wait-Process -Id $owner -Timeout 5 -ErrorAction SilentlyContinue
            $status = $null
        }
    }
    if ($status) {
        throw 'Порт 32157 уже занят. Заверши старый процесс локальной лиги или другое приложение, использующее этот порт.'
    }

    $python = Get-LeaguePython
    if (!(Test-Path -LiteralPath $serverScript -PathType Leaf)) {
        throw 'league_server.py is missing.'
    }
    $pythonArgs = @()
    if ($python.Name -eq 'py.exe') {
        $pythonArgs += '-3'
    }
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    $database = Join-Path $dataDir 'league.sqlite3'
    $pythonArgs += @(('"' + $serverScript + '"'), '--db', ('"' + $database + '"'),
        '--ipc-log', ('"' + $Install.ConsoleLog + '"'), '--ipc-dir', ('"' + $Install.IpcDirectory + '"'),
        '--role-file', ('"' + $Install.RoleFile + '"'),
        '--replay-dir', ('"' + (Join-Path $Install.Game 'dota') + '"'))
    $processOptions = @{
        FilePath = $python.Source
        ArgumentList = $pythonArgs
        WorkingDirectory = $PSScriptRoot
        WindowStyle = 'Hidden'
        PassThru = $true
        RedirectStandardOutput = Join-Path $dataDir 'server.log'
        RedirectStandardError = Join-Path $dataDir 'server.err.log'
    }
    $server = Start-Process @processOptions

    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ($server.HasExited) {
            break
        }
        $status = Get-LeagueHealth
        if ($status.service -eq 'dota_offline_league' -and $status.schema -eq $serviceSchema -and $status.file_ipc -eq $true) {
            return
        }
        Start-Sleep -Milliseconds 200
    }
    throw "The local league service did not start. See $dataDir\server.err.log."
}

function Get-LeagueCoachStatus {
    foreach ($port in 32125, 32126) {
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:$port/status" -TimeoutSec 1
        } catch {
            continue
        }
    }
}

function Test-LeagueCoachesReady($status) {
    if ($status.Count -ne 2 -or $status[0].team -ne 'radiant' -or $status[1].team -ne 'dire') {
        return $false
    }
    foreach ($side in $status) {
        if ($side.mode -ne $CoachMode -or $side.runtime_revision -ne 4 -or !$side.worker_health.telemetry.healthy) {
            return $false
        }
        if ($CoachMode -eq 'model' -and ($side.controller -ne 'session' -or !$side.coach_session.ready -or
            !$side.coach_session.initialized -or $side.coach_session.model -ne $Model -or !$side.worker_health.coach.healthy)) {
            return $false
        }
    }
    return $true
}

function Start-LeagueCoaches {
    param($Install)
    if ($CoachMode -eq 'model' -and !$Model) {
        throw 'Для режима model укажите доступную модель через -Model.'
    }

    $status = @(Get-LeagueCoachStatus)
    if (Test-LeagueCoachesReady $status) {
        return
    }
    if ($status.Count) {
        if (Get-Process -Name dota2 -ErrorAction SilentlyContinue) {
            throw 'Тренеры обновятся при следующем запуске лиги после закрытия Dota 2.'
        }
        $owners = @(Get-NetTCPConnection -State Listen -LocalPort 32125, 32126 | Select-Object -ExpandProperty OwningProcess -Unique)
        foreach ($owner in $owners) {
            $running = Get-CimInstance Win32_Process -Filter "ProcessId=$owner"
            if ($running.Name -ne 'python.exe' -or $running.CommandLine -notmatch '(?:^|\s)-m\s+team_coach\s+--both(?:\s|$)') {
                throw 'Порты тренеров заняты другим приложением.'
            }
        }
        foreach ($owner in $owners) {
            Stop-Process -Id $owner
            Wait-Process -Id $owner -Timeout 5 -ErrorAction SilentlyContinue
        }
    }

    $python = Get-LeaguePython
    $arguments = @()
    if ($python.Name -eq 'py.exe') { $arguments += '-3' }
    $game = Split-Path -Parent $Install.Game
    $journal = Join-Path $dataDir 'coach-events.jsonl'
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    $arguments += @('-X', 'utf8', '-m', 'team_coach', '--both', '--team', 'radiant',
        '--mode', $CoachMode, '--opponent-mode', $CoachMode,
        '--decision-interval', '2', '--cooldown', '15', '--max-calls', '4096', '--model-timeout', '30',
        '--game', ('"' + $game + '"'), '--dota-log', ('"' + $Install.ConsoleLog + '"'), '--log', ('"' + $journal + '"'))
    if ($Model) { $arguments += @('--model', $Model) }
    $process = Start-Process -FilePath $python.Source -ArgumentList $arguments -WorkingDirectory $workspace `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $dataDir 'coach.log') `
        -RedirectStandardError (Join-Path $dataDir 'coach.err.log')
    $attempts = if ($CoachMode -eq 'model') { 350 } else { 60 }
    for ($attempt = 0; $attempt -lt $attempts -and !$process.HasExited; $attempt++) {
        $status = @(Get-LeagueCoachStatus)
        if (Test-LeagueCoachesReady $status) {
            return
        }
        Start-Sleep -Milliseconds 200
    }
    throw "Тренеры лиги не запустились. Журнал ошибок $dataDir\coach.err.log."
}

function Prepare-LeagueBotScripts {
    param($Install)
    if (!$Install.Workshop -or !(Test-Path -LiteralPath (Join-Path $Install.Workshop 'ability_item_usage_generic.lua') -PathType Leaf)) {
        throw 'Подпишитесь на Open Hyper AI в Steam Workshop и дождитесь загрузки. При необходимости укажите -WorkshopPath.'
    }
    $module = Join-Path $workspace 'team_coach'
    $botRoot = Join-Path $Install.Game 'dota/scripts/vscripts/bots'
    $stamp = Join-Path $botRoot '.league-script-revision'
    $sources = @('install.py', 'adapter.lua', 'fight_adapter.lua', 'league_roles.py', 'league_roles.lua', 'workshop_compat.py')
    $revision = ($sources | ForEach-Object {
        (Get-FileHash -LiteralPath (Join-Path $module $_) -Algorithm SHA256).Hash
    }) -join ':'
    $revision += ':' + (Get-FileHash -LiteralPath (Join-Path $Install.Workshop 'ability_item_usage_generic.lua')).Hash
    $workshopVersion = Join-Path $Install.Workshop 'FuncLib/systems/version.lua'
    if (Test-Path -LiteralPath $workshopVersion -PathType Leaf) {
        $revision += ':' + (Get-FileHash -LiteralPath $workshopVersion).Hash
    }
    if ((Test-Path -LiteralPath $stamp -PathType Leaf) -and
        (Get-Content -LiteralPath $stamp -Raw).Trim() -eq $revision) { return }
    if (Get-Process -Name dota2 -ErrorAction SilentlyContinue) {
        throw 'Закройте Dota 2 для обновления скриптов ботов лиги.'
    }
    $python = Get-LeaguePython
    $arguments = @()
    if ($python.Name -eq 'py.exe') { $arguments += '-3' }
    $game = Split-Path -Parent $Install.Game
    $arguments += @('-B', '-m', 'team_coach.install', '--team', 'radiant', '--opponent-port', '32126',
        '--log-telemetry', '--file-commands', '--game', ('"' + $game + '"'),
        '--workshop', ('"' + $Install.Workshop + '"'))
    $options = @{
        FilePath = $python.Source
        ArgumentList = $arguments
        WorkingDirectory = $workspace
        WindowStyle = 'Hidden'
        PassThru = $true
        Wait = $true
    }
    $process = Start-Process @options
    if ($process.ExitCode -ne 0) { throw 'Не удалось обновить скрипты ботов лиги.' }
    Set-Content -LiteralPath $stamp -Value $revision -Encoding ASCII
}

try {
    $install = Get-DotaInstall $SteamPath $DotaPath $WorkshopPath
    if ($Action -eq 'Server') {
        Start-LeagueServer $install
        Write-Output 'The local league service is ready.'
        exit 0
    }
    if ($Action -eq 'Coaches') {
        Prepare-LeagueBotScripts $install
        Start-LeagueCoaches $install
        exit 0
    }

    if ($Action -eq 'Check') {
        $resources = @(Get-LeagueResources $install)
        $package = Get-LeaguePackage $install $resources
        $panoramaReady = $resources.Count -gt 0 -and @($resources | Where-Object { !$_.Current }).Count -eq 0
        if ($package) {
            $panoramaReady = @($package | Where-Object {
                $path = Join-Path $install.Runtime $_.Name
                !(Test-Path -LiteralPath $path -PathType Leaf) -or (Get-FileHash -LiteralPath $path).Hash -ne $_.Value
            }).Count -eq 0
        }
        [pscustomobject]@{
            Steam = $install.Steam
            DotaGame = $install.Game
            WorkshopCompiler = Test-Path -LiteralPath $install.Compiler -PathType Leaf
            WorkshopBots = $install.Workshop
            CoachMode = $CoachMode
            PanoramaPackage = [bool]$package
            PanoramaReady = $panoramaReady
            DotaRunning = [bool](Get-Process -Name dota2 -ErrorAction SilentlyContinue)
        } | Format-List
        exit 0
    }

    if ((Get-Process -Name dota2 -ErrorAction SilentlyContinue)) {
        if ($Action -in 'Play', 'Prepare' -and (Get-LeagueHealth).file_ipc) {
            Start-LeagueCoaches $install
            Write-Output 'В консоли Dota выполни offline_league или dota_launch_custom_game offline_league dota.'
            exit 0
        }
        throw 'Для первой установки закрой Dota 2 и снова открой «Играть.cmd».'
    }

    Copy-LeagueAddon $install
    if ($Action -eq 'Tools') {
        $steamArgs = @('-applaunch', '570', '-tools', '-console', '-addon', $addonName)
        Start-Process -FilePath $install.Steam -ArgumentList $steamArgs -WindowStyle Hidden
        exit 0
    }
    Build-LeaguePanorama $install
    Prepare-LeagueBotScripts $install
    if ($Action -eq 'Install') {
        Write-Output 'The Local League addon is installed.'
        exit 0
    }

    Start-LeagueServer $install
    Start-LeagueCoaches $install
    $steamArgs = @('-applaunch', '570', '-novid', '-console', '-condebug', '-con_logfile', 'offline_league.log',
        '-offline_league_file_ipc', '+exec', 'offline_league.cfg')
    if ($Action -eq 'Prepare') {
        Write-Output 'Лига и оба сервера тренеров готовы к запуску из Dota.'
        exit 0
    }
    if (!$FromDotaMenu) {
        $steamArgs += '+offline_league'
    }
    Start-Process -FilePath $install.Steam -ArgumentList $steamArgs -WindowStyle Hidden
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
