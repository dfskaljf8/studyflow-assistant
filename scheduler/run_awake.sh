#!/bin/bash
# Wrapper that prevents Mac from sleeping while StudyFlow scheduler runs.
# caffeinate -i = prevent idle sleep
# caffeinate -s = prevent system sleep (on AC power)
cd "$(dirname "$0")/.."
exec caffeinate -is ./venv/bin/python main.py schedule
