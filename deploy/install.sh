#!/usr/bin/env bash
# Vexora bot — one-command install/update on a fresh Ubuntu/Debian server
# (Oracle Cloud Always Free, Google Cloud e2-micro, any VPS or home Linux machine).
#
#   curl -fsSL https://raw.githubusercontent.com/MeneryOLD/Crypto-assistant/main/deploy/install.sh | bash
#
# Run the same command again later to update the bot to the latest version.
# Needs no open ports: the bot only makes outgoing connections.
set -euo pipefail

REPO="${VEXORA_REPO:-https://github.com/MeneryOLD/Crypto-assistant.git}"
DIR="${VEXORA_DIR:-$HOME/vexora}"
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "Installing system packages (git, curl)"
sudo apt-get update -qq
sudo apt-get install -y -qq git curl ca-certificates >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker"
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo systemctl enable --now docker >/dev/null 2>&1 || true

# Small machines (e.g. 1 GB e2-micro) need swap to build the image and run comfortably.
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$mem_mb" -lt 2000 ] && ! swapon --show | grep -q .; then
  say "Adding 2 GB swap (this machine has ${mem_mb} MB RAM)"
  sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

if [ -d "$DIR/.git" ]; then
  say "Updating the bot in $DIR"
  git -C "$DIR" pull --ff-only
else
  say "Downloading the bot to $DIR"
  git clone --depth 1 "$REPO" "$DIR"
fi
cd "$DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  token="${BOT_TOKEN:-}"
  while [ -z "$token" ]; do
    read -rp "Paste your Telegram bot token (from @BotFather): " token </dev/tty
  done
  sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=${token}|" .env
  if [ -n "${ADMIN_IDS:-}" ]; then sed -i "s|^ADMIN_IDS=.*|ADMIN_IDS=${ADMIN_IDS}|" .env; fi
  chmod 600 .env
  say "Saved settings to $DIR/.env (edit it any time, then re-run this script)"
fi

mkdir -p data
sudo chown -R 1000:1000 data  # the container runs as user 1000

say "Building and starting the bot (first build takes a few minutes)"
sudo docker compose up -d --build
sudo docker image prune -f >/dev/null 2>&1 || true

say "Done! The bot runs in the background and starts automatically after a reboot."
cat <<EOF
  Logs:     sudo docker compose -f $DIR/compose.yaml logs -f --tail 50
  Status:   sudo docker compose -f $DIR/compose.yaml ps
  Restart:  sudo docker compose -f $DIR/compose.yaml restart
  Update:   run the install command again
  Data:     $DIR/data (database and logs)
EOF
