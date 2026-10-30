#!/usr/bin/env bash
# Start the controller, the dashboard and the Mininet topology, in that order.
#   ./run.sh              Mininet CLI in this terminal (exit the CLI to stop everything)
#   ./run.sh --headless   everything in the background (used by demo.sh / experiments)
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
MODE=cli
[ "${1:-}" = "--headless" ] && MODE=headless

say() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
cfg() { python -c "import config; print(config.$1)"; }

if [ ! -d venv ]; then echo "venv missing: run ./setup.sh first"; exit 1; fi
# shellcheck disable=SC1091
source venv/bin/activate
export PYTHONPATH="$ROOT"
mkdir -p logs data ai/data ai/models

REST_PORT="$(cfg CONTROLLER_REST_PORT)"
OF_PORT="$(cfg OPENFLOW_PORT)"
DASH_PORT="$(cfg DASHBOARD_PORT)"
MN_API="$(cfg MININET_API)"
BACKEND="$(cat .controller_backend 2>/dev/null || true)"
if [ -z "$BACKEND" ]; then
    # setup.sh did not record a backend: use whichever framework the venv has.
    if python -c "import ryu.app.wsgi" 2>/dev/null; then BACKEND=ryu
    elif python -c "import os_ken.topology.api" 2>/dev/null; then BACKEND=os-ken
    else echo "Neither Ryu nor os-ken is installed in venv: run ./setup.sh and check it finishes"; exit 1
    fi
fi
MANAGER=(ryu-manager)
WSAPI_ARGS=(--wsapi-port "$REST_PORT")
if [ "$BACKEND" = "os-ken" ]; then
    # os-ken has no manager command and no --wsapi-port; see controller/osken_launcher.py
    # and controller/wsgi_fallback.py (which reads the port from config.py).
    MANAGER=(python -m controller.osken_launcher)
    WSAPI_ARGS=()
fi
TS="$(date +%Y%m%d_%H%M%S)"

sudo -v   # ask for the sudo password once, up front
say "Cleaning previous Mininet state"
./stop.sh --quiet || true
sudo systemctl start openvswitch-switch || sudo service openvswitch-switch start

say "Starting controller (${MANAGER[*]}) on OpenFlow :$OF_PORT, REST :$REST_PORT"
nohup "${MANAGER[@]}" --observe-links --ofp-tcp-listen-port "$OF_PORT" "${WSAPI_ARGS[@]}" \
    controller.sdn_controller > "logs/controller_console_$TS.log" 2>&1 &
echo $! > logs/controller.pid
for _ in $(seq 1 30); do
    curl -sf "http://127.0.0.1:$REST_PORT/api/health" >/dev/null && break
    sleep 1
done
if ! curl -sf "http://127.0.0.1:$REST_PORT/api/health" >/dev/null; then
    echo "controller did not start. Last lines of logs/controller_console_$TS.log:"
    tail -n 25 "logs/controller_console_$TS.log"
    exit 1
fi

say "Starting dashboard on http://localhost:$DASH_PORT"
nohup python dashboard/app.py > "logs/dashboard_console_$TS.log" 2>&1 &
echo $! > logs/dashboard.pid

SYS_PY=/usr/bin/python3   # Mininet is installed for the system interpreter, not the venv
if [ "$MODE" = headless ]; then
    say "Starting Mininet topology (headless)"
    sudo -b "$SYS_PY" topology/topology.py --no-cli > "logs/topology_$TS.log" 2>&1
    for _ in $(seq 1 60); do
        curl -sf "$MN_API/status" >/dev/null && break
        sleep 1
    done
    curl -sf "$MN_API/status" >/dev/null \
        || { echo "topology did not start, see logs/topology_$TS.log"; exit 1; }
    say "All components running. Dashboard: http://localhost:$DASH_PORT   Stop with ./stop.sh"
else
    say "Starting Mininet topology (CLI). Dashboard: http://localhost:$DASH_PORT"
    say "Type 'exit' in the Mininet CLI to stop everything."
    sudo "$SYS_PY" topology/topology.py || true
    ./stop.sh
fi
