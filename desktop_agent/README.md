# Dahua GSP Desktop Agent

This agent runs on a Windows machine that can access the GSP intranet site. It only checks PLA status and posts the result back to the Linux backend. It does not run pricing and does not send DingTalk/Huachat messages.

## Why This Does Not Steal Your Mouse Or Keyboard

- Default mode is `headless: true`, so Edge runs without a visible window.
- Playwright sends input to the browser process, not to the Windows desktop cursor/keyboard.
- The agent uses a dedicated Edge profile directory, for example `C:/DahuaPricingAgent/edge-profile`.
- The only interactive step is first login: run `login.ps1`, log in once in the dedicated Edge window, then close/press Enter.

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
- `headless`: keep `true` after the login profile is prepared.
- `max_tasks`: number of PLA rows to test per run.

## First Login

```powershell
.\login.ps1
```

This opens a dedicated Edge profile. Log in to GSP normally. The session cookies stay in `user_data_dir`.

## Run One Test

```powershell
.\run_once.ps1
```

The flow is:

```text
POST /api/agent/gsp/status-queue
  -> get rows where J/PLA has value and L/status is not completed
  -> query GSP PLA NO.
  -> POST /api/agent/gsp/status-result
  -> server saves JSON under /data/dahua_pricing_runtime/agent/gsp_status/
```

## Always-On Later

For a stable background setup, create a Windows Task Scheduler task that runs:

```powershell
powershell.exe -ExecutionPolicy Bypass -File C:\path\to\desktop_agent\run_once.ps1
```

Schedule it every 5-10 minutes. If you need continuous looping instead, set `poll_interval_seconds` in `config.json` and run `gsp_status_agent.py` without `--once`.
