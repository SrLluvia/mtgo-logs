@echo off
rem Opens the web viewer for the generated reviews (Ctrl+C to stop).
cd /d "%~dp0"
py -m mtgo_replay --serve
