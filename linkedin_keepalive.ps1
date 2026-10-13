<#
.SYNOPSIS
    Self-healing Keepalive supervisor for LinkedIn Auto-Apply Bot (GodsScion).
    Mirrors the Naukri keepalive pattern.

.DESCRIPTION
    1. Checks if today's daily application limit was reached:
       - Verified against LastLimitDate in state JSON, or today's logs (supervisor.log / log.txt).
       - The daily completion flag is ONLY set when LinkedIn's daily application limit is reached.
       - If the flag is set for today, logs status and sleeps peacefully until tomorrow (or exits 0 on -SinglePass).
    2. If limit is NOT yet reached:
       - Checks if python.exe running run_linkedin_supervised.py is alive via Get-CimInstance Win32_Process.
       - Debounces WMI queries (2-check miss required before declaring dead).
       - If dead:
         * Checks for hourly-cap velocity cooldown (GATE_STOPPED).
         * Applies exponential crash backoff.
         * Runs pre-launch cleanup of stale bot Chrome and chromedriver processes.
         * Starts run_linkedin_supervised.py detached in the background.
    3. Writes timestamped self-trimming log to logs/linkedin_keepalive.log (trims to 500 lines over 200KB).
    4. Supports -SinglePass, -Status, and -CheckIntervalSeconds switches.
    5. 2026-10-13 (L2): one Windows toast per incident: per wall day for LINKEDIN_WALL_<date>, and per NEEDS_HUMAN write
       (a wall's own NEEDS_HUMAN shares the wall's toast), recorded in the registry job-tracker\state\gulf_toasts.json.
       A LinkedIn verification wall ends that day; the next day's start is the one re-check. A wall on two days in a row
       writes logs\LINKEDIN_WALL_HOLD: nothing is relaunched while it exists, and only the user deletes it. -Status shows
       today's wall marker and the hold.

.PARAMETER SinglePass
    Run a single evaluation cycle and exit immediately. Default is continuous loop.

.PARAMETER CheckIntervalSeconds
    Frequency of liveness checks in continuous loop mode (default 60s).

.PARAMETER Status
    Show live status (process, today's applied count, limit flag, crash history) and exit without action.
#>

[CmdletBinding()]
param(
    [switch]$SinglePass,
    [int]$CheckIntervalSeconds = 60,
    [switch]$Status
)

$ErrorActionPreference = "Continue"

# ---------------------------------------------------------------- Configuration
$RepoDir = $PSScriptRoot
if (-not $RepoDir) {
    $RepoDir = (Split-Path -Parent $MyInvocation.MyCommand.Path)
}
if (-not $RepoDir) {
    $RepoDir = "C:\Users\Admin\GitHub\FORKED_Auto_job_applier_linkedIn"
}

$VenvPython = Join-Path $RepoDir ".venv\Scripts\python.exe"
$PythonExe  = if (Test-Path $VenvPython) { $VenvPython } else { "python.exe" }
$ScriptFile = "run_linkedin_supervised.py"
$ScriptPath = Join-Path $RepoDir $ScriptFile

$LogsDir = Join-Path $RepoDir "logs"
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Force -Path $LogsDir -ErrorAction SilentlyContinue | Out-Null
}

$LogPath           = Join-Path $LogsDir "linkedin_keepalive.log"
$StatePath         = Join-Path $LogsDir "linkedin_keepalive.state.json"
$StdoutPath        = Join-Path $LogsDir "linkedin_keepalive_stdout.log"
$StderrPath        = Join-Path $LogsDir "linkedin_keepalive_stderr.log"
$BotLogPath        = Join-Path $LogsDir "log.txt"
$SupervisorLogPath = Join-Path $LogsDir "supervisor.log"
$GateMarkerPath    = Join-Path $LogsDir "GATE_STOPPED"
$SupervisorLock    = Join-Path $LogsDir "supervisor.lock"
$HistoryCsvPath    = Join-Path $RepoDir "all excels\all_applied_applications_history.csv"
$ResetMarginMin    = 5     # start this many minutes after LinkedIn's daily reset (00:00 UTC = 05:30 IST)
$WallHoldPath      = Join-Path $LogsDir "LINKEDIN_WALL_HOLD"
# Per-day toast registry, meant to be shared with the Gulf tools (none writes it yet). Its format is defined at
# Read-ToastRegistry below.
$ToastRegistryPath = "C:\Users\Admin\GitHub\job-tracker\state\gulf_toasts.json"
$script:ToastsShown = @{}  # this process's own record too, so an unreadable registry never repeats a toast every pass

# LinkedIn's Easy Apply quota resets at 00:00 UTC, not at local midnight: measured 2026-10-01..10-08, every
# launch between 00:00 and 05:30 IST was refused and every first success of a day came after 05:30 IST.
function Get-NextLinkedInReset {
    $utcNow = (Get-Date).ToUniversalTime()
    return $utcNow.Date.AddDays(1).ToLocalTime().AddMinutes($ResetMarginMin)
}

# ---------------------------------------------------------------- Logging
function Write-KeepaliveLog {
    param(
        [string]$Message,
        [string]$Level = "INFO"
    )
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] [$Level] $Message"
    
    $color = switch ($Level) {
        "ERROR" { "Red" }
        "WARN"  { "Yellow" }
        "OK"    { "Green" }
        default { "Cyan" }
    }
    Write-Host $line -ForegroundColor $color

    try {
        Add-Content -Path $LogPath -Value $line -Encoding utf8 -ErrorAction SilentlyContinue
        
        # Self-trimming pattern: keep last 500 lines if > 200KB
        if (Test-Path $LogPath) {
            $fileItem = Get-Item $LogPath -ErrorAction SilentlyContinue
            if ($fileItem -and $fileItem.Length -gt 200KB) {
                $lines = Get-Content -Path $LogPath -Tail 500 -ErrorAction SilentlyContinue
                Set-Content -Path $LogPath -Value $lines -Encoding utf8 -ErrorAction SilentlyContinue
            }
        }
    } catch {}
}

# ---------------------------------------------------------------- Safe Shared File Reading
function Get-FileTailShared {
    param(
        [string]$Path,
        [int]$MaxLines = 100
    )
    if (-not (Test-Path $Path)) { return @() }
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
        $buffer = New-Object System.Collections.Generic.List[string]
        while (-not $reader.EndOfStream) {
            $buffer.Add($reader.ReadLine())
            if ($buffer.Count -gt 3000) {
                $buffer.RemoveRange(0, 1000)
            }
        }
        $reader.Close()
        $stream.Close()
        if ($buffer.Count -le $MaxLines) { return $buffer.ToArray() }
        return $buffer.GetRange($buffer.Count - $MaxLines, $MaxLines).ToArray()
    } catch {
        return @()
    }
}

# ---------------------------------------------------------------- State Helpers
function Get-KeepaliveState {
    if (Test-Path $StatePath) {
        try {
            $raw = Get-Content -Path $StatePath -Raw -ErrorAction SilentlyContinue
            if ($raw) {
                return ($raw | ConvertFrom-Json)
            }
        } catch {}
    }
    return [PSCustomObject]@{
        LastLaunchTime     = $null
        ConsecutiveCrashes = 0
        LastCrashTime      = $null
        LastLimitDate      = $null
        MissedChecks       = 0
        CooldownUntil      = $null
    }
}

function Save-KeepaliveState {
    param($State)
    try {
        $json = $State | ConvertTo-Json -Depth 5
        Set-Content -Path $StatePath -Value $json -Encoding utf8 -ErrorAction SilentlyContinue
    } catch {}
}

# ---------------------------------------------------------------- Toasts and the wall hold (L2, 2026-10-13)
# Show-Toast is copied from job-tracker\naukrigulf_apply_keepalive.ps1 (instahyre_apply_keepalive.ps1 has the same), plus a
# return value: $true when Windows accepted the toast, $false when it failed.
function Show-Toast([string]$Title, [string]$Body) {
    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
        $t = [System.Security.SecurityElement]::Escape($Title)
        $b = [System.Security.SecurityElement]::Escape($Body)
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual></toast>")
        $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show(
            [Windows.UI.Notifications.ToastNotification]::new($xml))
        return $true
    } catch {
        Write-KeepaliveLog "Toast failed: $($_.Exception.Message)" "WARN"
        return $false
    }
}

# Toast registry format (defined here; no other tool writes the file yet): one JSON object whose keys are
# "<yyyy-MM-dd>|<key>" and whose values are {at, by, title}, for example
#   {"2026-10-14|linkedin:LINKEDIN_WALL": {"at": "2026-10-14T06:01:22", "by": "linkedin_keepalive", "title": "..."}}
# Returns the entries as a hashtable (empty when the file does not exist or is blank), or $null when the file cannot be read
# or holds anything else. $null means "show, but do not save": another tool's file is never overwritten.
function Read-ToastRegistry {
    if (-not (Test-Path -LiteralPath $ToastRegistryPath)) { return @{} }
    $raw = $null
    for ($try = 0; ($try -lt 3) -and ($null -eq $raw); $try++) {
        try { $raw = [System.IO.File]::ReadAllText($ToastRegistryPath) } catch { Start-Sleep -Milliseconds 200 }
    }
    if ($null -eq $raw) {
        Write-KeepaliveLog ("Toast registry {0} could not be read; toasts are shown but not recorded." -f $ToastRegistryPath) "WARN"
        return $null
    }
    $reg = @{}
    if (-not $raw.Trim()) { return $reg }
    $obj = $null
    try { $obj = $raw | ConvertFrom-Json -ErrorAction Stop } catch { $obj = $null }
    $ok = $obj -is [System.Management.Automation.PSCustomObject]
    if ($ok) {
        foreach ($p in $obj.PSObject.Properties) {
            if (($p.Name -notmatch '^\d{4}-\d{2}-\d{2}\|.+') -or -not ($p.Value -is [System.Management.Automation.PSCustomObject])) {
                $ok = $false
                break
            }
            $reg[$p.Name] = $p.Value
        }
    }
    if (-not $ok) {
        Write-KeepaliveLog ("Toast registry {0} is not in this keepalive's format; it is left as it is (toasts are shown but not recorded)." -f $ToastRegistryPath) "WARN"
        return $null
    }
    return $reg
}

# Atomic write: a temporary file next to the registry, then File.Replace (or File.Move when it does not exist yet), so a
# reader never sees half a file. NullString: PowerShell would pass $null as "" (File.Replace's backup name; none wanted).
function Save-ToastRegistry([hashtable]$Reg) {
    $tmp = "{0}.{1}.tmp" -f $ToastRegistryPath, $PID
    try {
        $dir = Split-Path -Parent $ToastRegistryPath
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null }
        $json = $Reg | ConvertTo-Json -Depth 5
        for ($try = 0; $try -lt 5; $try++) {
            try {
                [System.IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
                if (Test-Path -LiteralPath $ToastRegistryPath) {
                    [System.IO.File]::Replace($tmp, $ToastRegistryPath, [System.Management.Automation.Language.NullString]::Value)
                } else {
                    [System.IO.File]::Move($tmp, $ToastRegistryPath)
                }
                return $true
            } catch {
                Start-Sleep -Milliseconds 200
            }
        }
    } catch {}
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    Write-KeepaliveLog ("Toast registry {0} could not be written." -f $ToastRegistryPath) "WARN"
    return $false
}

# One toast per incident key per day: $Day is the incident's date, $Key e.g. "linkedin:LINKEDIN_WALL". The shared registry
# records a toast only when Windows really showed it; a failed toast is marked in this process's memory only, so it is not
# retried every pass, and a restarted keepalive tries it once more.
function Show-ToastOnce([string]$Day, [string]$Key, [string]$Title, [string]$Body) {
    $id = "{0}|{1}" -f $Day, $Key
    if ($script:ToastsShown.ContainsKey($id)) { return }
    $reg = Read-ToastRegistry
    if (($null -ne $reg) -and $reg.ContainsKey($id)) {
        $script:ToastsShown[$id] = $true
        return
    }
    $shown = Show-Toast $Title $Body
    $script:ToastsShown[$id] = $true
    if (-not $shown) {
        Write-KeepaliveLog ("Toast {0} was not shown, so it is not recorded in the registry: {1}. {2}" -f $id, $Title, $Body) "WARN"
        return
    }
    if ($null -ne $reg) {
        $reg[$id] = [ordered]@{ at = (Get-Date).ToString("s"); by = "linkedin_keepalive"; title = $Title }
        [void](Save-ToastRegistry $reg)
    }
    Write-KeepaliveLog ("Toast {0}: {1}. {2}" -f $id, $Title, $Body) "WARN"
}

# Runs on every pass. 1) A wall marker (logs\LINKEDIN_WALL_<date>, written by run_linkedin_local.py before it exits 48) that
# this keepalive has not handled yet: the hold rule, then the wall toast. Yesterday's marker is looked at too, in case it
# appeared after the last pass of that day. $State.WallSeenDate records the last wall day handled, so a hold the user has
# deleted is never written again. 2) Today's NEEDS_HUMAN (run_linkedin_supervised.py: a wall, a failed sign-in, ...): one
# toast per write, keyed by its LastWriteTime, so a second incident on the same day (after the user cleared the first) toasts
# again. It gets no toast of its own only when it is the same incident as today's wall marker (it says "verification wall"
# and was written within 15 minutes of the marker): that wall's toast is the incident's one toast.
function Invoke-IncidentAlerts {
    param($State, [string]$TodayStr)
    $seen = ""
    try {
        $v = $State.WallSeenDate
        # a "yyyy-MM-dd" string; PowerShell 7's ConvertFrom-Json would hand back a [datetime]
        if ($v -is [datetime]) { $seen = $v.ToString("yyyy-MM-dd") } elseif ($v) { $seen = [string]$v }
    } catch { $seen = "" }
    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    $yesterdayStr = (Get-Date).AddDays(-1).ToString("yyyy-MM-dd")
    foreach ($day in @($yesterdayStr, $TodayStr)) {
        $marker = Join-Path $LogsDir ("LINKEDIN_WALL_{0}" -f $day)
        if (-not (Test-Path -LiteralPath $marker)) { continue }
        if ($seen -and ($seen -ge $day)) { continue }
        $kind = "unknown"
        $where = "unknown"
        try {
            $info = [System.IO.File]::ReadAllText($marker) | ConvertFrom-Json -ErrorAction Stop
            if ($info.kind) { $kind = [string]$info.kind }
            if ($info.where) { $where = [string]$info.where }
        } catch {}
        $prevDay = [datetime]::ParseExact($day, "yyyy-MM-dd", $inv).AddDays(-1).ToString("yyyy-MM-dd")
        $held = Test-Path -LiteralPath (Join-Path $LogsDir ("LINKEDIN_WALL_{0}" -f $prevDay))
        if ($held) {
            # Hold rule: a wall yesterday and a new one today. Only the user deletes the hold file.
            if (-not (Test-Path -LiteralPath $WallHoldPath)) {
                $hold = [ordered]@{
                    since = (Get-Date).ToString("s")
                    walls = @(("LINKEDIN_WALL_{0}" -f $prevDay), ("LINKEDIN_WALL_{0}" -f $day))
                    clear = "Delete this file once the wall is cleared in the linkedin-1 browser profile. The keepalive never deletes it."
                }
                try {
                    [System.IO.File]::WriteAllText($WallHoldPath, ($hold | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
                } catch {
                    Write-KeepaliveLog ("Could not write {0}: {1}" -f $WallHoldPath, $_.Exception.Message) "ERROR"
                }
            }
            Write-KeepaliveLog ("LinkedIn verification wall on {0} and again on {1} ({2}, {3}): LINKEDIN_WALL_HOLD is in force; nothing is relaunched until the user deletes it." -f $prevDay, $day, $kind, $where) "ERROR"
            $tail = "Second wall day in a row: the bot stays off until you delete logs\LINKEDIN_WALL_HOLD."
        } else {
            Write-KeepaliveLog ("LinkedIn verification wall on {0} ({1}, {2}): no relaunch that day; the next day's start is the one re-check." -f $day, $kind, $where) "ERROR"
            $tail = "No relaunch that day; the next day's start is the one re-check."
        }
        $body = "LinkedIn showed a security page ({0}, {1}). YOUR TURN: open the linkedin-1 browser profile and clear it. {2}" -f $kind, $where, $tail
        Show-ToastOnce -Day $day -Key "linkedin:LINKEDIN_WALL" -Title "LinkedIn bot stopped: verification wall" -Body $body
        $State | Add-Member -NotePropertyName "WallSeenDate" -NotePropertyValue $day -Force
        Save-KeepaliveState $State
        $seen = $day
    }

    $needsHuman = Join-Path $LogsDir "NEEDS_HUMAN"
    $nh = Get-Item -LiteralPath $needsHuman -ErrorAction SilentlyContinue
    if ($nh -and $nh.LastWriteTime.ToString("yyyy-MM-dd") -eq $TodayStr) {
        $line = ""
        try { $line = [string](Get-Content -LiteralPath $needsHuman -TotalCount 1 -Encoding UTF8 -ErrorAction Stop) } catch { $line = "" }
        $reason = ($line -replace '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*', '').Trim()
        $sameAsWall = $false
        $wallItem = Get-Item -LiteralPath (Join-Path $LogsDir ("LINKEDIN_WALL_{0}" -f $TodayStr)) -ErrorAction SilentlyContinue
        if ($wallItem -and ($reason -like "*verification wall*")) {
            $sameAsWall = [math]::Abs(($nh.LastWriteTime - $wallItem.LastWriteTime).TotalMinutes) -le 15
        }
        if (-not $sameAsWall) {
            if (-not $reason) { $reason = "The supervisor stopped and wrote logs\NEEDS_HUMAN." }
            if ($reason.Length -gt 200) { $reason = $reason.Substring(0, 200) + "..." }
            $key = "linkedin:NEEDS_HUMAN@{0}" -f $nh.LastWriteTime.ToString("HH:mm:ss")
            Show-ToastOnce -Day $TodayStr -Key $key -Title "LinkedIn bot needs you" -Body ("{0} Details: logs\supervisor.log in the LinkedIn bot folder." -f $reason)
        }
    }
}

# ---------------------------------------------------------------- Count & Limit Queries
function Get-DailyAppliedCount {
    param(
        [string]$CsvPath,
        [string]$PyExe
    )
    if (-not (Test-Path $CsvPath)) {
        return 0
    }
    $pyCode = @"
import csv, sys, os
from datetime import date
try:
    csv_file = r'$CsvPath'
    if not os.path.exists(csv_file):
        print(0); sys.exit(0)
    today = str(date.today())
    count = 0
    with open(csv_file, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f)
        for row in reader:
            da = row.get('Date Applied') or ''
            if da.startswith(today):
                count += 1
    print(count)
except Exception as e:
    sys.stderr.write(str(e) + '\n')
    print(-1)
"@
    try {
        $out = & $PyExe -c $pyCode 2>$null
        if ($out -match '^\d+$') {
            return [int]$out
        }
    } catch {
        return -1
    }
    return -1
}

function Test-DailyLimitReached {
    param(
        $State,
        [string]$TodayStr,
        [int]$TodayCount = -1
    )
    # 1. Check for today's machine-readable daily limit marker file (written ONLY on genuine limit banner)
    $markerPath = Join-Path $LogsDir "LINKEDIN_DAILY_LIMIT_$TodayStr"
    if (Test-Path $markerPath) {
        $State | Add-Member -NotePropertyName "LastLimitDate" -NotePropertyValue $TodayStr -Force
        Save-KeepaliveState $State
        return $true
    }

    # 2. State flag verified for today ONLY if marker exists or today's count > 0
    if ($State.LastLimitDate -and $State.LastLimitDate -eq $TodayStr) {
        if ($TodayCount -gt 0 -or (Test-Path $markerPath)) {
            return $true
        } else {
            # Guard: 0 applications applied today and no marker file exists.
            # Clear suspicious limit flag to ensure self-healing relaunch instead of false rest.
            $State.LastLimitDate = $null
            Save-KeepaliveState $State
        }
    }

    return $false
}

# ---------------------------------------------------------------- Process Inspection
function Get-SupervisorProcess {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*run_linkedin_supervised.py*" }
    return $procs
}

# ---------------------------------------------------------------- Process Launch
function Start-SupervisorDetached {
    Write-KeepaliveLog "Launching $ScriptFile detached in background..." "INFO"

    # Pre-launch sweep: clean orphaned Chrome instances using linkedin-1 profile
    try {
        $staleChrome = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*linkedin-1*" }
        foreach ($c in $staleChrome) {
            Stop-Process -Id $c.ProcessId -Force -ErrorAction SilentlyContinue
        }
        if ($staleChrome) {
            Write-KeepaliveLog "Pre-launch sweep: closed $($staleChrome.Count) stale bot-profile Chrome process(es)." "INFO"
            Start-Sleep -Seconds 2
        }
    } catch {}

    # Pre-launch sweep: clean orphaned chromedriver.exe
    try {
        $drivers = Get-CimInstance Win32_Process -Filter "Name='chromedriver.exe'" -ErrorAction SilentlyContinue
        foreach ($d in $drivers) {
            Stop-Process -Id $d.ProcessId -Force -ErrorAction SilentlyContinue
        }
    } catch {}

    # Clear stale supervisor lock if no supervisor is alive
    try {
        if (Test-Path $SupervisorLock) {
            Remove-Item -Path $SupervisorLock -Force -ErrorAction SilentlyContinue
        }
    } catch {}

    # Rotate previous stdout/stderr logs
    foreach ($p in @($StdoutPath, $StderrPath)) {
        if (Test-Path $p) {
            Copy-Item $p "$p.previous" -Force -ErrorAction SilentlyContinue
        }
    }

    $env:PYTHONUNBUFFERED = "1"
    # (2026-09-30: no C:\WebDrivers PATH prepend - a stale chromedriver on PATH pins the version.)

    $proc = Start-Process -FilePath $PythonExe `
        -ArgumentList $ScriptFile `
        -WorkingDirectory $RepoDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $StdoutPath `
        -RedirectStandardError $StderrPath `
        -PassThru

    Start-Sleep -Seconds 4
    return $proc
}

# ---------------------------------------------------------------- Core Keepalive Pass
function Invoke-KeepalivePass {
    $state = Get-KeepaliveState
    $todayStr = (Get-Date -Format "yyyy-MM-dd")
    $now = Get-Date

    # 1. Process inspection (debounced)
    $runningProcs = $null
    try {
        $runningProcs = Get-SupervisorProcess
    } catch {
        $runningProcs = $null
    }
    $isRunning = ($runningProcs -ne $null -and @($runningProcs).Count -gt 0)
    $activeProc = if ($isRunning) { @($runningProcs)[0] } else { $null }

    # 2. Check applied count & daily limit flag
    $todayCount   = Get-DailyAppliedCount -CsvPath $HistoryCsvPath -PyExe $PythonExe
    $limitReached = Test-DailyLimitReached -State $state -TodayStr $todayStr -TodayCount $todayCount

    # 3. Status display mode
    if ($Status) {
        Write-Host ""
        Write-Host "=================== LINKEDIN KEEPALIVE STATUS ===================" -ForegroundColor Cyan
        if ($isRunning) {
            Write-Host "Process State    : RUNNING (PID $($activeProc.ProcessId), Started: $($activeProc.CreationDate))" -ForegroundColor Green
        } else {
            Write-Host "Process State    : STOPPED / DEAD" -ForegroundColor Yellow
        }
        $flagText = if ($limitReached) { "REACHED ($todayStr) - resting until tomorrow" } else { "NOT YET REACHED (bot will keep running)" }
        $flagColor = if ($limitReached) { "Green" } else { "White" }
        Write-Host "Daily Limit Flag : $flagText" -ForegroundColor $flagColor
        Write-Host "Today Applied    : $todayCount" -ForegroundColor White
        Write-Host "Crash Count      : $($state.ConsecutiveCrashes)" -ForegroundColor White
        Write-Host "Last Launch Time : $($state.LastLaunchTime)" -ForegroundColor White
        Write-Host "Last Limit Date  : $($state.LastLimitDate)" -ForegroundColor White
        if ($state.CooldownUntil) {
            Write-Host "Cooldown Until   : $($state.CooldownUntil)" -ForegroundColor Yellow
        }
        # 2026-10-14 (L2): today's wall marker and the wall hold, when present.
        $wallToday = Join-Path $LogsDir ("LINKEDIN_WALL_{0}" -f $todayStr)
        if (Test-Path -LiteralPath $wallToday) {
            $wallText = "marker present (unreadable)"
            try {
                $w = [System.IO.File]::ReadAllText($wallToday) | ConvertFrom-Json -ErrorAction Stop
                $wallText = "{0}, {1}, since {2}, path {3}" -f $w.kind, $w.where, $w.since, $w.url_path
            } catch {}
            Write-Host ("Wall Today       : {0}" -f $wallText) -ForegroundColor Red
        }
        if (Test-Path -LiteralPath $WallHoldPath) {
            Write-Host ("Wall Hold        : IN FORCE - nothing is relaunched until you delete {0}" -f $WallHoldPath) -ForegroundColor Red
        }
        Write-Host "=================================================================" -ForegroundColor Cyan
        return
    }

    # 3b. 2026-10-13 (L2): one toast per incident per day (NEEDS_HUMAN, LINKEDIN_WALL) and the wall hold.
    Invoke-IncidentAlerts -State $state -TodayStr $todayStr

    # 4. If running, verify health and reset crash counters if stable
    if ($isRunning) {
        $state | Add-Member -NotePropertyName "MissedChecks" -NotePropertyValue 0 -Force
        Save-KeepaliveState $state

        $uptimeSec = [math]::Round(($now - $activeProc.CreationDate).TotalSeconds, 1)
        if ($uptimeSec -ge 120 -and $state.ConsecutiveCrashes -gt 0) {
            Write-KeepaliveLog "Process stable for $([math]::Round($uptimeSec/60, 1))m (>= 2m). Resetting crash count to 0." "INFO"
            $state.ConsecutiveCrashes = 0
            Save-KeepaliveState $state
        }
        Write-KeepaliveLog "Supervisor alive (PID $($activeProc.ProcessId), uptime $([math]::Round($uptimeSec/60, 1))m). Today applied: $todayCount. Limit reached: $limitReached." "OK"
        return
    }

    # 5. Process is DEAD - LinkedIn's daily limit
    # 2026-10-08: the limit is LinkedIn's own, counted per UTC day (resets 05:30 IST). Resting until local
    # midnight and then retrying every 2 h (the 10-01 design, made before the reset time was known) wasted
    # 3 refused launches a night, ran the UAE pass only when the quota was already gone, and made the
    # dashboard show "Failing". Now a limit, with or without saves today, rests until the next reset.
    $restUntil = $null
    try { if ($state.LimitRestUntil) { $restUntil = [datetime]$state.LimitRestUntil } } catch { $restUntil = $null }
    if ($limitReached -and -not $restUntil) {
        $restUntil = Get-NextLinkedInReset
        $state | Add-Member -NotePropertyName "LimitRestUntil" -NotePropertyValue $restUntil.ToString("s") -Force
        Write-KeepaliveLog "LinkedIn's Easy Apply limit is reached ($todayCount saved today). LinkedIn resets it at 00:00 UTC; resting until $($restUntil.ToString('yyyy-MM-dd HH:mm'))." "OK"
    }
    if ($restUntil) {
        $state.LastLimitDate = $todayStr
        $state.ConsecutiveCrashes = 0
        Save-KeepaliveState $state
        if ($SinglePass) { return }
        while ((Get-Date) -lt $restUntil) {
            $chunk = [Math]::Min(300, [int]($restUntil - (Get-Date)).TotalSeconds)
            if ($chunk -le 0) { break }
            Start-Sleep -Seconds $chunk
        }
        # A new LinkedIn day: set the limit markers that caused the rest aside so it starts clean.
        foreach ($d in @($todayStr, (Get-Date -Format "yyyy-MM-dd"))) {
            $marker = Join-Path $LogsDir "LINKEDIN_DAILY_LIMIT_$d"
            if (Test-Path $marker) {
                Move-Item -Path $marker -Destination "$marker.rested-$((Get-Date).ToString('HHmm'))" -Force -ErrorAction SilentlyContinue
            }
        }
        $state = Get-KeepaliveState
        $state.LastLimitDate = $null
        $state | Add-Member -NotePropertyName "LimitRestUntil" -NotePropertyValue $null -Force
        Save-KeepaliveState $state
        Write-KeepaliveLog "LinkedIn's daily reset has passed; relaunching on the next pass." "INFO"
        return
    }

    # 6. Process is DEAD and Daily Limit is NOT yet reached: Must Keep Running!
    # WMI-flake debounce: wait 1 more tick before declaring dead
    $missed = 0
    try { $missed = [int]$state.MissedChecks } catch { $missed = 0 }
    $missed++
    $state | Add-Member -NotePropertyName "MissedChecks" -NotePropertyValue $missed -Force
    Save-KeepaliveState $state
    if ($missed -lt 2) {
        Write-KeepaliveLog "Supervisor not seen, waiting one more tick before relaunch as WMI-flake guard." "WARN"
        return
    }
    $state | Add-Member -NotePropertyName "MissedChecks" -NotePropertyValue 0 -Force
    Save-KeepaliveState $state

    # 6b. 2026-09-30: the supervisor writes logs\NEEDS_HUMAN when it stops for a reason a relaunch
    # cannot fix (browser/driver launch failed rc 43, apply gate rc 42, zero yield, persistent
    # breakage, max restarts). Before this, the keepalive relaunched it every 2-4 min all night.
    # Today's marker blocks relaunching; a marker from an earlier day is set aside so the new day
    # gets one fresh attempt (a persistent break re-writes it within minutes).
    $needsHuman = Join-Path $LogsDir "NEEDS_HUMAN"
    $block = $null
    $fix = "Fix the cause, then delete $needsHuman (and GATE_STOPPED if present)."
    if (Test-Path $needsHuman) {
        $nh = Get-Item $needsHuman -ErrorAction SilentlyContinue
        if ($nh -and $nh.LastWriteTime.ToString("yyyy-MM-dd") -eq $todayStr) {
            $block = "NEEDS_HUMAN: " + (Get-Content $needsHuman -TotalCount 1 -ErrorAction SilentlyContinue)
        } elseif ($nh) {
            $prev = "$needsHuman.$($nh.LastWriteTime.ToString('yyyy-MM-dd')).prev"
            Move-Item $needsHuman $prev -Force -ErrorAction SilentlyContinue
            Write-KeepaliveLog "NEEDS_HUMAN from $($nh.LastWriteTime.ToString('yyyy-MM-dd')) set aside ($prev); giving the new day one fresh attempt." "WARN"
        }
    }
    # 2026-10-13 (L2): a LinkedIn verification wall on two days in a row (Invoke-IncidentAlerts writes the hold file).
    if (Test-Path -LiteralPath $WallHoldPath) {
        $block = "LINKEDIN_WALL_HOLD (a LinkedIn verification wall two days in a row)"
        $fix = "Clear the wall in the linkedin-1 browser profile, then delete $WallHoldPath (and $needsHuman to restart today)."
    }
    if (-not $block -and (Test-Path $GateMarkerPath) -and $state.LastLaunchTime) {
        try {
            if ((Get-Item $GateMarkerPath).LastWriteTime -gt [datetime]$state.LastLaunchTime) {
                $block = "GATE_STOPPED written after the last launch"
            }
        } catch {}
    }
    if ($block) {
        $lastNote = $null
        try { $lastNote = [datetime]$state.LastBlockedLog } catch {}
        if (-not $lastNote -or ($now - $lastNote).TotalMinutes -ge 30) {
            Write-KeepaliveLog "Not relaunching: $block. $fix" "ERROR"
            $state | Add-Member -NotePropertyName "LastBlockedLog" -NotePropertyValue $now.ToString("o") -Force
            Save-KeepaliveState $state
        }
        return
    }

    # 7. Exponential crash backoff
    if ($state.LastLaunchTime) {
        try {
            $lastLaunch = [datetime]$state.LastLaunchTime
            $runtimeSec = ($now - $lastLaunch).TotalSeconds
            if ($runtimeSec -lt 120) {
                $state.ConsecutiveCrashes++
                $state.LastCrashTime = $now.ToString("o")
                $backoff = switch ($state.ConsecutiveCrashes) {
                    1 { 10 }
                    2 { 30 }
                    3 { 60 }
                    4 { 180 }
                    default { 300 }
                }
                Write-KeepaliveLog "Process exited within 2 minutes ($([math]::Round($runtimeSec, 1))s since launch). Crash count: $($state.ConsecutiveCrashes). Backing off for ${backoff}s..." "WARN"
                Save-KeepaliveState $state
                Start-Sleep -Seconds $backoff
            } else {
                $state.ConsecutiveCrashes = 0
                Save-KeepaliveState $state
            }
        } catch {}
    }

    # 9. Launch supervisor
    Write-KeepaliveLog "Process is DEAD and daily limit is not yet reached ($todayCount applied today). Initiating autonomous launch..." "INFO"
    $newProc = Start-SupervisorDetached
    if ($newProc -and -not $newProc.HasExited) {
        Write-KeepaliveLog "Supervisor started successfully (PID $($newProc.Id)). Redirecting output to $StdoutPath" "OK"
        $state.LastLaunchTime = (Get-Date).ToString("o")
        Save-KeepaliveState $state
    } else {
        Write-KeepaliveLog "Failed to start supervisor process." "ERROR"
    }
}

# ---------------------------------------------------------------- Execution
if ($Status) {
    Invoke-KeepalivePass
    exit 0
}

# Prevent duplicate supervisor instances
$myPid = $PID
$otherSupervisors = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' or Name='pwsh.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.ProcessId -ne $myPid -and
        $_.CommandLine -like "*linkedin_keepalive.ps1*" -and
        $_.CommandLine -notlike "*-Status*"
    })
if ($otherSupervisors.Count -gt 0) {
    Write-KeepaliveLog "Another LinkedIn keepalive supervisor is already active (PID $($otherSupervisors[0].ProcessId)). Exiting this instance to prevent duplicates." "INFO"
    exit 0
}

Write-KeepaliveLog "LinkedIn keepalive supervisor started (Mode: $(if ($SinglePass) { 'SinglePass' } else { 'Continuous' }))." "INFO"

if ($SinglePass) {
    Invoke-KeepalivePass
    Write-KeepaliveLog "SinglePass evaluation completed." "INFO"
    exit 0
}

# Continuous loop
while ($true) {
    Invoke-KeepalivePass
    Start-Sleep -Seconds $CheckIntervalSeconds
}
