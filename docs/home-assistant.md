# Home Assistant: heartbeat monitor + sensors

Stormify's scariest failure is silent: the Pi dies and alerts just stop. Home Assistant runs on a different Pi, so it can watch Stormify and warn you through the HA app if it goes quiet.

`GET http://<stormify-pi>:8080/api/health` needs no login and returns:

```jsonc
{
  "status": "ok",                 // "stale" if no successful poll within heartbeat_stale_seconds
  "seconds_since_success": 24,
  "last_success_at": "2026-10-01T23:10:04Z",
  "last_error": null,
  "active_alerts": 412,
  "last_alert_event": "Tornado Warning",
  "last_alert_at": "2026-10-01T23:08:51Z",
  "last_push_at": "2026-10-01T23:08:52Z",
  "pushes_total": 37,
  "polls": 1440
}
```

It returns HTTP 200 when healthy and 503 when stale. For HA to reach it, the dashboard must listen on the LAN (`host = "0.0.0.0"` in `[web]`).

## configuration.yaml

```yaml
rest:
  - resource: http://wxalerts.local:8080/api/health
    scan_interval: 60
    timeout: 10
    sensor:
      - name: Stormify status
        value_template: "{{ value_json.status }}"
      - name: Stormify seconds since poll
        value_template: "{{ value_json.seconds_since_success }}"
        unit_of_measurement: s
      - name: Stormify active alerts
        value_template: "{{ value_json.active_alerts }}"
      - name: Stormify last alert
        value_template: "{{ value_json.last_alert_event }}"
        json_attributes:
          - last_alert_at
          - last_push_at
          - last_error
          - pushes_total
```

When the Pi is unreachable, the REST sensors go `unavailable`. That counts as down too.

## Automation: warn me if Stormify goes blind

```yaml
automation:
  - alias: Stormify down
    triggers:
      - trigger: state
        entity_id: sensor.stormify_status
        to: ["stale", "unavailable"]
        for: "00:05:00"
    actions:
      - action: notify.mobile_app_YOUR_PHONE
        data:
          title: "Stormify is down"
          message: "No successful weather poll for 5+ minutes. Alerts are NOT reaching you."
          data:
            ttl: 0
            priority: high
  - alias: Stormify recovered
    triggers:
      - trigger: state
        entity_id: sensor.stormify_status
        from: ["stale", "unavailable"]
        to: "ok"
    actions:
      - action: notify.mobile_app_YOUR_PHONE
        data:
          message: "Stormify is back. Alerts are flowing again."
```

Replace `notify.mobile_app_YOUR_PHONE` with your device's notify service (Developer Tools → Actions, search "mobile_app").
