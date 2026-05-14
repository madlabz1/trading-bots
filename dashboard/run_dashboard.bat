@echo off
cd /d "C:\Users\madlabz\Documents\Trading\dashboard"
call venv\Scripts\activate.bat
start http://localhost:5000
python app.py
