#!/usr/bin/env bash
# Install or update Stormify on a Raspberry Pi (Raspberry Pi OS Lite).
# Run from the repo checkout:  sudo ./deploy/install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR=/opt/stormify
CONF_DIR=/etc/stormify
DATA_DIR=/var/lib/stormify

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

echo "==> Creating stormify user and folders"
id stormify &>/dev/null || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin stormify
mkdir -p "$APP_DIR" "$CONF_DIR" "$DATA_DIR"
chown stormify:stormify "$DATA_DIR"

echo "==> Installing Stormify into $APP_DIR/venv (this is slow on a Pi Zero; be patient)"
[[ -d "$APP_DIR/venv" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
# Pillow (for map pictures on notifications) is optional: if pip can't install it, fall back to
# Debian's build and let the venv see it, and if that fails too, carry on without pictures.
if ! "$APP_DIR/venv/bin/pip" install --quiet "$REPO_DIR[images]"; then
  echo "    Pillow didn't install from pip; trying the Debian package"
  "$APP_DIR/venv/bin/pip" install --quiet "$REPO_DIR"
  if apt-get install -y -qq python3-pil; then
    sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' "$APP_DIR/venv/pyvenv.cfg"
  else
    echo "    Skipping map pictures on notifications (Pillow unavailable)"
  fi
fi

if [[ ! -f "$CONF_DIR/config.toml" ]]; then
  echo "==> Writing $CONF_DIR/config.toml"
  sed "s/^secret_key = \"\"/secret_key = \"$(openssl rand -hex 32)\"/" \
    "$REPO_DIR/deploy/config.example.toml" > "$CONF_DIR/config.toml"
  chown root:stormify "$CONF_DIR/config.toml"
  chmod 640 "$CONF_DIR/config.toml"
  echo "    Edit it now: set user_agent to your email (NWS asks for contact info)."
fi

echo "==> Installing systemd services"
cp "$REPO_DIR/deploy/stormify-poller.service" "$REPO_DIR/deploy/stormify-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable stormify-poller stormify-web >/dev/null

# Convenience wrapper so `stormify ...` uses the right config and user.
cat > /usr/local/bin/stormify <<'EOF'
#!/usr/bin/env bash
exec sudo -u stormify env STORMIFY_CONFIG=/etc/stormify/config.toml /opt/stormify/venv/bin/stormify "$@"
EOF
chmod 755 /usr/local/bin/stormify

if sudo -u stormify env STORMIFY_CONFIG="$CONF_DIR/config.toml" "$APP_DIR/venv/bin/stormify" user list | grep -q .; then
  systemctl restart stormify-poller stormify-web
  echo "==> Updated and restarted."
else
  cat <<EOF

==> Installed. Finish setup:
    1. sudo nano $CONF_DIR/config.toml        (set user_agent)
    2. stormify init --user YOURNAME            (creates your login + example rules)
    3. stormify ntfy --user YOURNAME --topic YOUR-TOPIC
    4. sudo systemctl start stormify-poller stormify-web
    5. stormify test-push --user YOURNAME
EOF
fi
