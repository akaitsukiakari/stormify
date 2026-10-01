# Self-hosting ntfy on the same Pi

ntfy is free and open source (Apache 2.0 / GPLv2), with no ads. Self-hosting means no rate limits, and alert data never leaves your Pi.

## Install (armv6 build for the Pi Zero W)

Check https://github.com/binwiederhier/ntfy/releases for the latest version number; 2.28.0 was current at the time of writing.

```bash
VER=2.28.0
wget https://github.com/binwiederhier/ntfy/releases/download/v${VER}/ntfy_${VER}_linux_armv6.deb
sudo dpkg -i ntfy_${VER}_linux_armv6.deb
```

## Configure

ntfy listens on port 80 by default. Move it to 2586 so it doesn't clash with anything else. Edit `/etc/ntfy/server.yml`:

```yaml
base-url: "https://ntfy.yourdomain.com"
listen-http: ":2586"
cache-file: "/var/cache/ntfy/cache.db"
behind-proxy: true
# Recommended once it's on the internet: require a login to read/publish.
# auth-file: "/var/lib/ntfy/user.db"
# auth-default-access: "deny-all"
```

```bash
sudo systemctl enable --now ntfy
```

If you turn on auth, create a user and a token for Stormify:

```bash
sudo ntfy user add --role=admin scott
sudo ntfy token add scott            # put this token in: stormify ntfy --token ...
```

## Phone

1. Install ntfy from the Play Store (or F-Droid).
2. Settings → set the default server to `https://ntfy.yourdomain.com` (or add the subscription with "use another server").
3. Subscribe to your topic, e.g. `stormify-scott`.
4. On Android, enable **instant delivery** for the subscription. Self-hosted servers don't go through Firebase, so instant delivery keeps a connection open for real-time alerts.
5. Optional: in the subscription settings, give priority 5 (max) a distinct sound and allow it to override Do Not Disturb. Emergencies always send at priority 5.

## iOS (for a friend)

iOS only delivers push through Apple's servers. ntfy handles this by relaying a wake-up through ntfy.sh. Add this to `server.yml`:

```yaml
upstream-base-url: "https://ntfy.sh"
```

Only a message ID passes through ntfy.sh, not the alert content.
