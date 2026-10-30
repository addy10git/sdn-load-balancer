# Intelligent SDN Load Balancer
### with Congestion Prediction, DDoS Mitigation and a Live Dashboard

An OpenFlow 1.3 controller (Ryu) for an emulated Mininet network. It balances
clients across a server pool behind the virtual IP **10.0.0.100**, routes around
congested or failed links, health-checks and auto-scales servers, blocks DDoS
sources at the edge, predicts link congestion 10 s ahead, and shows all of it
on a live web dashboard.

    B[Browser dashboard] <-- Socket.IO --> D[Flask dashboard :5000]
    D -- REST --> C[Ryu controller<br/>:6653 OpenFlow · :8080 REST]
    D -- control API --> M[Mininet :8001]
    C <-- OpenFlow 1.3 --> N[6 OVS switches<br/>h1-h4 · srv1-srv4]
    M --> N
    C --> S[(SQLite + CSV)]
    S -. train.py .-> P[AI model]
    P --> C
```

More detail: [docs/architecture.md](docs/architecture.md) ·
[module explanations](docs/modules.md) ·
[OpenFlow message flow](docs/openflow_message_flow.md) ·
[AI training results](docs/ai_training_results.md) ·
[viva questions](docs/viva_questions.md)

---

## 1. Requirements
* Ubuntu 20.04/22.04 **or** Kali Linux / Debian 12+ (a VM is fine; 2 CPUs and 4 GB RAM recommended), with sudo
* Internet access for `setup.sh`, and for the dashboard's CDN scripts (D3, Chart.js, Socket.IO)

`setup.sh` detects the distribution and picks one of two install paths:

| Path | Used on | Python | Controller | Packages |
|---|---|---|---|---|
| classic | Ubuntu | 3.8 (deadsnakes PPA on 22.04) | Ryu 4.34 | [requirements.txt](requirements.txt) (eventlet 0.30.2, …) |
| modern | Kali, Debian, anything else, or `./setup.sh --modern` | system Python 3.11+ | os-ken 4.2.2 | [requirements-modern.txt](requirements-modern.txt) |

Both install Mininet 2.3+, Open vSwitch, iperf/iperf3 and hping3, and create
`venv/`. The chosen backend is written to `.controller_backend`, and `run.sh`
starts the matching controller. On Ubuntu, if Ryu fails to install,
`setup.sh` switches to the modern path automatically.

os-ken is the maintained fork of Ryu with the same API. Current releases ship
neither `osken-manager` nor Ryu's WSGI module, so the project includes
[controller/osken_launcher.py](controller/osken_launcher.py) and
[controller/wsgi_fallback.py](controller/wsgi_fallback.py). The controller code
is identical on both backends (imports go through
[controller/compat.py](controller/compat.py)).

### Running on Kali Linux
1. Use Kali in a VM (VirtualBox/VMware) or a real install. WSL is not supported
   because Mininet needs Open vSwitch and `tc` support in the kernel. Give the VM
   2 CPUs and 4 GB RAM.
2. Update Kali and reboot so the running kernel matches the installed modules:
   `sudo apt update && sudo apt full-upgrade -y && sudo reboot`
3. Copy the `sdn-load-balancer` folder into Kali, for example to
   `~/sdn-load-balancer` (shared folder, USB stick, or `git clone`). Avoid paths
   with spaces.
4. `cd ~/sdn-load-balancer && chmod +x *.sh && ./setup.sh`. It should print
   `Detected distro: kali -> using the modern install path` and finish with
   `controller backend: os-ken`.
5. Check it: `source venv/bin/activate && python -m pytest -q`. All tests should
   pass.
6. Check Open vSwitch: `sudo ovs-vsctl show` should print a version line and no
   error, and `sudo mn --switch ovsbr --controller none --test pingall` should
   report `0% dropped`. That test runs OVS as a plain bridge, without our
   controller, then `sudo mn -c` cleans up.
7. Start the project: `./run.sh`, open **http://localhost:5000** in Kali's
   browser, then in the Mininet CLI run `pingall`. Then follow the milestone
   table in section 3.

Kali-specific problems:
* `modprobe: FATAL: Module openvswitch not found` → the kernel was upgraded but
  not rebooted (step 2), or `linux-headers` are missing:
  `sudo apt install -y linux-headers-$(uname -r)`, then reboot.
* `externally-managed-environment` from pip → you ran pip outside the venv; run
  `source venv/bin/activate` first (setup.sh always uses the venv).
* Port 5000 or 8080 already in use (e.g. by a Kali tool) → change
  `DASHBOARD_PORT` / `CONTROLLER_REST_PORT` in `config.py`.

## 2. Setup and run
```bash
cd sdn-load-balancer
chmod +x *.sh
./setup.sh
```
```bash
./run.sh
```
`run.sh` starts, in order: the controller → the dashboard → the Mininet topology
(with the Mininet CLI). Open **http://localhost:5000**. Type `exit` in the CLI to
stop everything. Use `./run.sh --headless` to run the topology in the background
(needed by `demo.sh`, the experiments and the AI data collector).

Stop and clean up (also runs `sudo mn -c`):
```bash
./stop.sh
```

All tunables (IPs, ports, bandwidth, intervals, thresholds) are in
[config.py](config.py).

## 3. Milestone-by-milestone testing

| # | What to run | Expected result |
|---|---|---|
| 1 | `./run.sh`, then in the CLI: `pingall` | `0% dropped` (the first run right after start may lose a few pings while LLDP finishes, so repeat once) |
| 2 | CLI: `h1 ping -c3 srv1`; `sh ovs-ofctl -O OpenFlow13 dump-flows s1`; `curl -s localhost:8080/api/topology` | 10 switch links in the API; `cookie=0x5d…` rules with `idle_timeout=10, hard_timeout=60` on the path |
| 3 | CLI: `h1 curl -s 10.0.0.100` (repeat), `h3 curl -s 10.0.0.100` | answers rotate srv1 → srv2 → srv3; change strategy on the dashboard and repeat |
| 4 | CLI: `h1 iperf -c 10.0.0.100 -t 30 &`, then the dashboard **Link Down** on the link that is carrying it | Event log: `link_down` then `recovery … in X ms`; iperf continues; summary shows the last recovery time |
| 5 | Dashboard: **Kill** srv1; heavy **TCP x4** from all clients; **Launch** SYN flood from h4 | srv1 marked down within ~6 s and leaves the pool; after 15 s above 75 % `scale_up` adds srv4; h4 gets a red marker and an `attack_blocked` event |
| 6 | `python ai/collect_data.py --minutes 20`, then `python ai/train.py` | metrics table printed; model saved; restart and the summary shows the model name instead of `fallback-moving-average` |
| 7 | Open the dashboard; stop the controller (`pkill -f sdn_controller`) | live updates every 1.5 s; when stopped, the page shows "Controller disconnected" and keeps working |
| 8 | `python experiments/run_experiments.py --duration 20` | `experiments/results/results_latest.csv` plus PNG graphs |
| – | `python -m pytest -q` | all tests pass |

Commands that use the venv (`python …`, `pytest`) need `source venv/bin/activate`
first, and `export PYTHONPATH=$(pwd)` for the scripts.

## 4. Demo scenarios

**Guided 5-minute demo:**
```bash
./demo.sh
```
It starts everything headless if needed, then runs these steps and prints the
controller summary and events after each one: normal traffic → heavy load
(auto-scaling) → link failure (reroute) → server failure (pool update) → DDoS
(blocked) → recovery. Keep the dashboard open beside it.

**Individual scenarios from the dashboard (Controls panel):**
* *Strategy:* pick `round_robin` / `least_load` / `ai_predictive` and press Apply.
* *Link failure:* choose a switch link → Down (it turns grey and dashed), Up to restore.
* *Server failure:* choose a server → Kill / Restore.
* *Heavy load:* Traffic burst, source "all clients", TCP x4, 60 s.
* *DDoS:* choose an attacker and SYN/UDP/ICMP flood → Launch; Unblock from the
  Security panel or the Unblock IP box.
* *Export:* CSV of link stats, predictions, server stats or events from SQLite.

**From the command line** (Mininet control API, while running):
```bash
curl -X POST localhost:8001/link -d '{"a":"s3","b":"s5","state":"down"}'
```
```bash
curl -X POST localhost:8001/attack -d '{"src":"h4","type":"syn","duration":20}'
```
```bash
curl -X POST localhost:8080/api/strategy -d '{"strategy":"ai_predictive"}'
```

## 5. Experiments
```bash
source venv/bin/activate && export PYTHONPATH=$(pwd)
./run.sh --headless
python experiments/run_experiments.py --duration 20
```
Runs normal, heavy, link_failure, server_failure and ddos under all three
strategies (about 15 runs of ~45 s). It measures throughput, latency, jitter,
loss, server-load and connection standard deviation, recovery time and detection
time, and writes CSV plus PNG bar charts to `experiments/results/`. Re-plot an
old CSV with `--plot-only <file>`.

## 6. Project layout
```
config.py            all settings
topology/            Mininet topology + local control API
controller/          Ryu app and modules (routing, LB, stats, health, security, REST)
ai/                  collect_data.py, train.py, predictor.py, features.py
dashboard/           Flask + Socket.IO backend, template, JS (D3, Chart.js), CSS
experiments/         run_experiments.py, results/
common/              SQLite store, logging setup
tests/               pytest suite
docs/                architecture, modules, message flow, AI results, viva Q&A
logs/  data/         created at runtime (timestamped logs, SQLite DB)
setup.sh run.sh stop.sh demo.sh
```

## 7. REST API (controller, port 8080)
| Method | Path | Returns |
|---|---|---|
| GET | `/api/topology` | switches, hosts, links (with utilisation, prediction, weight) |
| GET | `/api/stats` | per-link utilisation, Mbps, drops, flows + rolling history |
| GET | `/api/servers` | pool, health, load, active flows, mapped clients |
| GET | `/api/predictions` | predicted vs actual per link, model name |
| GET | `/api/events?since=<id>` | recent events |
| GET | `/api/security` | blocked IPs, per-source rates |
| GET | `/api/summary` | numbers for the summary bar |
| POST | `/api/strategy` | `{"strategy": "round_robin" \| "least_load" \| "ai_predictive"}` |
| POST | `/api/unblock` | `{"ip": "10.0.0.4"}` |

## 8. Troubleshooting
| Problem | Fix |
|---|---|
| `ImportError: cannot import name 'ALREADY_HANDLED'` | eventlet too new: `pip install eventlet==0.30.2` (pinned in requirements.txt) |
| Ryu install fails with `get_script_args` / `use_2to3` errors | `pip install setuptools==57.5.0` then `pip install --no-build-isolation ryu==4.34`; setup.sh does this and falls back to os-ken |
| `dnspython` / `greenlet` errors when starting Ryu | `pip install dnspython==1.16.0 greenlet==1.1.3` |
| `ovs-vsctl: unix:/var/run/openvswitch/db.sock: database connection failed` | `sudo systemctl start openvswitch-switch` (or `sudo service openvswitch-switch start`) |
| `Exception: Error creating interface pair` / `File exists` | a previous run was not cleaned: `./stop.sh` or `sudo mn -c` |
| Port 6653 already in use | `sudo systemctl stop openvswitch-testcontroller` and `pkill -f ryu-manager` |
| Pings fail right after start | the controller ignores traffic until LLDP has found all 10 links (≤ 10 s); wait and retry |
| `pingall` fails for every host | check `logs/controller_console_*.log`; switches must use OpenFlow13 (`sudo ovs-vsctl get bridge s1 protocols`) |
| Dashboard shows "controller disconnected" | the controller is not running or not on port 8080; check `curl localhost:8080/api/health` and the controller log |
| Mininet pill shows "not running" | the topology (control API on 8001) is not up; start `./run.sh` |
| Links occasionally deleted under very heavy load | LLDP frames were dropped by full queues; the link is rediscovered within seconds. Lower traffic or raise `LINK_MAX_QUEUE` |
| `hping3: command not found` | `sudo apt install hping3` |

Logs: `logs/controller_<timestamp>.log`, `logs/dashboard_<timestamp>.log`,
`logs/*_console_*.log` (stdout/stderr), `logs/topology_*.log` (headless mode).

## 9. Tests
```bash
source venv/bin/activate
python -m pytest -q
```
Covers path computation, load-balancing strategy selection, DDoS thresholds,
auto-scaling hysteresis, the predictor fallback and training pipeline, iperf/ping
parsing and SQLite persistence. When Ryu or os-ken is installed it also runs the
controller logic against fake switches, encoding every OpenFlow message with the
framework's real 1.3 encoder.

## 10. Notes and limitations
* The dashboard shows only values from the controller, the Mininet API or the
  database. Nothing is simulated in the UI; with no traffic, values are 0.
* The AI model is not pre-trained: it must be trained on data from your own
  network run. Until then the moving-average fallback is used and the dashboard
  says so.
* Optional QoS (meters and OVS queues) is not implemented; it is listed as
  future work.
