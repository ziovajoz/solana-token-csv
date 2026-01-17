@echo off
setlocal

REM --- Go to repo root ---
cd /d "C:\Users\phili\OneDrive\Documents\GitHub\solana-token-csv" || exit /b 1

REM --- Make sure we're on main ---
git rev-parse --is-inside-work-tree >nul 2>&1 || exit /b 1
git checkout main >nul 2>&1

REM --- Refresh index (helps on Windows/OneDrive sometimes) ---
git update-index -q --refresh

REM --- If no changes, exit quietly ---
git diff --quiet && git diff --cached --quiet && exit /b 0

REM --- Stage only the files we care about ---
git add "output/*.csv" "candidate_cache.csv" >nul 2>&1

REM --- If staging resulted in nothing, exit ---
git diff --cached --quiet && exit /b 0

REM --- Commit with timestamp ---
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH:mm"') do set TS=%%i
git commit -m "Auto update shortlists %TS%" >nul 2>&1

REM --- Push ---
git push origin main >nul 2>&1

endlocal
exit /b 0
