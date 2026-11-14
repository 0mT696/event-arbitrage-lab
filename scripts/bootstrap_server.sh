#!/usr/bin/env bash
# Bootstrap for the dedicated event-arbitrage-lab server.
# Tested target: fresh Ubuntu 24.04 / Debian 12, run as root or with sudo.
# Research-only host: NO trading keys, NO exchange credentials, ever.
set -euo pipefail

REPO_URL="${1:-https://github.com/0mT696/event-arbitrage-lab.git}"

echo "== system packages =="
apt-get update -qq
apt-get install -y -qq python3.12 python3.12-venv git

echo "== dedicated service account (isolation contract §6) =="
id -u eventarb >/dev/null 2>&1 || useradd -r -m -s /bin/bash eventarb

echo "== clone + install + gate as eventarb =="
sudo -u eventarb bash -s "$REPO_URL" <<'EOF'
set -euo pipefail
REPO_URL="$1"
cd ~
[ -d event-arbitrage-lab ] || git clone "$REPO_URL"
cd event-arbitrage-lab
git pull --ff-only || true
python3.12 -m venv .venv
.venv/bin/pip install -q -e ".[dev]"
bash scripts/check.sh
echo "== live smoke (Polymarket public reads) =="
EAL_LIVE_SMOKE=1 .venv/bin/python -m pytest tests/test_polymarket_live.py -q || \
  echo "smoke failed — check egress to gamma-api.polymarket.com / clob.polymarket.com"
EOF

echo "BOOTSTRAP DONE"
