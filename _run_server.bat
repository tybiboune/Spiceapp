@echo off
REM Internal helper launched by SpiceApp.bat in its own window — keeps the
REM server logs visible and this window open if the server crashes on
REM startup (e.g. port already in use), instead of it flashing shut.
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
python server.py

echo.
echo Le serveur s'est arrete.
pause >nul
