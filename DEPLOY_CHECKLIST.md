# Deployment checklist

## Before deployment
- [ ] New GitHub repository created, separate from Diamond-Bot.
- [ ] `.env` and real webhook URL are not committed.
- [ ] GitHub Actions Python checks pass.
- [ ] Separate Compute Engine VM created; no Diamond-Bot files/services reused.

## First VM run
- [ ] Python dependencies installed in `.venv`.
- [ ] Playwright Chromium and Linux dependencies installed.
- [ ] `/etc/chartink-discord.env` created with mode `0640`, owner `root:chartink`.
- [ ] `RUN_SINGLE_CHART_TEST=true` run manually; test Discord message and GAIL image checked.
- [ ] `RUN_SINGLE_CHART_TEST=false` restored.
- [ ] systemd service enabled and active.
- [ ] Logs checked with `journalctl`.
- [ ] `BASELINE_ON_FIRST_SCAN=true` used on first startup to avoid a burst of historical/current matches.
