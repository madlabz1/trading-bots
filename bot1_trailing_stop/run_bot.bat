@echo off
:: Bot 1 - Trailing Stop Bot
:: This file is called by Windows Task Scheduler every 5 minutes.
:: It activates the virtual environment and runs the bot.

cd /d "C:\Users\madlabz\Documents\Trading\bot1_trailing_stop"
call venv\Scripts\activate.bat
python trailing_stop_bot.py
