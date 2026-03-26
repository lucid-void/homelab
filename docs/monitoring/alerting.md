---
tags:
  - monitoring
  - alerting
  - grafana
  - gotify
---

# Alerting

Grafana's built-in alerting engine evaluates rules against Prometheus metrics. All notifications are delivered to Gotify via a webhook contact point. No Alertmanager is used.

## Gotify contact point

- **URL:** `http://gotify.blackcats.cc`
- **Token:** Gotify app token — stored as a Grafana secret, provisioned by Ansible

## Alert rules

| Alert | Condition | Severity |
|---|---|---|
| Host down | `up == 0` for any scrape target for > 2 min | Critical |
| High CPU | Node CPU usage > 90% sustained for 5 min | Warning |
| Low disk | Any mount with < 20% free | Warning |
| Disk spike | Disk usage growing > 5 GB/h on any mount | Warning |
| ZFS pool degraded | `truenas_pool_status != healthy` | Critical |
| GPU temp high | `dcgm_gpu_temp > 85°C` | Warning |

## Provisioning

Alert rules are provisioned via Ansible-templated YAML files in Grafana's file-based provisioning directory:

```
/etc/grafana/provisioning/alerting/
```

Files are loaded at container startup — reproducible on rebuild without touching the Grafana UI.

## Key decisions

| Topic | Decision |
|---|---|
| Alerting engine | Grafana built-in — no Alertmanager |
| Notification target | Gotify webhook |
| Rule delivery | File-based provisioning via Ansible |
