@echo off
REM ============================================================
REM  PMX to FBX Converter for Unreal Engine 4.27
REM  Drag a .pmx file onto this .bat to convert it,
REM  or double-click to launch the GUI.
REM ============================================================
setlocal

REM -- Find Python (python, then py launcher, then python3) --
set "PYEXE="
where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE where python3 >nul 2>nul && set "PYEXE=python3"

if not defined PYEXE (
  echo ERROR: Python was not found on PATH.
  echo Please install Python 3.8+ from https://www.python.org/downloads/
  echo and ensure it is added to PATH during installation.
  pause
  exit /b 1
)

REM -- Resolve the directory of this .bat so we can find main.py --
set "SCRIPT_DIR=%~dp0"

REM -- If a file was dragged onto this .bat, %1 is its path --
if "%~1"=="" (
  REM No argument: launch the GUI.
  %PYEXE% "%SCRIPT_DIR%main.py"
) else (
  REM Argument provided: convert directly. Support multiple files.
  :loop
  if "%~1"=="" goto done
  echo Converting: %~1
  %PYEXE% "%SCRIPT_DIR%main.py" "%~1"
  shift
  goto loop
  :done
  echo.
  echo All done. Press any key to close.
  pause >nul
)

endlocal
