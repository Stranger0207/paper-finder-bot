#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "🔄 Pulling latest changes from GitHub..."
git pull

echo "📦 Updating python dependencies..."
if [ -d "$DIR/venv" ]; then
    "$DIR/venv/bin/pip" install -r "$DIR/requirements.txt" --quiet
fi

if systemctl is-active --quiet paper-bot; then
    echo "🚀 Restarting paper-bot service..."
    sudo systemctl restart paper-bot
    echo "✅ Paper Finder Bot is up and running!"
    sudo systemctl status paper-bot --no-pager
else
    echo "⚠️ Systemd service not found. Restarting directly..."
    pkill -f "main.py" || true
    sleep 1
    nohup "$DIR/venv/bin/python3" "$DIR/main.py" > "$DIR/bot.log" 2>&1 &
    echo "✅ Bot started in background (PID: $!)"
fi
