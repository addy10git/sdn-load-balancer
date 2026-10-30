# Module-by-module explanation (for the project report)

Networking is about 85 % of the project; the AI predictor about 15 %.

## 1. Topology (`topology/topology.py`, `topology/control_api.py`)
A Mininet script builds 6 OpenFlow 1.3 OVS switches in a partial mesh (10
inter-switch links), 4 clients and 4 servers on TCLinks (10 Mbit/s, 5 ms). Each
server runs `python3 -m http.server 80` (returns its own name) and iperf TCP/UDP
servers on 5001. A `RemoteController` points at 127.0.0.1:6653.
A small threaded HTTP API on 127.0.0.1:8001 runs inside the Mininet process and
exposes link up/down (`configLinkStatus`), server stop/start, iperf bursts,
hping3 floods and measurements (iperf/ping parsed to JSON). The dashboard, demo
and experiments use it, so no Mininet CLI typing is needed.

## 2. Topology discovery (`controller/topology_manager.py`)
Started with `--observe-links`, Ryu's `switches` app sends LLDP out of every port
and raises `EventSwitchEnter/Leave` and `EventLinkAdd/Delete`. The manager keeps a
`networkx.Graph` (nodes = dpids, edges carry the port on each side and a routing
weight). Hosts are learned from PacketIn on ports that are *not* inter-switch
ports (MAC → switch, port, IP). Packets are ignored until all expected links are
discovered (or 10 s pass), so a host is never learned on a trunk port.

## 3. Routing (`controller/routing.py`, `controller/path_utils.py`)
* **ARP:** the controller answers every ARP request itself (proxy ARP) for the VIP,
  the probe address and every known host. Unknown targets are delivered only to
  host-facing (edge) ports via PacketOut, never across inter-switch links. The
  controller is effectively the root of the broadcast tree, so the looped mesh
  cannot produce a broadcast storm.
* **Paths:** Dijkstra on the graph with
  `weight = 1 + 4 × utilisation + 10 if predicted utilisation > 80 %`.
* **Rules:** one FlowMod per hop (`ipv4_src, ipv4_dst`), idle 10 s / hard 60 s,
  installed egress-first, each path with its own cookie, tracked in a registry.

## 4. Load balancer (`controller/load_balancer.py`, `controller/lb_strategies.py`)
Per-connection balancing (TCP/UDP 5-tuple; ICMP per client). The client edge
switch rewrites `dst VIP → server` on the way in and `src server → VIP` on the
way out, so clients only see 10.0.0.100. Strategies (switchable at runtime via
`POST /api/strategy`):
1. `round_robin`: next server in order.
2. `least_load`: lowest current load of the server's access link, ties broken by
   active connections.
3. `ai_predictive`: `0.6 × predicted server load + 0.4 × worst predicted link on
   the client→server path`, +100 if either exceeds 80 %.
Sessions stay pinned to their server for 120 s (stickiness); when a server leaves
the pool its rules are deleted so new packets get re-balanced.

## 5. Statistics (`controller/stats_collector.py`, `controller/stats_utils.py`)
Every 2 s: `OFPPortStatsRequest` and `OFPFlowStatsRequest` to every switch.
Byte-counter deltas give per-direction throughput; link utilisation is the larger
direction divided by capacity. Also drops and flows per link, server load,
per-source packet rates and completed TCP handshakes. History is kept in memory
(5 min), written to SQLite (`data/sdn.db`) and appended to
`ai/data/link_stats.csv` for training.

## 6. Fault tolerance (`controller/fault_tolerance.py`)
On `OFPPortStatus` link-down (or LLDP `EventLinkDelete`) the link leaves the
graph, rules of every affected path are deleted by cookie, new paths are
computed and installed, and a `BarrierRequest` is sent to every touched switch.
Recovery time = failure detection → last BarrierReply. It is logged as an event
and shown on the dashboard.

## 7. Health checks and auto-scaling (`controller/health_monitor.py`, `controller/autoscaler.py`)
Every 3 s the controller injects a TCP SYN to port 80 of each server (from
10.0.0.254). SYN-ACK = healthy (a RST then closes it); RST = service down;
no answer within 1.5 s = host down. Two consecutive failures remove a server from
the pool, one success re-adds it. Auto-scaling uses hysteresis: average pool load
> 75 % for 15 s activates standby srv4; < 30 % for 30 s removes it. Every pool
change is an event.

## 8. DDoS detection and mitigation (`controller/ddos_detector.py`, `controller/security.py`)
Three rule-based signals per source: packets/s (> 1500, from flow counters),
new flows/s (> 40, from PacketIns) and SYN/ACK ratio (> 3 with ≥ 20 SYNs,
handshake completion seen as ≥ 3 packets on the connection's rule). Detection is
also checked inline on every PacketIn so a flood is stopped after ~1 s. A
priority-1000 drop rule (`in_port, ipv4_src`) with `hard_timeout=60` is installed at
the attacker's edge switch, so it is unblocked automatically; manual unblock via
`POST /api/unblock`. Servers and the VIP are never blocked.

## 9. AI congestion predictor (`ai/`)
* `collect_data.py` drives steady / bursty / heavy / idle traffic through the
  Mininet API while the controller logs link statistics.
* `train.py`: features = last 5 utilisation samples + active flows; target =
  utilisation 10 s (5 samples) ahead. Linear Regression vs Random Forest on a
  time-ordered split, compared with MAE/RMSE (plus the moving-average baseline);
  the better model is saved with joblib.
* `predictor.py` runs every stats cycle; without a model it uses a moving average
  plus damped trend. Predictions > 80 % add a routing penalty and steer
  `ai_predictive` away from that server/path.

## 10. REST API and dashboard (`controller/rest_api.py`, `dashboard/`)
The controller serves JSON (`/api/topology, stats, servers, predictions, events,
security, summary`, `POST /api/strategy, /api/unblock`). The Flask backend polls it
every 1.5 s and pushes one Socket.IO `update` to browsers; if the controller is
down the page shows "controller disconnected". The single-page UI has a D3 tiered
topology (colour = utilisation, thickness = throughput, dashed red outline =
predicted congestion, red ring = blocked attacker), server cards, three Chart.js
charts, controls, a colour-coded event log, a security panel and CSV export from
SQLite.

## 11. Experiments (`experiments/run_experiments.py`)
For each strategy × scenario (normal, heavy, link failure, server failure, DDoS)
the network is reset, measurements run concurrently (iperf TCP from h1-h3, iperf
UDP + ping from h4), failures/attacks are triggered mid-test, and throughput,
latency, jitter, loss, server-load standard deviation, connection-count standard
deviation, recovery and detection time are written to CSV and plotted as PNG bar
charts.

## 12. Persistence, logs, tests
* SQLite tables: `link_stats`, `predictions` (with the actual value filled in 10 s
  later), `server_stats`, `events`. Events survive restarts.
* Logs: `logs/controller_<ts>.log`, `logs/dashboard_<ts>.log`, console logs.
* Tests: `pytest` covers path computation, strategy selection, DDoS thresholds,
  auto-scaling, the predictor fallback, iperf/ping parsing, SQLite, and (when
  Ryu/os-ken is installed) the controller logic against fake switches with real
  OpenFlow message encoding.
