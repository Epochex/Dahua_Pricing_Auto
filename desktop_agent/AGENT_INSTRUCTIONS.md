# Windows GSP Desktop Agent Instructions

Use this document as the handoff prompt for the next Codex/desktop agent that continues development on Windows.

## Objective

Build and test a Windows-side desktop agent that checks GSP PLA application status from the rows pushed by the DingTalk/Alidocs online spreadsheet.

The current scope is status checking only:

- Pull pending PLA rows from the Linux backend.
- Query GSP status through the GSP frontend API by `priceListApplicationId`.
- Use browser automation only as a fallback/debug path because GSP may block DevTools-controlled browsers.
- Post the status result back to the backend.
- If GSP returns `Approved`, the backend queues `L{row_index}=已完成`; `script/alidocs_apply_status_updates.js` must run inside the online spreadsheet to apply and ACK it.

Do not run pricing calculations. Do not submit or edit GSP applications. Do not send Huachat/DingTalk group messages during testing.

## Current Backend Contract

The backend is in `/data/Dahua_Pricing_Auto`.

The Windows agent should use:

```text
POST /api/agent/gsp/status-queue
POST /api/agent/gsp/status-result
POST /api/agent/sheet/status-updates/pending
POST /api/agent/sheet/status-updates/ack
```

For the first Windows test, the configured server URL is:

```text
http://100.96.202.40:18081
```

This is a Tailscale-only nginx entry. It should expose the GSP desktop-agent endpoints and the sheet status update endpoints above.

Both requests require the shared `agent_token`. On the Linux server this token is stored at:

```text
/data/dahua_pricing_runtime/agent/sheet_push_token.txt
```

Never commit the real token.

## Queue Rule

The backend parser already filters rows from the latest Alidocs push.

Rows enter the GSP status queue when:

- Column J / `PLA NO.` has a value.
- Column L / `状态` is not completed.
- A valid `PLA...` number can be extracted.

The known smoke-test row is:

```text
sheet: 2026.06
row_index: 23
pla_no: PLA20260605143508433
pn: 1.0.01.15.12158
sheet_status: 进行中
```

## Windows Agent Files

```text
desktop_agent/
  gsp_status_agent.py
  config.example.json
  install.ps1
  login.ps1
  run_once.ps1
  README.md
  GSP_API_PLAYBOOK.md
```

Install:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

Create/edit `config.json`:

```json
{
  "server_url": "http://100.96.202.40:18081",
  "agent_token": "PASTE_REAL_TOKEN_HERE",
  "gsp_url": "https://gsp.dahuasecurity.com/#/pricing/application/list",
  "gsp_base_url": "https://gsp.dahuasecurity.com",
  "gsp_query_mode": "api",
  "gsp_username": "PASTE_GSP_USERNAME_HERE",
  "gsp_password": "PASTE_GSP_PASSWORD_HERE",
  "gsp_country_code": "FR",
  "gsp_auth_path": "gsp_auth.local.json",
  "edge_channel": "msedge",
  "user_data_dir": "C:/DahuaPricingAgent/edge-profile",
  "headless": true,
  "max_tasks": 5,
  "poll_interval_seconds": 0,
  "dry_run": false
}
```

First login:

```powershell
.\login.ps1
```

In API mode, this logs in through `/dahua-b-usercenter/oauth/token` and stores `gsp_auth.local.json`. Browser/Edge login is only a fallback.

Run one status-check pass:

```powershell
.\run_once.ps1
```

Safer staged tests:

```powershell
# Backend queue/token only. Does not open GSP.
.\.venv\Scripts\python.exe .\gsp_status_agent.py --config .\config.json --queue-only

# Query GSP but do not write the result back to the backend.
.\.venv\Scripts\python.exe .\gsp_status_agent.py --config .\config.json --once --no-push

# Query one PLA directly and print status/current step/taskers.
.\.venv\Scripts\python.exe .\gsp_status_agent.py --config .\config.json --pla PLA20260602141029254
```

## Non-Interference Requirement

The agent must not steal the user's main mouse or keyboard.

Preferred approach:

- Use Playwright with a dedicated Edge profile.
- Keep `headless=true` after login is prepared.
- Interact with DOM elements, not OS-level mouse/keyboard automation.

If GSP blocks headless mode:

- Debug with `headless=false`.
- For production, run on a separate Windows user session, Windows VM, or dedicated office mini PC.
- Avoid relying on the user's active desktop session for unattended operation.

## Suggested Prompt For The Next Agent

```text
You are continuing the Dahua GSP Desktop Agent on Windows.

Repository branch contains `desktop_agent/`.
Goal: test and improve `desktop_agent/gsp_status_agent.py` so it can query GSP PLA status without modifying GSP data or sending group notifications.

Constraints:
- Do not run pricing calculation.
- Do not submit, approve, edit, or create anything in GSP.
- Do not send Huachat/DingTalk group messages during testing.
- Do not commit real tokens, cookies, screenshots with private data, or local Edge profiles.
- Use Playwright + dedicated Edge profile. Avoid OS-level mouse/keyboard automation.
- Keep the user’s main desktop usable; default to headless/background operation.

Backend:
- server_url: http://100.96.202.40:18081
- queue endpoint: POST /api/agent/gsp/status-queue
- result endpoint: POST /api/agent/gsp/status-result
- token comes from the user/server runtime, never from git.

First target row:
- sheet 2026.06 row 23
- PLA20260605143508433
- expected current GSP result observed manually: Approved

Work steps:
1. Install dependencies with `install.ps1`.
2. Put the real token into local `config.json`.
3. Run `--queue-only` first and confirm the target PLA row appears.
4. Run `login.ps1` once to persist the API OAuth token in `gsp_auth.local.json`.
5. Run `--once --no-push` and confirm status extraction in stdout.
6. If API status extraction fails, inspect the GSP frontend API payloads before using browser fallback.
7. Run `run_once.ps1` only after the no-push query looks correct.
8. Report stdout and the backend saved result path.

Read `desktop_agent/GSP_API_PLAYBOOK.md` before changing GSP selectors or API payloads.
```
