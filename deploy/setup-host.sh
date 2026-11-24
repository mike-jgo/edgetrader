#!/usr/bin/env bash
# One-time setup for the EdgeTrader homelab container (Debian 13).
# Run as root from a checkout of this repository:
#   sudo bash deploy/setup-host.sh
# Safe to run again. It does not register the GitHub runner or write Alpaca keys;
# see docs/DEPLOY.md for those steps.
set -euo pipefail

cd "$(dirname "$0")/.."

apt-get update
apt-get install -y --no-install-recommends python3 python3-venv tzdata curl ca-certificates sudo

# Service user: runs the bot. Its home holds config.ini, state, journal and log.
if ! id edgetrader >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/edgetrader --create-home --shell /usr/sbin/nologin edgetrader
fi
install -d -m 700 -o edgetrader -g edgetrader /var/lib/edgetrader/.edgetrader

# Runner user: runs GitHub Actions jobs and publishes releases. It never sees the keys.
if ! id runner >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash runner
fi
usermod -aG systemd-journal runner

# Releases and the shared virtualenv belong to the runner; the bot only reads them.
install -d -m 755 -o runner -g runner /srv/edgetrader /srv/edgetrader/releases
if [[ ! -x /srv/edgetrader/venv/bin/python ]]; then
    sudo -u runner python3 -m venv /srv/edgetrader/venv
fi
sudo -u runner /srv/edgetrader/venv/bin/pip install --quiet -r requirements.txt

# First release from this checkout, so the service can start before the first deploy.
if [[ ! -e /srv/edgetrader/current ]]; then
    release=/srv/edgetrader/releases/initial
    install -d -o runner -g runner "$release"
    install -m 644 -o runner -g runner edgetrader_bot.py edgetrader.py requirements.txt "$release/"
    sudo -u runner ln -sfn "$release" /srv/edgetrader/current
fi

# Bot arguments. Kept if already present.
install -d -m 755 /etc/edgetrader
if [[ ! -f /etc/edgetrader/bot.env ]]; then
    install -m 644 deploy/bot.env.example /etc/edgetrader/bot.env
fi

install -m 644 deploy/edgetrader-bot.service /etc/systemd/system/edgetrader-bot.service
systemctl daemon-reload

# The runner may restart the bot and nothing else.
cat > /etc/sudoers.d/edgetrader-runner <<'EOF'
runner ALL=(root) NOPASSWD: /usr/bin/systemctl restart edgetrader-bot
EOF
chmod 440 /etc/sudoers.d/edgetrader-runner
visudo -cf /etc/sudoers.d/edgetrader-runner

echo
echo "Host setup done. Next (see docs/DEPLOY.md):"
echo "  1. Put Alpaca paper keys in /var/lib/edgetrader/.edgetrader/config.ini"
echo "  2. systemctl enable --now edgetrader-bot"
echo "  3. Register the GitHub runner as user 'runner' with the label 'edgetrader'"
