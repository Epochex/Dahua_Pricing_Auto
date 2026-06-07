# Dahua GSP Desktop Agent

This agent runs on a Windows machine that can access GSP. It only checks PLA status and posts the result back to the Linux backend. It does not run pricing and does not send DingTalk/Huachat messages.

Default mode is `gsp_query_mode: "api"`: the agent logs in through the same GSP frontend OAuth endpoint and reads the PLA list API. This avoids browser DevTools automation, which GSP may block.

The fixed GSP API contract is documented in `GSP_API_PLAYBOOK.md`.

## Why This Does Not Steal Your Mouse Or Keyboard

- Default status-check mode is API-only, so no browser window is needed.
- Browser fallback mode uses `headless: true`, so Edge runs without a visible window.
- Playwright sends input to the browser process, not to the Windows desktop cursor/keyboard.
- Browser fallback uses a dedicated Edge profile directory, for example `C:/DahuaPricingAgent/edge-profile`.

If GSP blocks headless browser sessions, set `headless` to `false` only for debugging. For always-on production without disturbing your main screen, run this agent under a separate Windows user session, a small Windows VM, or a dedicated office mini PC.

## Install On Windows

Open PowerShell in this directory:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

Edit `config.json`:

- `server_url`: backend URL reachable from Windows. Prefer Tailscale or LAN URL, for example `http://<server-tailscale-ip>:8000`.
- `agent_token`: the same token stored on the server at `/data/dahua_pricing_runtime/agent/sheet_push_token.txt`.
- `gsp_query_mode`: keep `api` unless you explicitly need browser debugging.
- `gsp_username` / `gsp_password`: GSP account for local API login. Never commit `config.json`.
- `gsp_country_code`: country code used to query PLA status, for example `FR`.
- `headless`: keep `true` after the login profile is prepared.
- `max_tasks`: number of PLA rows to test per run.

## First Login

```powershell
.\login.ps1
```

In API mode this stores `gsp_auth.local.json` with an OAuth token. In browser fallback mode this opens a dedicated Edge profile and stores session cookies in `user_data_dir`.

## Run One Test

```powershell
.\run_once.ps1
```

To test in safer stages:

```powershell
# Only verify backend URL, token, and pending PLA queue. Does not open GSP.
.\.venv\Scripts\python.exe .\gsp_status_agent.py --config .\config.json --queue-only

# Open/query GSP but do not POST the result back to the backend.
.\.venv\Scripts\python.exe .\gsp_status_agent.py --config .\config.json --once --no-push

# Query one PLA directly, including approval current step/taskers when available.
.\.venv\Scripts\python.exe .\gsp_status_agent.py --config .\config.json --pla PLA20260602141029254
```

The flow is:

```text
POST /api/agent/gsp/status-queue
  -> get rows where J/PLA has value and L/status is not completed
  -> query GSP PLA status by API
  -> POST /api/agent/gsp/status-result
  -> server saves JSON under /data/dahua_pricing_runtime/agent/gsp_status/
  -> if GSP status is Approved, server queues an online-sheet update for L{row}=已完成
```

## Online Sheet Write-Back

The backend can decide which cells should change, but it cannot edit the Alidocs/DingTalk online spreadsheet unless something with spreadsheet write permission executes the update.

Current lightweight flow:

1. `script/alidocs_push_all_sheets.js` runs inside the online spreadsheet and pushes the latest sheet rows to the backend.
2. The Windows agent checks queued PLA rows and posts GSP results back.
3. When a result is `Approved`, the backend creates a pending sheet update:

```text
POST /api/agent/sheet/status-updates/pending
POST /api/agent/sheet/status-updates/ack
```

4. `script/alidocs_apply_status_updates.js` runs inside the online spreadsheet, writes `L{row_index}=已完成`, then ACKs the update.

So yes, the backend needs the new endpoints deployed, but the actual online-sheet cell modification is done by the Alidocs-side script unless you later add an official DingTalk/Alidocs OpenAPI writer on the server.

## Always-On Later

For a stable background setup, create a Windows Task Scheduler task that runs:

```powershell
powershell.exe -ExecutionPolicy Bypass -File C:\path\to\desktop_agent\run_once.ps1
```

Schedule it every 5-10 minutes. If you need continuous looping instead, set `poll_interval_seconds` in `config.json` and run `gsp_status_agent.py` without `--once`.
