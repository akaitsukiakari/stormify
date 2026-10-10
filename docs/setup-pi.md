# Setting up Stormify on a Raspberry Pi Zero W

Everything (poller, database, dashboard, ntfy) runs on one Pi Zero W. Idle load is close to zero.

## 1. Flash the SD card

- Use a 32 GB **endurance-rated** microSD card. Lots of small writes wear out cheap cards, and that's the failure that matters here, not running out of space.
- In Raspberry Pi Imager, choose **Raspberry Pi Zero** → **Raspberry Pi OS (other)** → **Raspberry Pi OS Lite (32-bit)**. The Zero W can't run 64-bit.
- In the customization settings, set a hostname (e.g. `wxalerts`), a user, your Wi-Fi (2.4 GHz only), timezone `America/Denver`, and enable SSH.
- Optional but recommended: give the Pi a DHCP reservation in your router.

## 2. Get the code onto the Pi

The repo is private, so either clone with a GitHub personal access token or copy it over:

```bash
sudo apt-get install -y git
git clone https://github.com/akaitsukiakari/stormify.git
cd stormify
```

## 3. Install

```bash
sudo ./deploy/install.sh
sudo nano /etc/stormify/config.toml    # set user_agent to your email
stormify init --user scott             # asks for a dashboard password, loads example rules
```

The pip install step is slow on a Zero W (several minutes). That's normal.

## 4. Set up ntfy

See [ntfy.md](ntfy.md), then:

```bash
stormify ntfy --user scott --topic stormify-scott --server http://localhost:2586
stormify test-push --user scott
```

## 5. Start it

```bash
sudo systemctl start stormify-poller stormify-web
stormify health                         # "status": "ok" after the first poll
journalctl -u stormify-poller -f        # watch it work
```

The first poll archives everything currently active **without** pushing it, so you don't get blasted with the whole country's alerts. Pushes start on the second poll.

## 6. Reach the dashboard from anywhere

Add public hostnames to your existing Cloudflare tunnel (Zero Trust dashboard → Networks → Tunnels → your tunnel → Public Hostname):

| Hostname | Service |
|---|---|
| `wx.yourdomain.com` | `http://<pi-ip>:8080` |
| `ntfy.yourdomain.com` | `http://<pi-ip>:2586` |

If the tunnel connector (`cloudflared`) runs on a different machine than the Stormify Pi, set `host = "0.0.0.0"` under `[web]` in the config so the dashboard listens on your LAN. If `cloudflared` runs on the Stormify Pi itself, `127.0.0.1` / `localhost` is fine.

Then set `public_url = "https://wx.yourdomain.com"` in the config, so tapping a notification opens the alert in the dashboard and pushes can carry a map picture (your phone fetches it from that address).

**DNS note:** Cloudflare Tunnel public hostnames only work for a domain whose DNS is on Cloudflare. If your DreamHost domain's DNS is still at DreamHost, either move that domain's nameservers to Cloudflare (free plan), or use whichever domain your old Home Assistant tunnel used. Check how the existing tunnel is set up before changing anything.

## Updating

```bash
cd ~/stormify && git pull && sudo ./deploy/install.sh
```
