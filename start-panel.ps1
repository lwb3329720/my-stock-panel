$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Install backend dependencies first.' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend\dist\index.html'))) { throw 'Build frontend first.' }
if (Get-NetTCPConnection -LocalPort 3018 -State Listen -ErrorAction SilentlyContinue) {
    Write-Output 'Port 3018 is already in use. No process was stopped.'
    exit 1
}
# Python platform.machine() reads this Windows variable; Codex child environments may omit it.
if (-not $env:PROCESSOR_ARCHITECTURE -and [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq 'X64') {
    $env:PROCESSOR_ARCHITECTURE = 'AMD64'
}
$logDir = Join-Path $projectRoot 'data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$process = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '3018', '--env-file', '../.env') -WorkingDirectory (Join-Path $projectRoot 'backend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir 'panel.out.log') -RedirectStandardError (Join-Path $logDir 'panel.err.log')
$process.Id | Set-Content -LiteralPath (Join-Path $logDir 'panel.pid')
Write-Output "Started PID $($process.Id): http://127.0.0.1:3018"
