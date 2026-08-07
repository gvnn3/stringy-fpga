# Grafana dashboard for the PYRO overlay engine

Grafana is **not installed** on this host. The dashboard in
`pyro-overlay-dashboard.json` is an import file, not a running service; the
shortest path from zero to graphs is below. Everything reads from the metrics
endpoint the telemetry demo app already serves — no exporter to write.

## 1. Start the metrics source

From the repo root (the collector owns all hardware access in one thread, so
this is the only process that should talk to the card):

```sh
.venv-pyro/bin/python3 scripts/pyro_telemetry_demo.py --serve 9091 --iface ens2
```

This serves `/metrics` (Prometheus text format), plus `/snapshot.json`,
`/history.json`, and the self-contained HTML dashboard at `/`. Without
`--iface` it still runs, but host-only: device metrics are omitted (nulls are
never zeroed), so Grafana panels for the card will show "No data" — which is
the truthful rendering.

## 2. Point Prometheus at it

Add to `prometheus.yml` (the demo app collects every 2 s; scraping faster than
that only re-reads the same snapshot):

```yaml
scrape_configs:
  - job_name: pyro
    scrape_interval: 5s
    static_configs:
      - targets: ["localhost:9091"]
```

## 3. Install Prometheus + Grafana

Pick one.

**docker compose** (`docker-compose.yml` in any scratch directory;
`network_mode: host` so containers can reach the demo app on localhost):

```yaml
services:
  prometheus:
    image: prom/prometheus
    network_mode: host          # reach the demo app on localhost:9091
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
  grafana:
    image: grafana/grafana-oss
    network_mode: host          # UI on localhost:3000
```

```sh
docker compose up -d
```

**apt** (Ubuntu; grafana needs the vendor repo, prometheus is in universe):

```sh
sudo apt-get install -y prometheus   # then merge the scrape_config into
                                     # /etc/prometheus/prometheus.yml
sudo mkdir -p /etc/apt/keyrings
wget -qO- https://apt.grafana.com/gpg.key | gpg --dearmor \
  | sudo tee /etc/apt/keyrings/grafana.gpg >/dev/null
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg]" \
     "https://apt.grafana.com stable main" \
  | sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt-get update && sudo apt-get install -y grafana
sudo systemctl enable --now grafana-server
```

## 4. Import the dashboard

1. Grafana UI (default `http://localhost:3000`, admin/admin on first login).
2. Connections → Data sources → Add → Prometheus, URL
   `http://localhost:9090`, Save & test.
3. Dashboards → New → Import → Upload
   `grafana/pyro-overlay-dashboard.json` → when prompted for the
   `${DS_PROMETHEUS}` input, select the datasource from step 2 → Import.

## What you get

The panels mirror `web/pyro_dashboard.html`: the four owner metrics
first-class — switch time against the 13.6 s JTAG PR baseline on a log axis,
nominations/s with a top-10 gid:sid bar gauge, netdev drops plus request-loss
ratio, and the rules-missed decomposition (hard misses pinned at 0 by the SR3
invariant, OVF truncations, non-resident, lowering-dropped) — plus table
identity (epoch, capacity, commit flags) and most-recent-scan throughput.

Below those sits a Scheduler row for the WIRE-MAC round-robin
scheduler: mode and quantum as read by the SCHED_ACK refusal probe (an
invalid SCHED_SET is refused, but the ACK still echoes the live state —
a read that never writes), per-program wire grant rate and 0-1 share
(P0 keeps blue and P1 keeps orange in every panel), and 0-healthy stat
tiles for MAC records lost and the two zero-slack residuals.

Caveats are encoded in panel descriptions rather than hidden: netdev counters
zero on onic reload, EQDMA multi-queue loss never sets tuser_err so only the
request-loss ratio sees it, and PERF counters describe the last scan, not an
average.
