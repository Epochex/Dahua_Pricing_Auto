$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
  py -3 -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium

if (-not (Test-Path "config.json")) {
  Copy-Item "config.example.json" "config.json"
  Write-Host "Created config.json. Edit server_url and agent_token before running."
}

Write-Host "Install complete."
