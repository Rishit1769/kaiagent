@echo off
REM â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
REM Kai Agent Windows â€” one-click build script
REM
REM Produces:  dist\Kai Agent\Kai Agent.exe   (portable folder)
REM            Setup-Kai-Agent.exe         (if Inno Setup is installed)
REM
REM Usage:  build.bat           â† builds portable folder only
REM         build.bat installer â† also builds Setup-Kai-Agent.exe
REM â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ================================================================
echo   Kai Agent for Windows â€” Build
echo ================================================================
echo.

REM â”€â”€ 1. Sanity check Python â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found on PATH. Install Python 3.11+ first.
    exit /b 1
)

REM â”€â”€ 2. Install build deps if missing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
echo [1/4] Checking build dependencies...
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo     Installing PyInstaller...
    python -m pip install --quiet --upgrade pyinstaller
)
python -c "import PyQt6" 2>nul
if errorlevel 1 (
    echo     Installing project requirements...
    python -m pip install --quiet -r requirements.txt
)

REM â”€â”€ 2b. Generate icon if missing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
if not exist "assets\icon.ico" (
    echo     Generating default icon...
    python "assets\make_icon.py"
)

REM â”€â”€ 3. Clean old build â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
echo [2/4] Cleaning old build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM â”€â”€ 4. Run PyInstaller â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
echo [3/4] Building with PyInstaller (this takes 2-5 min)...
python -m PyInstaller kai_agent.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. Check the output above.
    exit /b 1
)

REM â”€â”€ 5. Copy .env.example and LICENSE into the dist folder â”€â”€â”€â”€â”€â”€â”€â”€â”€
echo [4/4] Bundling docs and env template...
copy /y ".env.example" "dist\Kai Agent\.env.example" >nul
copy /y "LICENSE"       "dist\Kai Agent\LICENSE"      >nul
copy /y "README.md"     "dist\Kai Agent\README.md"    >nul

echo.
echo ================================================================
echo   Portable build complete!
echo   Run:  dist\Kai Agent\Kai Agent.exe
echo ================================================================
echo.

REM â”€â”€ 6. Optional: build Inno Setup installer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
if /i "%1"=="installer" (
    echo Building Inno Setup installer...
    where iscc >nul 2>&1
    if errorlevel 1 (
        set "ISCC=C:\Program Files ^(x86^)\Inno Setup 6\ISCC.exe"
        if not exist "!ISCC!" (
            echo [WARN] Inno Setup not found. Install from https://jrsoftware.org/isdl.php
            echo        Then re-run:  build.bat installer
            exit /b 0
        )
        "!ISCC!" installer.iss
    ) else (
        iscc installer.iss
    )
    echo.
    echo Installer: dist\Setup-Kai-Agent.exe
)

endlocal

