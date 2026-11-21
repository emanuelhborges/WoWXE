@echo off
rem Opens the WoWXE launcher (no console window). The first run installs the Python packages it needs.
cd /d "%~dp0"
where python >nul 2>nul && (set PY=python) || (set PY=py -3)
%PY% -m pip install -q -r companion\requirements.txt >nul 2>nul
where pythonw >nul 2>nul && (start "" pythonw WoWXE.pyw) || (start "" pyw -3 WoWXE.pyw)
