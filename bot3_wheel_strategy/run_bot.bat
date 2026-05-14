@echo off
:: Bot 3 - Wheel Strategy Bot
:: Called by Windows Task Scheduler every 15 minutes, Mon-Fri 9:34 AM - 4:00 PM ET

cd /d "C:\Users\madlabz\Documents\Trading\bot3_wheel_strategy"
call venv\Scripts\activate.bat
python wheel_bot.py
