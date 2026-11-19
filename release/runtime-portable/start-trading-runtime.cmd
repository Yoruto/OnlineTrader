@echo off
setlocal
"%~dp0OnlineTraderCLI.exe" runtime run --root "%~dp0runtime" %*
exit /b %ERRORLEVEL%
