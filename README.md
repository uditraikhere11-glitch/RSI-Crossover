# Chartink → Discord Scanner (Standalone)

A standalone 24/7 Linux service that polls Chartink every five minutes, posts matching NSE symbols to Discord, captures each new symbol using Chartink Snapshot #2139, and uploads the PNG to Discord.

**This project is intentionally independent of Diamond-Bot.** It does not import Diamond-Bot code, use its `.env`, share its WebSocket, connect to a broker, or place trades.

## What is preserved from the supplied script

- The exact `SCAN_CLAUSE` expression.
- Chartink scanner endpoint and CSRF-token flow.
- Snapshot URL/layout `relative-strength-index-chart-snapshot-2139` with `&symbol=NSE_SYMBOL`.
- Discord normal alert followed by queued chart capture/upload.
- Five-minute polling, retry settings, GAIL as the sample test symbol.

## Deployment differences

- Removed `google.colab.userdata`; configuration comes from environment variables.
- Dependencies and Chromium are installed during server setup, not every process start.
- Scanner match state is saved to a JSON file so a restart does not automatically resend every still-matching stock.
- A stock can alert again if it leaves the scan and later re-enters.
- Chartink request failure is treated separately from a successful scan with zero matches.
- Logs go to standard output / systemd journal.
- The supplied strategy is an alerting scanner only; no orders are placed.

## 1. Create a separate GitHub repository

1. On GitHub, create a **new repository**, for example `chartink-discord-scanner`. Do not put these files in the Diamond-Bot repository.
2. Upload the contents of this project folder to the new repository (the files inside this folder, not the outer ZIP directory).
3. Keep the repository free of secrets. Commit `.env.example`, never a real `.env` or Discord webhook.
4. Wait for the **Python checks** GitHub Actions workflow to pass.

For a first run, leave the GitHub repository private if you prefer. A private repository requires SSH/deploy-key or other Git authentication on the VM.

## 2. Create a separate Google Cloud VM

Create a new Compute Engine VM specifically for this service; do not install it on `diamond-bot-1` if separation is a requirement. Use Ubuntu 24.04 LTS, a modest e2-small or better machine, and a boot disk large enough for the OS and Playwright browser packages (20 GB is a practical starting point). No inbound HTTP/HTTPS firewall rule is required because this worker makes outbound HTTPS requests only. A static public IP is not required by this app.

Connect using the Google Cloud Console SSH button or `gcloud compute ssh`. Google Cloud's official guidance: <https://docs.cloud.google.com/compute/docs/create-linux-vm-instance?hl=en>.

## 3. Install the service on the new VM

The commands below assume the GitHub repository is named `chartink-discord-scanner` and is public. Change the clone URL for your repository.

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip
sudo useradd --system --create-home --home-dir /home/chartink --shell /usr/sbin/nologin chartink || true
sudo mkdir -p /opt/chartink-discord
sudo chown chartink:chartink /opt/chartink-discord
sudo -u chartink git clone https://github.com/YOUR_GITHUB_USERNAME/chartink-discord-scanner.git /opt/chartink-discord
sudo -u chartink mkdir -p /opt/chartink-discord/data /opt/chartink-discord/.cache/ms-playwright
sudo -u chartink python3 -m venv /opt/chartink-discord/.venv
sudo -u chartink /opt/chartink-discord/.venv/bin/python -m pip install --upgrade pip
sudo -u chartink /opt/chartink-discord/.venv/bin/pip install -r /opt/chartink-discord/requirements.txt
```

Install the required Linux libraries as root, then install the Chromium browser as the `chartink` service user so it is available to that account. Playwright's official browser setup guidance: <https://playwright.dev/python/docs/browsers>.

```bash
sudo /opt/chartink-discord/.venv/bin/python -m playwright install-deps chromium
sudo -u chartink mkdir -p /opt/chartink-discord/.cache/ms-playwright
sudo -u chartink env PLAYWRIGHT_BROWSERS_PATH=/opt/chartink-discord/.cache/ms-playwright /opt/chartink-discord/.venv/bin/python -m playwright install chromium
```

## 4. Configure the Discord webhook securely

Create a Discord webhook in the channel where alerts should be posted. Treat its URL as a password. On the new VM, create a root-managed environment file:

```bash
sudo install -o root -g chartink -m 0640 /dev/null /etc/chartink-discord.env
sudo nano /etc/chartink-discord.env
```

Put this configuration in the file and replace the placeholder with the real webhook URL:

```dotenv
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/REPLACE_ME/REPLACE_ME
RUN_SINGLE_CHART_TEST=false
TEST_STOCK=GAIL
SCAN_INTERVAL=300
REQUEST_TIMEOUT=15
ERROR_RETRY_INTERVAL=60
CHART_WIDTH=1800
CHART_HEIGHT=1100
CHARTINK_WAIT_SECONDS=10
CHART_MAX_RETRIES=2
BASELINE_ON_FIRST_SCAN=true
STATE_FILE=/opt/chartink-discord/data/last_matches.json
LOG_LEVEL=INFO
```

`BASELINE_ON_FIRST_SCAN=true` is recommended for first deployment: the first successful scan records the current matches without alerting every stock already in the scan. After that, only newly appearing matches alert. Set it to `false` to keep the original script's first-run behavior. Do not paste the real webhook URL into chat or commit it to GitHub.

## 5. Test one chart before starting 24/7 scanning

Temporarily edit `/etc/chartink-discord.env` and set `RUN_SINGLE_CHART_TEST=true`. Then run the test directly:

```bash
sudo -u chartink env PLAYWRIGHT_BROWSERS_PATH=/opt/chartink-discord/.cache/ms-playwright /usr/bin/env bash -lc 'set -a; source /etc/chartink-discord.env; set +a; cd /opt/chartink-discord; .venv/bin/python app.py'
```

It first posts a Discord connection test and then attempts the GAIL snapshot. Check Discord and the terminal output. When it succeeds, set `RUN_SINGLE_CHART_TEST=false` again before enabling the service. If the VM returns a Chartink bot challenge, blank chart, or unexpected layout, the screenshot flow will need to be adjusted to the page behavior Chartink serves from that VM.

## 6. Install and enable systemd

```bash
sudo cp /opt/chartink-discord/deploy/chartink-discord.service /etc/systemd/system/chartink-discord.service
sudo systemctl daemon-reload
sudo systemctl enable --now chartink-discord.service
sudo systemctl status chartink-discord.service --no-pager
```

Useful operations:

```bash
sudo journalctl -u chartink-discord.service -f
sudo systemctl restart chartink-discord.service
sudo systemctl stop chartink-discord.service
sudo systemctl start chartink-discord.service
```

## 7. Deploy future GitHub changes

After merging changes to the repo's `main` branch, run on the **Chartink VM only**:

```bash
sudo -u chartink git -C /opt/chartink-discord pull --ff-only
sudo -u chartink /opt/chartink-discord/.venv/bin/pip install -r /opt/chartink-discord/requirements.txt
sudo /opt/chartink-discord/.venv/bin/python -m playwright install-deps chromium
sudo -u chartink env PLAYWRIGHT_BROWSERS_PATH=/opt/chartink-discord/.cache/ms-playwright /opt/chartink-discord/.venv/bin/python -m playwright install chromium
sudo systemctl restart chartink-discord.service
sudo systemctl status chartink-discord.service --no-pager
```

This is intentionally a separate Git repository and service from Diamond-Bot. Do not run these commands from `/opt/Diamond-Bot`.

## Operational notes / limitations

- The Chartink HTML/CSRF and scanner endpoint are website implementation details and may change; a successful startup does not guarantee Chartink will always accept the request.
- The screenshot is a browser screenshot, not a market-data API or a guarantee the chart has completely rendered. Validate the first GAIL screenshot on the actual VM before enabling unattended scanning.
- A Discord webhook is rate-limited by Discord. Very large symbol sets may be posted in multiple messages.
- `systemd` restarts the process if it crashes. Match state persists under `/opt/chartink-discord/data/last_matches.json`.
- This software only creates scanner notifications/images. It does not submit trades or provide investment advice.
