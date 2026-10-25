#!/usr/bin/env bash
# Install a self-hosted ntfy server on this Pi and point Stormify at it.
# Run after deploy/install.sh and `stormify init`:
#   sudo ./deploy/install-ntfy.sh --user scott [--base-url https://ntfy.yourdomain.com] [--topic NAME]
# Safe to re-run: it upgrades ntfy, keeps existing logins, and makes a new Stormify token.
set -euo pipefail

usage() {
  echo "Usage: sudo $0 --user STORMIFY_USER [--base-url https://ntfy.example.com] [--topic NAME] [--version X.Y.Z]" >&2
  exit 1
}

USER_NAME="" BASE_URL="" TOPIC="" VER="${NTFY_VERSION:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) USER_NAME="${2:-}"; shift 2 ;;
    --base-url) BASE_URL="${2%/}"; shift 2 ;;
    --topic) TOPIC="${2:-}"; shift 2 ;;
    --version) VER="${2#v}"; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n "$USER_NAME" ]] || usage
if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 ..." >&2
  exit 1
fi
command -v stormify >/dev/null || { echo "Run deploy/install.sh first." >&2; exit 1; }
grep -qw "$USER_NAME" <<<"$(stormify user list)" || { echo "No Stormify user '$USER_NAME'. Run: stormify init --user $USER_NAME" >&2; exit 1; }

PORT=2586
CONF=/etc/ntfy/server.yml

# The Pi Zero W is armv6; the armv7 build crashes on it, so pick by CPU, not by Debian arch.
case "$(uname -m)" in
  armv6l) ARCH=armv6 ;;
  armv7l) ARCH=armv7 ;;
  aarch64) ARCH=arm64 ;;
  x86_64) ARCH=amd64 ;;
  *) echo "Unsupported CPU: $(uname -m)" >&2; exit 1 ;;
esac

if [[ -z "$VER" ]]; then
  latest=$(curl -fsSLI -o /dev/null -w '%{url_effective}' https://github.com/binwiederhier/ntfy/releases/latest || true)
  VER="${latest##*/v}"
  [[ "$VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Couldn't look up the latest ntfy version; pass --version X.Y.Z" >&2; exit 1; }
fi

if [[ "$(dpkg-query -W -f='${Version}' ntfy 2>/dev/null || true)" == "$VER" ]]; then
  echo "==> ntfy $VER already installed"
else
  echo "==> Installing ntfy $VER ($ARCH)"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -o "$tmp/ntfy.deb" \
    "https://github.com/binwiederhier/ntfy/releases/download/v${VER}/ntfy_${VER}_linux_${ARCH}.deb"
  dpkg -i "$tmp/ntfy.deb"
fi

echo "==> Writing $CONF"
[[ -f "$CONF" && ! -f "$CONF.orig" ]] && cp "$CONF" "$CONF.orig"
{
  echo "# Written by stormify deploy/install-ntfy.sh. The package's original is in server.yml.orig."
  [[ -n "$BASE_URL" ]] && echo "base-url: \"$BASE_URL\""
  echo "listen-http: \":$PORT\""
  echo "cache-file: \"/var/cache/ntfy/cache.db\""
  echo "cache-duration: \"12h\""
  # Reached through the Cloudflare tunnel, so trust its X-Forwarded-For for rate limiting.
  [[ -n "$BASE_URL" ]] && echo "behind-proxy: true"
  echo "auth-file: \"/var/lib/ntfy/user.db\""
  echo "auth-default-access: \"deny-all\""
  echo "# For an iPhone, uncomment to relay wake-ups (message IDs only) through ntfy.sh:"
  echo "# upstream-base-url: \"https://ntfy.sh\""
} > "$CONF"
chown root:ntfy "$CONF"
chmod 640 "$CONF"

# Keep ntfy from crowding out Stormify on a 512 MB Pi.
mkdir -p /etc/systemd/system/ntfy.service.d
cat > /etc/systemd/system/ntfy.service.d/stormify.conf <<'EOF'
[Service]
MemoryMax=80M
Environment=GOGC=50
Restart=always
RestartSec=10
EOF
systemctl daemon-reload
systemctl enable ntfy >/dev/null
systemctl restart ntfy

for _ in $(seq 1 30); do
  curl -fs "http://localhost:$PORT/v1/health" >/dev/null && break
  sleep 1
done
curl -fs "http://localhost:$PORT/v1/health" >/dev/null || { echo "ntfy didn't start; check: journalctl -u ntfy" >&2; exit 1; }

# The ntfy CLI reads $CONF for the auth file; run it as the service user so user.db stays theirs.
ntfy_cli() { sudo -u ntfy ntfy "$@"; }

if ntfy_cli user list 2>&1 | grep -q "^user $USER_NAME "; then
  echo "==> ntfy login '$USER_NAME' already exists"
else
  echo "==> Creating ntfy login '$USER_NAME' (you'll use this in the phone app)"
  ntfy_cli user add --role=admin "$USER_NAME"
fi

TOKEN=$(ntfy_cli token add --label=stormify "$USER_NAME" 2>&1 | grep -o 'tk_[A-Za-z0-9]*' | head -1)
[[ -n "$TOKEN" ]] || { echo "Couldn't create an ntfy token for $USER_NAME" >&2; exit 1; }

[[ -n "$TOPIC" ]] || TOPIC="stormify-$(openssl rand -hex 4)"
stormify ntfy --user "$USER_NAME" --topic "$TOPIC" --server "http://localhost:$PORT" --token "$TOKEN" >/dev/null

PI_IP=$(hostname -I | awk '{print $1}')
cat <<EOF

==> ntfy is running and Stormify is pointed at it.

On your phone (ntfy app from the Play Store):
  1. Settings > Manage users > Add user
       Server:   ${BASE_URL:-http://$PI_IP:$PORT}
       Username: $USER_NAME   (and the password you just set)
  2. Tap + , turn on "Use another server", enter the same server, topic: $TOPIC
     Leave "Instant delivery" on.
  3. Then send a test:  stormify test-push --user $USER_NAME
EOF
if [[ -z "$BASE_URL" ]]; then
  cat <<EOF

The address above only works on your home Wi-Fi. To get alerts anywhere, add a Cloudflare
tunnel hostname for http://$PI_IP:$PORT (see docs/setup-pi.md), then re-run with
--base-url https://ntfy.yourdomain.com --topic $TOPIC and change the server in the app.
EOF
fi
