# FORKED_Auto_job_applier_linkedIn — agent notes (all harnesses)

## Secrets handling — secure-vault is mandatory

Same global rules as `~/.claude/CLAUDE.md` and `~/AGENTS.md`:

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

## How the unattended chain behaves (2026-10-17)

- Chain: scheduled task -> `linkedin_keepalive.ps1` -> `run_linkedin_supervised.py` -> `run_linkedin_local.py` (the bot). Only that chain applies; agents never click Apply.
- Home-internet outage: a run that ends on a local-network error (`net::ERR_NAME_NOT_RESOLVED` and the like, see `linkedin_net.py`) makes the supervisor wait for
  www.linkedin.com (`logs/NETWORK_DOWN` while it waits, one toast after 30 min, 8 h cap) and relaunch without using its restart budget. Six such runs in a row with no
  saved application, or any other persistent breakage, still write `logs/NEEDS_HUMAN`, which stops the keepalive for the day: to restart, rename it to
  `NEEDS_HUMAN.<yyyy-mm-dd>.prev`. `python linkedin_net.py status` says whether LinkedIn is reachable.
- Answers to form questions: `use_AI = True` with `ai_provider = "proxy"` (`modules/ai/proxyConnections.py`: Gemini through a local Anthropic-style proxy on
  127.0.0.1:8080). The model answers only from a private knowledge file kept outside the repo (read through `modules/knowledge_answers.py`); "unknown" or any error
  falls back to the old behaviour. `logs/ai_answers_india.jsonl` and `logs/knowledge_gaps.jsonl` record what was asked; `python ai_answers_report.py` summarises them.
  Switch it off with `use_AI = False`.
- UAE pass (`linkedin_uae_overlay.py`): inert unless `logs/LINKEDIN_UAE_ARMED` is valid JSON with a future `until`. Arming is the owner's decision, never an agent's. Every live
  Submit re-reads that file. Two days before its end, and after its end, `LINKEDIN_UAE_ARMING_ENDS` / `LINKEDIN_UAE_ARMING_EXPIRED` appear in `logs/` with one toast a day.
