@echo off
REM Double-click launcher: sets up the app on first run (virtual env +
REM dependencies + database), then starts the server and opens the browser.
REM Works on this computer once the app has been downloaded here — for a
REM brand new computer, use Install-Windows.bat instead, which grabs a copy
REM of the app then calls this file automatically.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python n'est pas installe ou pas dans le PATH sur cet ordinateur.
    echo Ouverture de la page de telechargement...
    start "" "https://www.python.org/downloads/"
    echo Installe Python ^(coche bien la case "Add python.exe to PATH"^),
    echo puis relance ce fichier.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Premiere installation, mise en place de l'environnement...
    python -m venv .venv
)

call ".venv\Scripts\activate.bat"
python -m pip install -q --disable-pip-version-check -r requirements.txt

if not exist "database.db" (
    echo Creation de la base de donnees...
    python setup_database.py
)

echo Demarrage du serveur...
start "Spiceapp server" "%~dp0_run_server.bat"

REM Give the server a moment to actually bind to the port before opening
REM the browser, so the first load doesn't hit a connection error.
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:5000/"

exit /b 0
