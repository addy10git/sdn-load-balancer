#!/usr/bin/env python3
"""
Run the same traffic scenarios under every load-balancing strategy and compare.

Scenarios: normal, heavy, link_failure, server_failure, ddos
Metrics:   throughput (Mbps, sum of measured clients), latency (ping avg ms),
           jitter (iperf UDP ms), packet loss (%), load distribution (std-dev of
           server load % and of connections assigned), recovery / detection time.

Requires the system to be running:   ./run.sh --headless
Usage:  python experiments/run_experiments.py [--duration 20] [--strategies ...] [--scenarios ...]
Output: experiments/results/results_<timestamp>.csv, results_latest.csv, *.png
"""
import argparse
import csv
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

SCENARIOS = ["normal", "heavy", "link_failure", "server_failure", "ddos"]
FAIL_LINK = ("s3", "s5")
FAIL_SERVER = "srv1"
ATTACKER = "h4"
FIELDS = ["strategy", "scenario", "throughput_mbps", "latency_ms", "jitter_ms", "loss_pct",
          "load_std", "assigned_std", "recovery_ms", "detection_s", "duration_s"]


# ===================================================================== API helpers
def ctl(path, payload=None, timeout=5):
    url = config.CONTROLLER_API + path
    resp = requests.post(url, json=payload, timeout=timeout) if payload is not None \
        else requests.get(url, timeout=timeout)
    return resp.json()


def mn(path, payload=None, timeout=10):
    url = config.MININET_API + path
    resp = requests.post(url, json=payload, timeout=timeout) if payload is not None \
        else requests.get(url, timeout=timeout)
    return resp.json()


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ===================================================================== scenario steps
def reset_network():
    """Everything back to a clean baseline before each run."""
    mn("/stop_traffic", {})
    for a, b in config.SWITCH_LINKS:
        mn("/link", {"a": a, "b": b, "state": "up"})
    for name in config.SERVERS:
        mn("/server", {"name": name, "action": "start"})
    for ip in ctl("/api/security").get("blocked", []):
        ctl("/api/unblock", {"ip": ip["ip"]})
    deadline = time.time() + 60
    primaries = [n for n, s in config.SERVERS.items() if not s["standby"]]
    while time.time() < deadline:
        servers = {s["name"]: s for s in ctl("/api/servers")["servers"]}
        if all(servers[n]["health"] == "healthy" for n in primaries):
            break
        time.sleep(2)
    # Let old rules idle out (idle_timeout) and utilisation drop back to zero.
    time.sleep(config.FLOW_IDLE_TIMEOUT + 4)


class LoadSampler(threading.Thread):
    """Samples server load % every stats interval while a test runs."""

    def __init__(self):
        super(LoadSampler, self).__init__(daemon=True)
        self.samples = {name: [] for name in config.SERVERS}
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            try:
                for s in ctl("/api/servers")["servers"]:
                    if s["in_pool"]:
                        self.samples[s["name"]].append(s["load"])
            except (requests.RequestException, ValueError):
                pass
            self.stop_event.wait(config.STATS_INTERVAL)

    def load_std(self):
        means = [statistics.mean(v) for v in self.samples.values() if v]
        return statistics.pstdev(means) if len(means) > 1 else 0.0


def assigned_counts():
    return {s["name"]: s["total_assigned"] for s in ctl("/api/servers")["servers"]}


def run_measurements(duration, tcp_clients, probe_client, parallel=1):
    """Concurrent TCP iperf from tcp_clients + UDP jitter and ping from probe_client."""
    jobs = {}
    with ThreadPoolExecutor(max_workers=len(tcp_clients) + 2) as pool:
        for c in tcp_clients:
            jobs[c] = pool.submit(mn, "/measure", {"type": "iperf", "src": c, "duration": duration,
                                                   "parallel": parallel}, duration + 30)
        jobs["udp"] = pool.submit(mn, "/measure", {"type": "iperf", "src": probe_client, "udp": True,
                                                   "bw": "1M", "duration": duration}, duration + 30)
        jobs["ping"] = pool.submit(mn, "/measure", {"type": "ping", "src": probe_client,
                                                    "count": int(duration / 0.2)}, duration + 30)
        res = {k: f.result() for k, f in jobs.items()}
    throughput = sum(res[c].get("throughput_mbps") or 0.0 for c in tcp_clients)
    return {"throughput_mbps": round(throughput, 3),
            "latency_ms": res["ping"].get("avg_ms"),
            "jitter_ms": res["udp"].get("jitter_ms"),
            "loss_pct": res["ping"].get("loss_pct")}


def wait_for(predicate, timeout, interval=0.25):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if predicate():
                return time.time() - start
        except (requests.RequestException, ValueError, KeyError):
            pass
        time.sleep(interval)
    return None


def run_scenario(strategy, scenario, duration):
    reset_network()
    ctl("/api/strategy", {"strategy": strategy})
    before = assigned_counts()
    sampler = LoadSampler()
    sampler.start()
    row = {"strategy": strategy, "scenario": scenario, "recovery_ms": None, "detection_s": None,
           "duration_s": duration}
    clients = ["h1", "h2", "h3"]

    if scenario == "heavy":
        for c in config.CLIENTS:
            mn("/traffic", {"src": c, "mode": "tcp", "parallel": 3, "duration": duration + 5})

    side_result = {}

    def failure_action():
        time.sleep(duration / 3.0)
        t_fail = time.time()
        if scenario == "link_failure":
            mn("/link", {"a": FAIL_LINK[0], "b": FAIL_LINK[1], "state": "down"})
            found = wait_for(lambda: any(r["ts"] >= t_fail for r in ctl("/api/summary")["recoveries"]), 15)
            if found is not None:
                rec = [r for r in ctl("/api/summary")["recoveries"] if r["ts"] >= t_fail][-1]
                side_result["recovery_ms"] = rec["recovery_ms"]
        elif scenario == "server_failure":
            mn("/server", {"name": FAIL_SERVER, "action": "stop"})
            side_result["detection_s"] = wait_for(lambda: FAIL_SERVER not in ctl("/api/servers")["pool"], 30)
            if side_result["detection_s"] is not None:
                side_result["recovery_ms"] = round(side_result["detection_s"] * 1000, 1)

    def attack_action():
        ip = config.CLIENTS[ATTACKER]["ip"]
        mn("/attack", {"src": ATTACKER, "type": "syn", "duration": duration})
        side_result["detection_s"] = wait_for(lambda: ip in ctl("/api/summary")["blocked_now"], duration)

    helper = None
    if scenario in ("link_failure", "server_failure"):
        helper = threading.Thread(target=failure_action, daemon=True)
    elif scenario == "ddos":
        helper = threading.Thread(target=attack_action, daemon=True)
    if helper:
        helper.start()

    row.update(run_measurements(duration, clients, "h4" if scenario != "ddos" else "h3"))
    if helper:
        helper.join(timeout=30)
    sampler.stop_event.set()
    sampler.join(timeout=5)
    after = assigned_counts()
    deltas = [after[n] - before.get(n, 0) for n in config.SERVERS
              if not config.SERVERS[n]["standby"]]
    row["load_std"] = round(sampler.load_std(), 3)
    row["assigned_std"] = round(statistics.pstdev(deltas), 3) if len(deltas) > 1 else 0.0
    row.update({k: v for k, v in side_result.items() if v is not None})
    if row.get("detection_s") is not None:
        row["detection_s"] = round(row["detection_s"], 2)
    mn("/stop_traffic", {})
    return row


# ===================================================================== output
def save_csv(rows, path):
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in FIELDS})


def plot(rows, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = ["#2a78d6", "#eb6834", "#1baf7a"]  # fixed order: one colour per strategy
    strategies = list(dict.fromkeys(r["strategy"] for r in rows))
    scenarios = list(dict.fromkeys(r["scenario"] for r in rows))
    metrics = [("throughput_mbps", "Throughput (Mbps)"), ("latency_ms", "Latency (ms)"),
               ("jitter_ms", "Jitter (ms)"), ("loss_pct", "Packet loss (%)"),
               ("load_std", "Server load std-dev (%)"), ("assigned_std", "Connections std-dev"),
               ("recovery_ms", "Recovery / detection (ms)"), ("detection_s", "Attack/failure detection (s)")]
    written = []
    width = 0.8 / max(1, len(strategies))
    for key, title in metrics:
        if all(r.get(key) in (None, "") for r in rows):
            continue
        fig, ax = plt.subplots(figsize=(8, 4.2))
        for i, strat in enumerate(strategies):
            vals = []
            for sc in scenarios:
                match = [r for r in rows if r["strategy"] == strat and r["scenario"] == sc]
                v = match[0].get(key) if match else None
                vals.append(float(v) if v not in (None, "") else 0.0)
            xs = [j + (i - (len(strategies) - 1) / 2.0) * width for j in range(len(scenarios))]
            ax.bar(xs, vals, width * 0.92, label=strat, color=colors[i % len(colors)])
        ax.set_xticks(range(len(scenarios)))
        ax.set_xticklabels(scenarios)
        ax.set_title(title)
        ax.grid(axis="y", color="#e4e3df", linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.legend(frameon=False)
        fig.tight_layout()
        path = os.path.join(out_dir, "%s.png" % key)
        fig.savefig(path, dpi=130)
        plt.close(fig)
        written.append(path)
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration", type=int, default=20, help="seconds per measurement")
    parser.add_argument("--strategies", nargs="+", default=config.LB_STRATEGIES)
    parser.add_argument("--scenarios", nargs="+", default=SCENARIOS, choices=SCENARIOS)
    parser.add_argument("--plot-only", help="re-plot an existing results CSV")
    args = parser.parse_args()
    os.makedirs(config.RESULTS_DIR, exist_ok=True)

    if args.plot_only:
        with open(args.plot_only) as fh:
            rows = list(csv.DictReader(fh))
        for p in plot(rows, config.RESULTS_DIR):
            print("wrote", p)
        return

    try:
        ctl("/api/health")
        mn("/status")
    except requests.RequestException as exc:
        sys.exit("Controller or Mininet API not reachable (%s). Start ./run.sh --headless first." % exc)

    rows = []
    total = len(args.strategies) * len(args.scenarios)
    for strategy in args.strategies:
        for scenario in args.scenarios:
            log("(%d/%d) strategy=%s scenario=%s" % (len(rows) + 1, total, strategy, scenario))
            row = run_scenario(strategy, scenario, args.duration)
            log("  -> " + ", ".join("%s=%s" % (k, row.get(k)) for k in FIELDS[2:]))
            rows.append(row)

    reset_network()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(config.RESULTS_DIR, "results_%s.csv" % stamp)
    save_csv(rows, path)
    save_csv(rows, os.path.join(config.RESULTS_DIR, "results_latest.csv"))
    log("results saved to %s" % path)
    for p in plot(rows, config.RESULTS_DIR):
        log("graph: %s" % p)


if __name__ == "__main__":
    main()
