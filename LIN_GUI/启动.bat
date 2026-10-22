@echo off
rem ---------------------------------------------------------------
rem  LIN bus monitor launcher (double-click to run)
rem  Finds a Python that already has PyQt5 + pyserial, then
rem  starts the GUI. All output here is ASCII on purpose: the
rem  Chinese error text is shown by main.py via a native Win32
rem  message box, which has no console encoding issues.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PYEXE="
set "PYWEXE="

rem --- known interpreters first (fast, deterministic) ---
call :probe "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :probe "C:\ProgramData\anaconda3\python.exe"
call :probe "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :probe "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"

rem --- then anything on PATH ---
if not defined PYEXE for /f "delims=" %%P in ('where python 2^>nul') do call :probe "%%P"

if not defined PYEXE goto :nodeps

echo   Using: %PYEXE%
if defined PYWEXE (
  start "" "%PYWEXE%" "%~dp0main.py"
) else (
  start "" "%PYEXE%" "%~dp0main.py"
)
exit /b 0

:nodeps
echo.
echo   [ERROR] No Python with PyQt5 + pyserial was found.
echo.
echo   Install the dependencies first:
echo       python -m pip install -r requirements.txt
echo.
echo   Details: %~dp0logs\app.log
echo.
pause
exit /b 1

rem --- probe: if %1 is a python that has both deps, remember it ---
:probe
if "%~1" == ""        exit /b 0
if not exist "%~1"   exit /b 0
if defined PYEXE     exit /b 0
"%~1" -c "import PyQt5, serial" >nul 2>&1
if errorlevel 1      exit /b 0
set "PYEXE=%~1"
set "PYWEXE=%~dp1pythonw.exe"
if not exist "%PYWEXE%" set "PYWEXE="
exit /b 0
