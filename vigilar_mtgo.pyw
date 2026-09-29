"""Runs the MTGO watcher silently (no console window). Log: data/watch.log"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mtgo_replay.__main__ import main

main(["--watch"])
