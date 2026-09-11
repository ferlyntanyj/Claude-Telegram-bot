# Manual/local-testing helper only -- the scheduled production path is
# .github/workflows/major_news_alert.yml (runs every 20 min in the cloud, no
# dependency on this machine being on). Not registered in Task Scheduler.
$ErrorActionPreference = "Stop"

$PythonDir = "C:\Users\Ferlyn\AppData\Local\Programs\Python\Python312"
$env:Path = "$PythonDir;$PythonDir\Scripts;$env:Path"

$RepoRoot = "C:\Users\Ferlyn\Documents\FT Claude Code\Liquidity Momentum SGX"
$ScriptsDir = Join-Path $RepoRoot "scripts"
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("major_news_alert_{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HHmm"))

Start-Transcript -Path $LogFile -Append | Out-Null

try {
    Set-Location $ScriptsDir
    python major_news_alert.py
}
catch {
    Write-Host "ERROR: $_"
    throw
}
finally {
    Stop-Transcript | Out-Null
}
