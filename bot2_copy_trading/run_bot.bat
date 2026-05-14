@echo off
:: Bot 2 - Copy Trading Bot
:: Called by Windows Task Scheduler once daily at 9:34 AM ET.

cd /d "C:\Users\madlabz\Documents\Trading\bot2_copy_trading"
call venv\Scripts\activate.bat
python copy_trading_bot.py
