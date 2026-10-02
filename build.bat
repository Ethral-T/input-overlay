@echo off
rem Builds the Windows program with PyInstaller.
rem   build.bat           -> dist\InputOverlay\InputOverlay.exe  (folder; starts fast, fewer antivirus false positives)
rem   build.bat onefile   -> dist\InputOverlay.exe               (single file; slower to start)
cd /d "%~dp0"

rem A running copy locks its own files, and PyInstaller can't replace them ("Access is denied").
tasklist /fi "imagename eq InputOverlay.exe" 2>nul | find /i "InputOverlay.exe" >nul
if not errorlevel 1 (
    echo.
    echo Input Overlay is still running, so it can't be rebuilt.
    echo Right-click its tray icon ^(bottom-right, maybe under the ^^ arrow^) and choose Quit, then run build.bat again.
    goto :fail
)

if not exist .venv ( python -m venv .venv || goto :fail )
.venv\Scripts\python -m pip install -q -r requirements-dev.txt || goto :fail
if not exist lib\SDL3.dll ( echo lib\SDL3.dll is missing - see README. & goto :fail )
if not exist lib\libusb-1.0.dll ( echo lib\libusb-1.0.dll is missing - see README. & goto :fail )
.venv\Scripts\python appicon.py || goto :fail

set MODE=--onedir
if /i "%1"=="onefile" set MODE=--onefile

.venv\Scripts\python -m PyInstaller --noconfirm --clean %MODE% --noconsole --name InputOverlay ^
  --icon assets\icon.ico ^
  --add-data "overlay;overlay" --add-binary "lib\SDL3.dll;lib" --add-binary "lib\libusb-1.0.dll;lib" ^
  --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 --hidden-import pystray._win32 ^
  --exclude-module numpy --exclude-module scipy ^
  app.py || goto :fail

echo.
if /i "%1"=="onefile" ( echo Built dist\InputOverlay.exe ) else ( echo Built dist\InputOverlay\InputOverlay.exe )
goto :done

:fail
echo.
echo BUILD FAILED.
call :hold
exit /b 1

:done
exit /b 0

rem On failure, keep the window open when launched by double-click so the message can be read.
:hold
echo %cmdcmdline% | find /i "%~nx0" >nul && pause
exit /b 0
