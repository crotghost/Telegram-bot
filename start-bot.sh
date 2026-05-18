#!/bin/bash

# Watchdog: auto-restart the Telegram bot if it ever exits
while true; do
    echo "[watchdog] Starting Telegram bot..."
    python telegram-bot/bot.py || true
    echo "[watchdog] Bot exited. Restarting in 5s..."
    sleep 5
done
