@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Ensure core Windows tools are on PATH (fixes missing findstr/where).
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%PATH%"

set "APP_VERSION=unknown"
for /f "usebackq delims=" %%v in (`python -c "from palworld_manager.constants import APP_VERSION; print(APP_VERSION)"`) do set "APP_VERSION=%%v"
if "%APP_VERSION%"=="unknown" (
  echo Could not read APP_VERSION from palworld_manager.constants
  pause
  exit /b 1
)

echo Installing / updating build dependencies...
python -m pip install --upgrade pip
python -m pip install "pyinstaller>=6.0" "psutil>=5.9.0"
if errorlevel 1 (
  echo Failed to install PyInstaller / psutil.
  pause
  exit /b 1
)

set "PYINSTALLER_CMD=python -m PyInstaller"

if not exist "build" mkdir "build"

echo Building release executable...
call %PYINSTALLER_CMD% --noconfirm --clean --windowed --onedir --name "Palworld-Dedicated-Server-Manager" --icon "palworld_logo.ico" --add-data "palworld_logo.ico;." "run_manager.py"
if errorlevel 1 (
  echo Release build failed.
  pause
  exit /b 1
)

echo Building debug executable...
call %PYINSTALLER_CMD% --noconfirm --clean --console --onedir --debug=all --name "Palworld-Dedicated-Server-Manager-Debug" --icon "palworld_logo.ico" --add-data "palworld_logo.ico;." "run_manager.py"
if errorlevel 1 (
  echo Debug build failed.
  pause
  exit /b 1
)

set "ZIP_NAME=Palworld-Dedicated-Server-Manager-v%APP_VERSION%.zip"
if exist "build\%ZIP_NAME%" del /f /q "build\%ZIP_NAME%"
python -c "import shutil; shutil.make_archive(r'build\Palworld-Dedicated-Server-Manager-v%APP_VERSION%', 'zip', r'dist\Palworld-Dedicated-Server-Manager')"
if errorlevel 1 (
  echo Failed to create release zip.
  pause
  exit /b 1
)

set "DEBUG_ZIP_NAME=Palworld-Dedicated-Server-Manager-Debug-v%APP_VERSION%.zip"
if exist "build\%DEBUG_ZIP_NAME%" del /f /q "build\%DEBUG_ZIP_NAME%"
python -c "import shutil; shutil.make_archive(r'build\Palworld-Dedicated-Server-Manager-Debug-v%APP_VERSION%', 'zip', r'dist\Palworld-Dedicated-Server-Manager-Debug')"
if errorlevel 1 (
  echo Failed to create debug zip.
  pause
  exit /b 1
)

echo.
echo Build complete.
echo Version: %APP_VERSION%
echo Zip: build\%ZIP_NAME%
echo Debug Zip: build\%DEBUG_ZIP_NAME%
pause
