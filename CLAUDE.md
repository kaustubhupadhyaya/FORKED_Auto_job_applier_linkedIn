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
