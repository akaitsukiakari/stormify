# Self-hosting ntfy on the same Pi

ntfy is free and open source (Apache 2.0 / GPLv2), with no ads. Self-hosting means no rate limits, and alert data never leaves your Pi.

## Install

One command installs ntfy, locks it down so only your login can read or send, caps its memory so it can't crowd out Stormify, and points Stormify at it:

```bash
cd ~/stormify
sudo ./deploy/install-ntfy.sh --user scott
```

It asks you to pick a password for an ntfy login named after your Stormify user (that's what the phone app signs in with). At the end it prints the server address, your login and your topic.

Without `--base-url`, the server is only reachable on your home Wi-Fi at `http://<pi-ip>:2586`. To get alerts away from home, add the `ntfy.yourdomain.com` hostname to your Cloudflare tunnel (see [setup-pi.md](setup-pi.md#6-reach-the-dashboard-from-anywhere)), then re-run with the address and the same topic:

```bash
sudo ./deploy/install-ntfy.sh --user scott --base-url https://ntfy.yourdomain.com --topic stormify-xxxxxxxx
```

Re-running is safe: it keeps your login, upgrades ntfy if there's a newer release, and gives Stormify a fresh token. The script writes `/etc/ntfy/server.yml` (the package's original is kept as `server.yml.orig`) and a memory cap in `/etc/systemd/system/ntfy.service.d/stormify.conf`.

## Phone

1. Install ntfy from the Play Store (or F-Droid).
2. Settings → Manage users → Add user: the server address the script printed, your login and password.
3. Tap **+**, turn on **Use another server**, enter the same address, and subscribe to the topic the script printed.
4. Keep **Instant delivery** on for the subscription. Self-hosted servers don't go through Firebase, so instant delivery keeps a connection open for real-time alerts.
5. Optional: in the subscription settings, give priority 5 (max) a distinct sound and allow it to override Do Not Disturb. Emergencies always send at priority 5.

## iOS (for a friend)

iOS only delivers push through Apple's servers. ntfy handles this by relaying a wake-up through ntfy.sh. Uncomment this line in `/etc/ntfy/server.yml` and run `sudo systemctl restart ntfy`:

```yaml
upstream-base-url: "https://ntfy.sh"
```

Only a message ID passes through ntfy.sh, not the alert content.

## Troubleshooting

```bash
stormify test-push --user scott      # "Sent test via ntfy" means the Pi side works
systemctl status ntfy                # is the server running?
journalctl -u ntfy -n 50             # its log
```

If the test says it sent but the phone shows nothing, the phone isn't subscribed to the same server and topic, or it isn't signed in to that server (Settings → Manage users).
