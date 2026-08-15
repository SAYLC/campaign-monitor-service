@echo off
cd /d "%~dp0"
echo Installation du bot Whop...
python -m venv .venv
if errorlevel 1 goto :error
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --trusted-host pypi.org --trusted-host files.pythonhosted.org
if errorlevel 1 goto :error
python -m pip install -r requirements.txt --trusted-host pypi.org --trusted-host files.pythonhosted.org
if errorlevel 1 goto :error
if not exist .env copy .env.example .env >nul
echo.
echo Installation terminee.
echo Ouvre le fichier .env et remplace REMPLACE_MOI par ton webhook Discord.
pause
exit /b 0

:error
echo.
echo Une erreur est survenue pendant l'installation.
pause
exit /b 1
