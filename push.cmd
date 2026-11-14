@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    echo usage: push.cmd "commit message"
    exit /b 2
)

set "MSG=%*"

echo [1/3] git add -A
git add -A
if errorlevel 1 exit /b 1

git diff --cached --quiet
if errorlevel 1 (
    echo [2/3] git commit -m "%MSG%"
    git commit -m "%MSG%"
    if errorlevel 1 exit /b 1
) else (
    echo [2/3] no changes, skip commit
)

echo [3/3] git push
git push
