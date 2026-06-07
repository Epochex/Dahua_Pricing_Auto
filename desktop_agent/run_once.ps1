$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
.\.venv\Scripts\python.exe .\gsp_status_agent.py --config .\config.json --once
