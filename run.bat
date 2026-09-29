@echo off
cd /d %~dp0
if not exist instance\olc.db python seed.py
python app.py
