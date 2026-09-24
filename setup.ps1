$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
Write-Host 'Requires Python 3.12+, Node 22.18+, pnpm, SQL Server and ODBC Driver 18.'
if (-not (Test-Path '.venv\Scripts\python.exe')) { python -m venv .venv; if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' } }
& '.venv\Scripts\python.exe' -m pip install -r backend/requirements.lock
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
pnpm install --frozen-lockfile
if ($LASTEXITCODE -ne 0) { throw 'React dependency installation failed.' }
pnpm run build
if ($LASTEXITCODE -ne 0) { throw 'React build failed.' }
& '.venv\Scripts\python.exe' backend/manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw 'Database migration failed. Create the configured database first.' }
& '.venv\Scripts\python.exe' backend/manage.py collectstatic --noinput
if ($LASTEXITCODE -ne 0) { throw 'Static asset collection failed.' }
Write-Host 'Setup complete. Run start.ps1, then open http://127.0.0.1:8765/.'
