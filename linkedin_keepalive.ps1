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
        [string]$TodayStr
    )
    # 1. State flag already verified for today
    if ($State.LastLimitDate -and $State.LastLimitDate -eq $TodayStr) {
        return $true
    }

    # 2. Check for today's machine-readable daily limit marker file
    $markerPath = Join-Path $LogsDir "LINKEDIN_DAILY_LIMIT_$TodayStr"
    if (Test-Path $markerPath) {
        $State | Add-Member -NotePropertyName "LastLimitDate" -NotePropertyValue $TodayStr -Force
        Save-KeepaliveState $State
        return $true
    }

    # 3. Check supervisor.log for today's limit signal
    if (Test-Path $SupervisorLogPath) {
        $supLines = Get-FileTailShared -Path $SupervisorLogPath -MaxLines 200
        foreach ($line in $supLines) {
            if ($line.StartsWith($TodayStr) -and ($line -like "*daily Easy Apply limit reached*" -or $line -like "*Daily application limit*")) {
                $State | Add-Member -NotePropertyName "LastLimitDate" -NotePropertyValue $TodayStr -Force
                Save-KeepaliveState $State
                return $true
            }
        }
    }

    # 4. Check log.txt if modified today
    if (Test-Path $BotLogPath) {
        $logItem = Get-Item $BotLogPath -ErrorAction SilentlyContinue
        if ($logItem -and $logItem.LastWriteTime.ToString("yyyy-MM-dd") -eq $TodayStr) {
            $botLines = Get-FileTailShared -Path $BotLogPath -MaxLines 300
            foreach ($line in $botLines) {
                if ($line -like "*Daily application limit for Easy Apply is reached!*" -or `
                    $line -like "*exceeded the daily application limit*" -or `
                    $line -like "*limit daily submissions*" -or `
                    $line -like "*apply tomorrow*") {
                    $State | Add-Member -NotePropertyName "LastLimitDate" -NotePropertyValue $TodayStr -Force
                    Save-KeepaliveState $State
                    return $true
                }
            }
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
    $env:PATH = "C:\WebDrivers;" + $env:PATH

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

    # 2. Check daily limit flag and applied count
    $limitReached = Test-DailyLimitReached -State $state -TodayStr $todayStr
    $todayCount   = Get-DailyAppliedCount -CsvPath $HistoryCsvPath -PyExe $PythonExe

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
        Write-Host "=================================================================" -ForegroundColor Cyan
        return
    }

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

    # 5. Process is DEAD - Check Daily Limit Flag
    # If today's daily limit has been reached, sleep peacefully until tomorrow (or exit on SinglePass)
    if ($limitReached) {
        Write-KeepaliveLog "Daily application limit reached for today ($todayStr). LinkedIn auto-apply has completed its daily run ($todayCount applied). Resting until tomorrow." "OK"
        $state.LastLimitDate = $todayStr
        $state.ConsecutiveCrashes = 0
        Save-KeepaliveState $state

        if (-not $SinglePass) {
            $tomorrow = $now.Date.AddDays(1).AddMinutes(1)
            $sleepSec = [int]($tomorrow - (Get-Date)).TotalSeconds
            if ($sleepSec -gt 0) {
                Write-KeepaliveLog "Continuous mode: sleeping peacefully for $([math]::Round($sleepSec/3600, 2)) hours until tomorrow ($($tomorrow.ToString('yyyy-MM-dd HH:mm:ss')))..." "INFO"
                while ((Get-Date) -lt $tomorrow) {
                    $rem = [int]($tomorrow - (Get-Date)).TotalSeconds
                    $chunk = [Math]::Min(300, $rem)
                    if ($chunk -le 0) { break }
                    Start-Sleep -Seconds $chunk
                }
            }
        }
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
