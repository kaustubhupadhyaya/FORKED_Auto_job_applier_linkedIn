# FORKED_Auto_job_applier_linkedIn — agent notes

## Secrets handling — secure-vault is mandatory

Same global rules as `~/.claude/CLAUDE.md`:

- Store: `%USERPROFILE%\.secrets\vault.dpapi` (DPAPI CurrentUser, ACL-locked).
- Library: `C:\Users\Admin\.agents\skills\secure-vault\vault.py`
- NEVER ask user to paste passwords/API keys in chat. NEVER echo secrets to shell/logs.
- Read in-process only: `import vault; vault.get_secret("<label>", "<field>")`
- Collect missing secrets via `python vault.py gui --request "..."`, OTPs via `prompt_gui.pyw --mode code`.
- If code uses `vault.get_secret()` cleanly, that is SAFE — do not lecture.

## This repo specifically

- `config/secrets.py`, `config/*.py` with real credentials, `.env`, `creds.env` MUST NEVER be committed with real values.
- This repo has leaked plaintext credentials to a public GitHub remote before. Treat any real password/API key found in a git-tracked file as an active incident: warn briefly once (no secret value in output), advise rotate + Private + history scrub.
- For local runs: use untracked `.env` or vault. `config/secrets.py` should contain placeholders / load from env only.
- Do NOT print `config/secrets.py` contents, logs in `logs/`, or `linkedin_*.log` if they may contain secrets.

## Time: IST only, on this whole computer (mandatory)

The user, 2026-10-21: "dont use UTC timing anywhere on this computer ... apply 1 common rule to use IST only."

- Every time you say, write, log, store, schedule, compare or show is IST (India Standard Time, UTC+05:30, this PC's own clock). Never UTC, GMT or a "Z" time.
- A day is an IST calendar day, midnight to midnight. When a service resets at another time of day, give that time in IST ("LinkedIn's count resets at 05:30 IST"), never as "00:00 UTC".
- A time you meet in UTC (a site's reset time, a cloud tool's log, an API field) is converted to IST before you use, show or store it. A time the user quotes is IST.
- Code written or edited here uses local time (`datetime.now()`, `Get-Date`) or `ZoneInfo("Asia/Kolkata")`. No `utcnow()`, `gmtime`, `-AsUTC`, `ToUniversalTime()`, `date -u`, `TZ=UTC`, and no `Z`-suffixed timestamps in logs, files, databases, dashboards, file names, schedules or messages.
- Only a third-party protocol that forces UTC (HTTP headers, JWT or cookie expiry, a GitHub Actions cron) may touch UTC, and only at that edge: convert on the way in, never store or show it.
