#!/usr/bin/env bash
# Stop the dashboard, controller, topology and any traffic, then clean Mininet.
#   ./stop.sh [--quiet]
cd "$(dirname "$0")"
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1
say() { [ "$QUIET" = 1 ] || printf '\033[1;36m==> %s\033[0m\n' "$*"; }

stop_pid_file() {
    local file="logs/$1.pid"
    if [ -f "$file" ]; then
        kill "$(cat "$file")" 2>/dev/null || sudo kill "$(cat "$file")" 2>/dev/null || true
        rm -f "$file"
    fi
}

# Patterns use the "[x]yz" trick: the regex matches "xyz" but not the pkill/sudo
# command line itself (which contains "[x]yz"), so pkill never kills its own sudo.
say "Stopping dashboard and controller"
stop_pid_file dashboard
stop_pid_file controller
pkill -f "[d]ashboard/app.py" 2>/dev/null || true
pkill -f "[c]ontroller.sdn_controller" 2>/dev/null || true

say "Stopping Mininet topology and traffic generators"
if pgrep -f "[t]opology/topology.py" >/dev/null; then
    sudo pkill -INT -f "[t]opology/topology.py" 2>/dev/null || true
    sleep 2
    sudo pkill -9 -f "[t]opology/topology.py" 2>/dev/null || true
fi
sudo pkill -f "[h]ping3" 2>/dev/null || true
sudo pkill -f "[i]perf -" 2>/dev/null || true
sudo pkill -f "[h]ttp.server" 2>/dev/null || true

say "Running sudo mn -c"
if [ "$QUIET" = 1 ]; then sudo mn -c >/dev/null 2>&1; else sudo mn -c; fi
say "Stopped."
