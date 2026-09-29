@echo off
chcp 65001 >nul
setlocal

echo === Product Idea Radar 설치 ===
echo GitHub 릴리스에서 최신 실행 파일을 내려받아 설치합니다.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*

echo.
pause
