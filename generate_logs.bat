@echo off
rem Generates reviews for the 10 most recent MTGO matches into the "output" folder.
cd /d "%~dp0"
py -m mtgo_replay --last 10
pause
