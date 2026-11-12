@echo off
setlocal
chcp 65001 >nul
if not "%~1"=="" (
    start "" "%~dp0OnlineTrader.exe" --compare-fills "%~f1"
) else (
    start "" "%~dp0OnlineTrader.exe" --compare-fills
)
