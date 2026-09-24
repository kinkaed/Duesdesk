$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run setup.ps1 once to create the Python environment.' }
Set-Location -LiteralPath (Join-Path $PSScriptRoot 'backend')
& $python serve.py
