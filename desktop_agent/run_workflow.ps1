$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
& .\.venv\Scripts\python.exe .\pricing_workflow_agent.py --config .\config.json
