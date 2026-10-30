#!/usr/bin/env python3
"""
Generate varied traffic so the controller's stats module logs training data.

The controller appends one row per link every STATS_INTERVAL to
ai/data/link_stats.csv. This script only drives traffic through the Mininet
control API in rounds of three patterns:
    steady  constant UDP streams from two clients
    bursty  short TCP bursts from random clients with random gaps
    heavy   all clients run parallel TCP streams at once
Usage (with ./run.sh --headless already running):
    python ai/collect_data.py --minutes 20
"""
import argparse
import os
import random
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

CLIENTS = sorted(config.CLIENTS)


def api(path, payload=None):
    url = config.MININET_API + path
    try:
        if payload is None:
            resp = requests.get(url, timeout=5)
        else:
            resp = requests.post(url, json=payload, timeout=5)
        return resp.json()
    except requests.RequestException as exc:
        sys.exit("Mininet control API not reachable at %s (%s). Start ./run.sh --headless first."
                 % (config.MININET_API, exc))


def steady(rng, seconds):
    for src in rng.sample(CLIENTS, 2):
        bw = "%dM" % rng.randint(2, 6)
        api("/traffic", {"src": src, "mode": "udp", "bw": bw, "duration": seconds})
    time.sleep(seconds)


def bursty(rng, seconds):
    end = time.time() + seconds
    while time.time() < end:
        length = rng.randint(3, 8)
        api("/traffic", {"src": rng.choice(CLIENTS), "mode": "tcp", "duration": length,
                         "parallel": rng.randint(1, 3)})
        time.sleep(length + rng.uniform(1, 6))


def heavy(rng, seconds):
    for src in CLIENTS:
        api("/traffic", {"src": src, "mode": "tcp", "duration": seconds, "parallel": rng.randint(2, 4)})
    time.sleep(seconds)


def idle(rng, seconds):
    time.sleep(seconds)


PATTERNS = [("steady", steady), ("bursty", bursty), ("heavy", heavy), ("idle", idle)]


def rows_in_csv():
    if not os.path.exists(config.STATS_CSV):
        return 0
    with open(config.STATS_CSV) as fh:
        return max(0, sum(1 for _ in fh) - 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=float, default=20.0, help="total collection time")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    api("/status")
    start_rows = rows_in_csv()
    deadline = time.time() + args.minutes * 60
    round_no = 0
    print("Collecting for %.1f minutes; the controller writes %s" % (args.minutes, config.STATS_CSV))
    while time.time() < deadline:
        round_no += 1
        name, pattern = rng.choice(PATTERNS)
        seconds = int(min(rng.randint(30, 60), max(5, deadline - time.time())))
        print("[round %d] %-6s for %ds  (rows so far: %d)" % (round_no, name, seconds,
                                                           rows_in_csv() - start_rows))
        pattern(rng, seconds)
        api("/stop_traffic", {})
        time.sleep(4)  # let utilisation settle between patterns
    print("Done. %d new rows in %s. Next: python ai/train.py" % (rows_in_csv() - start_rows,
                                                                 config.STATS_CSV))


if __name__ == "__main__":
    main()
