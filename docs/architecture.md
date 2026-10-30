# Architecture

## Component diagram

```mermaid
flowchart LR
    subgraph Browser
        UI["Dashboard page<br/>D3 topology · Chart.js · controls"]
    end

    subgraph Dashboard["dashboard/app.py (Flask + Socket.IO)"]
        POLL["poller (every 1.5 s)"]
        PROXY["/control/* proxy"]
        EXPORT["/export/*.csv"]
    end

    subgraph Controller["Ryu app: controller/sdn_controller.py"]
        TOPO["topology_manager<br/>networkx graph, hosts"]
        ROUTE["routing<br/>ARP/proxy-ARP, Dijkstra, flow registry"]
        LB["load_balancer<br/>VIP, 3 strategies, rewrite"]
        STATS["stats_collector<br/>port/flow stats every 2 s"]
        PRED["ai/predictor<br/>model or moving-average"]
        FT["fault_tolerance<br/>reroute + recovery time"]
        HM["health_monitor<br/>TCP-SYN probes, auto-scaling"]
        SEC["security<br/>DDoS detector + drop rules"]
        REST["rest_api (WSGI :8080)"]
    end

    subgraph Mininet["topology/topology.py (sudo)"]
        NET["6 OVS switches · h1-h4 · srv1-srv4<br/>TCLink 10 Mbit/s 5 ms"]
        CAPI["control_api :8001<br/>links · servers · traffic · attacks · measure"]
    end

    DB[("data/sdn.db<br/>SQLite")]
    CSV[("ai/data/link_stats.csv")]
    MODEL[("ai/models/congestion_model.joblib")]

    UI <-- "Socket.IO update" --> POLL
    UI -- "fetch /control/*" --> PROXY
    UI -- "download" --> EXPORT
    POLL -- "GET /api/*" --> REST
    POLL -- "GET /status" --> CAPI
    PROXY -- "POST /api/strategy, /api/unblock" --> REST
    PROXY -- "POST /link /server /traffic /attack" --> CAPI
    EXPORT --> DB
    CAPI --> NET
    NET <-- "OpenFlow 1.3 :6653" --> Controller
    STATS --> PRED
    PRED --> STATS
    STATS -- "edge weights" --> TOPO
    STATS --> DB
    STATS --> CSV
    SEC --> DB
    MODEL --> PRED
    CSV -. "ai/train.py" .-> MODEL
```

## Emulated network

```mermaid
flowchart LR
    h1((h1)) --- s1[s1]
    h2((h2)) --- s1
    h3((h3)) --- s2[s2]
    h4((h4)) --- s2
    s1 --- s3[s3]
    s1 --- s4[s4]
    s2 --- s3
    s2 --- s4
    s3 --- s4
    s3 --- s5[s5]
    s3 --- s6[s6]
    s4 --- s5
    s4 --- s6
    s5 --- s6
    s5 --- srv1[(srv1)]
    s5 --- srv2[(srv2)]
    s6 --- srv3[(srv3)]
    s6 --- srv4[(srv4 standby)]
```

Every client has at least four loop-free paths to every server, so link failures
and congestion-aware routing always have an alternative.

| Item | Value |
|---|---|
| Clients | h1-h4, 10.0.0.1-4 |
| Servers | srv1-srv3 10.0.0.11-13 (active), srv4 10.0.0.14 (standby) |
| Virtual IP | 10.0.0.100 / 00:00:00:00:01:00 (answered by the controller) |
| Probe address | 10.0.0.254 / 00:00:00:00:02:54 (controller health checks) |
| Links | TCLink, 10 Mbit/s, 5 ms (switch-switch), 1 ms (host) |
| Controller | 127.0.0.1:6653 (OpenFlow 1.3), REST 8080 |
| Dashboard | http://localhost:5000 |
| Mininet control API | http://127.0.0.1:8001 |

## Flow-rule priorities

| Priority | Rule | Timeouts |
|---|---|---|
| 1000 | DDoS drop: `in_port, ipv4_src=attacker` | hard 60 s |
| 65535 | LLDP to controller (installed by Ryu topology app) | none |
| 20 | Load-balancer per-connection rules (with rewrite at the edges) | idle 10 s, hard 60 s |
| 10 | Host-to-host routes `ipv4_src, ipv4_dst` | idle 10 s, hard 60 s |
| 1 | Drop IPv6 | none |
| 0 | Table-miss: to controller (no buffering) | none |

All path rules carry a unique cookie (`0x5D…`), which lets the controller delete
one path precisely, map flow statistics back to it, and clear stale rules after a
controller restart without touching the LLDP rule.
