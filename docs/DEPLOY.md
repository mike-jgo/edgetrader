# Deploying The Bot To The Homelab

The bot runs as a systemd service in its own Proxmox LXC container, trading the Alpaca **paper** account. GitHub Actions tests every push and pull request. A push to `main` also deploys to the container through a self-hosted runner that runs there.

```text
push to main
  -> test job (GitHub-hosted, ubuntu-latest): compile + unit tests
  -> deploy job (self-hosted runner in the container, label "edgetrader"):
       pip install into /srv/edgetrader/venv
       copy files to /srv/edgetrader/releases/<sha>
       point /srv/edgetrader/current at it
       restart edgetrader-bot and check it stays up
```

Pull requests only run the test job.

## Layout In The Container

| Path | Owner | What |
| --- | --- | --- |
| `/srv/edgetrader/releases/<sha>` | `runner` | One folder per deploy; the newest five are kept |
| `/srv/edgetrader/current` | `runner` | Symlink to the running release |
| `/srv/edgetrader/venv` | `runner` | Python virtualenv shared by all releases |
| `/etc/edgetrader/bot.env` | root | Bot arguments (`BOT_ARGS`) |
| `/var/lib/edgetrader/.edgetrader/` | `edgetrader` | `config.ini` (keys), `bot_state.json`, `journal.csv`, `bot.log` |

Two users keep the keys apart from CI:

- `edgetrader` runs the bot and is the only user that can read the Alpaca keys.
- `runner` runs GitHub Actions jobs. It can publish releases and run `systemctl restart edgetrader-bot`, and nothing else as root.

State and the journal live outside the releases, so deploys and rollbacks don't touch them.

## One-Time Setup

### 1. Create the container

In Proxmox, create an unprivileged LXC container from the Debian 13 template. 1 core, 512 MB RAM and 4 GB of disk are plenty. It needs outbound internet access only: no ports, tunnel or DNS name.

### 2. Run the setup script

As root in the container:

```bash
apt-get update && apt-get install -y git
git clone https://github.com/mike-jgo/edgetrader.git /root/edgetrader
bash /root/edgetrader/deploy/setup-host.sh
```

This installs Python, creates both users, the folders, the virtualenv, a first release, the systemd unit, `/etc/edgetrader/bot.env`, and the sudo rule for the runner. It is safe to run again.

### 3. Add the Alpaca paper keys

```bash
cat > /var/lib/edgetrader/.edgetrader/config.ini <<'EOF'
[default]
env = paper

[alpaca_paper]
api_key = YOUR_PAPER_KEY_ID
secret_key = YOUR_PAPER_SECRET_KEY
EOF
chown edgetrader:edgetrader /var/lib/edgetrader/.edgetrader/config.ini
chmod 600 /var/lib/edgetrader/.edgetrader/config.ini
```

Use paper keys only. The service always passes `--env paper`.

### 4. Set the bot's arguments and start it

Edit `/etc/edgetrader/bot.env` (watchlist, strategy, interval, risk limits; see [BOT_README.md](BOT_README.md)). Don't add `--env`. Then:

```bash
systemctl enable --now edgetrader-bot
journalctl -u edgetrader-bot -f
```

To try it without placing orders first, add `--dry-run` to `BOT_ARGS` and restart the service.

### 5. Register the GitHub runner

On GitHub, open the repo's **Settings > Actions > Runners > New self-hosted runner**, choose Linux x64, and copy the download commands and token it shows. Run them as `runner`, adding the label and name:

```bash
su - runner
mkdir actions-runner && cd actions-runner
# paste the curl + tar commands from the GitHub page here
./config.sh --url https://github.com/mike-jgo/edgetrader --token TOKEN_FROM_GITHUB \
  --labels edgetrader --name edgetrader-ct --unattended
exit
```

Then, as root, install it as a service that runs as `runner`:

```bash
cd /home/runner/actions-runner
./svc.sh install runner
./svc.sh start
```

The runner should show as **Idle** on the Runners page.

### 6. Lock down fork pull requests

The repository is public. A fork pull request can change the workflow file and ask for the `edgetrader` runner, which would run someone else's code in the container. The deploy job never runs for pull requests, but that check lives in the workflow file the pull request could change.

In **Settings > Actions > General > Approval for running fork pull request workflows from contributors**, choose **Require approval for all external contributors**. Never approve a run from a fork pull request that edits `.github/workflows/`. Making the repository private removes the risk entirely.

## Day-To-Day

- **Deploy:** merge or push to `main`. Watch it with `gh run watch`.
- **Logs:** `journalctl -u edgetrader-bot -f`, or `/var/lib/edgetrader/.edgetrader/bot.log`.
- **Trades:** `/var/lib/edgetrader/.edgetrader/journal.csv`.
- **Change settings:** edit `/etc/edgetrader/bot.env`, then `systemctl restart edgetrader-bot`. No deploy needed.
- **Pause trading:** `systemctl stop edgetrader-bot`. A deploy starts it again even when it is stopped or disabled, so to keep it stopped also stop the runner: `cd /home/runner/actions-runner && ./svc.sh stop`.

A restart in the middle of a cycle is safe. Each order is saved to `bot_state.json` before it is sent, and the next cycle reconciles it with Alpaca.

## Rolling Back

List releases, point `current` at an earlier one, and restart:

```bash
ls -1t /srv/edgetrader/releases
sudo -u runner ln -sfn /srv/edgetrader/releases/<sha> /srv/edgetrader/current
systemctl restart edgetrader-bot
```

The next push to `main` deploys again. To make a rollback stick, revert the commit on `main` instead.
