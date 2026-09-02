$ErrorActionPreference = "Stop"

$PythonDir = "C:\Users\Ferlyn\AppData\Local\Programs\Python\Python312"
$env:Path = "$PythonDir;$PythonDir\Scripts;$env:Path"

$RepoRoot = "C:\Users\Ferlyn\Documents\FT Claude Code\Liquidity Momentum SGX"
$ScriptsDir = Join-Path $RepoRoot "scripts"
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("weekly_run_{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HHmm"))

Start-Transcript -Path $LogFile -Append | Out-Null

try {
    Set-Location $ScriptsDir
    python 01_get_universe.py
    python 02_fetch_history.py
    python 03_compute_screener.py
    python 04_build_workbook.py
    python 05_weekly_diff.py

    Set-Location $RepoRoot
    git add output/ history/
    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git commit -m "Weekly screener run: $(Get-Date -Format yyyy-MM-dd)"
        git push
        Write-Host "Committed and pushed updated results."
    } else {
        Write-Host "No changes to commit (output identical to last run)."
    }

    Set-Location $ScriptsDir
    python send_weekly_email.py
}
catch {
    Write-Host "ERROR: $_"
    throw
}
finally {
    Stop-Transcript | Out-Null
}
