@echo off
rem Watches MTGO and generates reviews automatically when each match ends (Ctrl+C to stop).
cd /d "%~dp0"
py -m mtgo_replay --watch
