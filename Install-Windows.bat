@echo off
REM Universal installer/launcher: works on ANY Windows computer, even one
REM that has never seen this app before. Copy this single file anywhere
REM (Desktop, USB key, cloud drive) and double-click it.
REM
REM First run on a given computer: downloads a fresh copy of the app from
REM GitHub into %USERPROFILE%\Spiceapp, then hands off to SpiceApp.bat there
REM (which sets up Python's virtual env + dependencies and starts the app).
REM Every run after that just re-launches the existing copy — from then on,
REM use the in-app "Update" button to pull new content, or just re-run this
REM installer to force a fresh download.
setlocal
set "TARGET=%USERPROFILE%\Spiceapp"
set "ZIP=%TEMP%\spiceapp_download.zip"
set "EXTRACT=%TEMP%\spiceapp_extract"

where python >nul 2>nul
if errorlevel 1 (
    echo Python n'est pas installe sur cet ordinateur.
    echo Ouverture de la page de telechargement...
    start "" "https://www.python.org/downloads/"
    echo Installe Python ^(coche bien la case "Add python.exe to PATH"^),
    echo puis relance ce fichier.
    pause
    exit /b 1
)

if not exist "%TARGET%\server.py" (
    echo Premiere installation sur cet ordinateur.
    echo Telechargement de l'application depuis GitHub...

    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Invoke-WebRequest -Uri 'https://github.com/tybiboune/Spiceapp/archive/refs/heads/main.zip' -OutFile '%ZIP%'"
    if errorlevel 1 (
        echo Le telechargement a echoue. Verifie ta connexion internet et relance ce fichier.
        pause
        exit /b 1
    )

    if exist "%EXTRACT%" rmdir /s /q "%EXTRACT%"
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Expand-Archive -Path '%ZIP%' -DestinationPath '%EXTRACT%' -Force"

    if exist "%TARGET%" rmdir /s /q "%TARGET%"
    move "%EXTRACT%\Spiceapp-main" "%TARGET%" >nul

    rmdir /s /q "%EXTRACT%" 2>nul
    del "%ZIP%" 2>nul

    echo Installation terminee dans %TARGET%
)

call "%TARGET%\SpiceApp.bat"
exit /b 0
