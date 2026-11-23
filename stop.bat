@echo off
REM DocHub 企业级知识库平台 — 双击一键停止入口（委托 PowerShell 执行 stop.ps1）
chcp 65001 >nul
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
if errorlevel 1 (
    echo.
    echo [DocHub] 停止过程中出现错误，请检查上方输出。
    pause
    exit /b 1
)
endlocal
