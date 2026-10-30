"""
Central configuration for the Intelligent SDN Load Balancer.

Every tunable value (addresses, ports, intervals, thresholds, bandwidth) lives
here so the topology, controller, AI, dashboard and experiment scripts all agree.
This file has no third-party imports so it can be loaded by the Mininet script
(system python3 under sudo) as well as by the controller virtualenv.
"""
import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "sdn.db")                 # SQLite history
STATS_CSV = os.path.join(BASE_DIR, "ai", "data", "link_stats.csv")  # AI training data
MODEL_PATH = os.path.join(BASE_DIR, "ai", "models", "congestion_model.joblib")
RESULTS_DIR = os.path.join(BASE_DIR, "experiments", "results")

# ---------------------------------------------------------------------------
# Controller / API endpoints
# ---------------------------------------------------------------------------
CONTROLLER_IP = "127.0.0.1"
OPENFLOW_PORT = 6653
CONTROLLER_REST_PORT = 8080          # Ryu WSGI (--wsapi-port)
CONTROLLER_API = "http://127.0.0.1:%d" % CONTROLLER_REST_PORT

MININET_API_HOST = "127.0.0.1"       # control interface inside the Mininet script
MININET_API_PORT = 8001
MININET_API = "http://%s:%d" % (MININET_API_HOST, MININET_API_PORT)

DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 5000
DASHBOARD_PUSH_INTERVAL = 1.5        # seconds between Socket.IO pushes
API_TIMEOUT = 1.5                    # seconds for dashboard -> controller calls

# ---------------------------------------------------------------------------
# Topology: 6 switches in a partial mesh, 4 clients, 3 active + 1 standby server
#
#   h1,h2 - s1 ---- s3 ---- s5 - srv1,srv2
#            \    /  |  \  /
#             \  /   |   \/
#              \/    |   /\
#   h3,h4 - s2 ---- s4 ---- s6 - srv3,srv4
# ---------------------------------------------------------------------------
SWITCHES = ["s1", "s2", "s3", "s4", "s5", "s6"]
SWITCH_LINKS = [
    ("s1", "s3"), ("s1", "s4"),
    ("s2", "s3"), ("s2", "s4"),
    ("s3", "s4"),
    ("s3", "s5"), ("s3", "s6"),
    ("s4", "s5"), ("s4", "s6"),
    ("s5", "s6"),
]

CLIENTS = {
    "h1": {"ip": "10.0.0.1", "mac": "00:00:00:00:00:01", "switch": "s1"},
    "h2": {"ip": "10.0.0.2", "mac": "00:00:00:00:00:02", "switch": "s1"},
    "h3": {"ip": "10.0.0.3", "mac": "00:00:00:00:00:03", "switch": "s2"},
    "h4": {"ip": "10.0.0.4", "mac": "00:00:00:00:00:04", "switch": "s2"},
}
SERVERS = {
    "srv1": {"ip": "10.0.0.11", "mac": "00:00:00:00:00:11", "switch": "s5", "standby": False},
    "srv2": {"ip": "10.0.0.12", "mac": "00:00:00:00:00:12", "switch": "s5", "standby": False},
    "srv3": {"ip": "10.0.0.13", "mac": "00:00:00:00:00:13", "switch": "s6", "standby": False},
    "srv4": {"ip": "10.0.0.14", "mac": "00:00:00:00:00:14", "switch": "s6", "standby": True},
}
SUBNET_PREFIX = 24

# Virtual IP shared by all servers; the controller answers ARP for it.
VIP = "10.0.0.100"
VIP_MAC = "00:00:00:00:01:00"
# Address the controller uses as the source of health-check probes.
PROBE_IP = "10.0.0.254"
PROBE_MAC = "00:00:00:00:02:54"

HTTP_PORT = 80
IPERF_PORT = 5001

# Link characteristics (TCLink)
LINK_BW_MBPS = 10
LINK_DELAY = "5ms"
HOST_LINK_BW_MBPS = 10
HOST_LINK_DELAY = "1ms"
LINK_MAX_QUEUE = 100

# ---------------------------------------------------------------------------
# Routing / flows
# ---------------------------------------------------------------------------
FLOW_IDLE_TIMEOUT = 10
FLOW_HARD_TIMEOUT = 60
TOPOLOGY_SETTLE_SECONDS = 10     # treat topology as ready after this, even if links are missing
UTIL_COST_WEIGHT = 4.0           # edge weight = 1 + UTIL_COST_WEIGHT * util/100 + prediction penalty
PREDICTION_CONGESTION_THRESHOLD = 80.0   # % predicted utilisation considered congested
PREDICTION_PENALTY = 10.0        # extra cost on links predicted to be congested

# Priorities
PRIORITY_TABLE_MISS = 0
PRIORITY_DROP_IPV6 = 1
PRIORITY_ROUTE = 10
PRIORITY_LB = 20
PRIORITY_BLOCK = 1000

# ---------------------------------------------------------------------------
# Load balancer
# ---------------------------------------------------------------------------
LB_STRATEGIES = ["round_robin", "least_load", "ai_predictive"]
DEFAULT_STRATEGY = "round_robin"
LB_SESSION_TTL = 120             # seconds a client connection stays pinned to a server

# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
STATS_INTERVAL = 2.0             # seconds between port/flow stats polls
HISTORY_LENGTH = 150             # samples kept in memory per link (150 * 2 s = 5 min)
LOG_STATS_CSV = True             # append link stats to STATS_CSV for AI training
DB_RETENTION_DAYS = 7

# ---------------------------------------------------------------------------
# Health checks and auto-scaling
# ---------------------------------------------------------------------------
HEALTH_CHECK_INTERVAL = 3.0
HEALTH_CHECK_TIMEOUT = 1.5       # seconds to wait for SYN-ACK
HEALTH_FAIL_THRESHOLD = 2        # consecutive failures before a server is marked down
HEALTH_CHECK_PORT = HTTP_PORT
SCALE_UP_LOAD = 75.0             # % average pool load
SCALE_UP_DURATION = 15.0         # seconds above SCALE_UP_LOAD before adding standby
SCALE_DOWN_LOAD = 30.0
SCALE_DOWN_DURATION = 30.0

# ---------------------------------------------------------------------------
# DDoS detection / mitigation
# ---------------------------------------------------------------------------
DDOS_PPS_THRESHOLD = 1500        # packets per second from one source
DDOS_NEW_FLOWS_PER_SEC = 40      # new flows per second from one source
DDOS_RATE_WINDOW = 2.0           # seconds of history for the new-flow rate
DDOS_SYN_ACK_RATIO = 3.0         # SYNs per completed handshake
DDOS_SYN_MIN_COUNT = 20          # minimum SYNs in window before the ratio is checked
DDOS_SYN_WINDOW = 10.0           # seconds of history for the SYN/ACK ratio
DDOS_BLOCK_SECONDS = 60          # hard_timeout of the drop rule (auto-unblock)
ATTACK_INTERVAL_US = 500         # hping3 -i u<N> used by simulated attacks (~2000 pps)

# ---------------------------------------------------------------------------
# AI predictor
# ---------------------------------------------------------------------------
PREDICTION_WINDOW = 5            # last N utilisation samples used as features
PREDICTION_HORIZON_SECONDS = 10  # predict utilisation this far ahead
PREDICTION_HORIZON_STEPS = max(1, int(round(PREDICTION_HORIZON_SECONDS / STATS_INTERVAL)))


def all_hosts():
    """Return {name: info} for clients and servers with a 'role' key added."""
    hosts = {}
    for name, info in CLIENTS.items():
        hosts[name] = dict(info, role="client")
    for name, info in SERVERS.items():
        hosts[name] = dict(info, role="server")
    return hosts


def host_name_by_ip(ip):
    for name, info in all_hosts().items():
        if info["ip"] == ip:
            return name
    return None
