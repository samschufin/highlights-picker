@echo off
REM Double-click for 1 NBA + 1 NFL pick. Or run: picks.bat --minutes 60
if "%~1"=="" (python "%~dp0picks.py" --nba 1 --nfl 1 --html) else (python "%~dp0picks.py" %* --html)
start "" "%~dp0picks.html"
pause
