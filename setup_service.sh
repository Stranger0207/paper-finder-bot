#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CURRENT_USER="$(whoami)"

echo "⚙️ Setting up systemd service for Paper Finder Bot..."

# Ensure venv exists
if [ ! -d "$DIR/venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv "$DIR/venv"
    "$DIR/venv/bin/pip" install --upgrade pip
    "$DIR/venv/bin/pip" install -r "$DIR/requirements.txt"
fi

SERVICE_FILE="/etc/systemd/system/paper-bot.service"

sudo bash -c "cat <<EOF > $SERVICE_FILE
[Unit]
Description=Paper Finder Telegram Bot
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python3 $DIR/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF"

sudo systemctl daemon-reload
sudo systemctl enable --now paper-bot

# Setup Git post-merge hook so 'git pull' automatically restarts the service
HOOK_FILE="$DIR/.git/hooks/post-merge"
mkdir -p "$DIR/.git/hooks"
cat << 'EOF' > "$HOOK_FILE"
#!/usr/bin/env bash
echo "🔄 Auto-restarting paper-bot service after git pull..."
sudo systemctl restart paper-bot
echo "✅ paper-bot restarted successfully!"
EOF
chmod +x "$HOOK_FILE"

echo "🎉 Setup complete! paper-bot is now running 24/7 as a background service."
echo "🔄 Every time you run 'git pull', it will automatically restart!"
sudo systemctl status paper-bot --no-pager
