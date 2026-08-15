@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Le bot n'est pas installe. Lance d'abord setup.bat.
  pause
  exit /b 1
)
if not exist .env (
  copy .env.example .env >nul
  echo Le fichier .env vient d'etre cree.
  echo Ajoute ton webhook Discord dedans, puis relance start.bat.
  pause
  exit /b 1
)
.venv\Scripts\python.exe bot.py
pause

