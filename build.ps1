# TradeDesk AI (Phase 3) - Windows version of the Makefile.
# Usage: .\build.ps1 <target>     e.g.  .\build.ps1 install   .\build.ps1 test   .\build.ps1 dashboard
# If PowerShell blocks the script: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
param([string]$Target = "help")
$ErrorActionPreference = "Stop"

$Venv = ".venv"
$Py = "$Venv\Scripts\python.exe"
$Phase2Ref = "9696262"   # last Phase 2 commit; code reports cover only what changed after it
$ReportDir = "eval\phase3"

function Ensure-Venv {
    if (-not (Test-Path $Py)) { python -m venv $Venv }
}

switch ($Target) {
    "install" {
        Ensure-Venv
        & $Py -m pip install --upgrade pip
        & $Py -m pip install -e ".[dashboard,mcp,e3]" pytest ruff lizard
        if (-not (Test-Path ".env")) {
            Copy-Item ".env.example" ".env"
            Write-Host "Created .env from .env.example: add your GOOGLE_API_KEY (and Alpaca paper keys for trading)."
        }
    }
    "test"      { Ensure-Venv; & $Py -m pytest -q }
    "lint"      { Ensure-Venv; & $Py -m ruff check tradingagents cli }
    "doctor"    { Ensure-Venv; & $Py -m tradingagents.doctor }
    "cli"       { Ensure-Venv; & "$Venv\Scripts\tradingagents.exe" }
    "dashboard" { Ensure-Venv; & "$Venv\Scripts\tradingagents-dashboard.exe" }
    "mcp"       { Ensure-Venv; & "$Venv\Scripts\tradingagents-mcp.exe" }
    "analyze" {
        Ensure-Venv
        New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
        git diff --name-only --diff-filter=AM $Phase2Ref HEAD -- 'tradingagents/*.py' 'tradingagents/**/*.py' 'cli/*.py' |
            Set-Content "$ReportDir\new_files.txt"
        $files = Get-Content "$ReportDir\new_files.txt"
        cloc --by-file "--list-file=$ReportDir\new_files.txt" | Set-Content "$ReportDir\cloc.txt"
        & "$Venv\Scripts\lizard.exe" @files | Set-Content "$ReportDir\lizard.txt"
        & "$Venv\Scripts\lizard.exe" -C 10 -w @files | Set-Content "$ReportDir\lizard_warnings.txt"
        Write-Host "Reports written to $ReportDir\"
    }
    "docker" { docker compose run --rm tradingagents }
    "clean" {
        Remove-Item -Recurse -Force build, dist, *.egg-info, .pytest_cache, .ruff_cache -ErrorAction SilentlyContinue
        Get-ChildItem -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
    }
    default {
        Write-Host "Targets: install, test, lint, doctor, cli, dashboard, mcp, analyze, docker, clean"
    }
}
