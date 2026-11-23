@echo off
REM DocHub 企业级知识库平台 — 双击一键启动入口（委托 PowerShell 执行 start.ps1）
chcp 65001 >nul
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 (
    echo.
    echo [DocHub] 启动过程中出现错误，请检查上方输出与 tmp\logs\ 日志文件。
    pause
    exit /b 1
)
endlocal
