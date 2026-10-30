#!/usr/bin/env bash
# Collect everything needed to debug a non-working run. Run it while ./run.sh
# is running (in a second terminal):   ./diagnose.sh
cd "$(dirname "$0")"
section() { printf '\n===== %s =====\n' "$*"; }

section "backend / python"
cat .controller_backend 2>/dev/null || echo "(no .controller_backend)"
venv/bin/python --version 2>&1

section "controller REST /api/summary"
curl -s -m 3 localhost:8080/api/summary || echo "controller REST not reachable"
echo

section "processes"
pgrep -af "[o]sken_launcher|[r]yu-manager|[d]ashboard/app.py|[t]opology/topology.py" || echo "none found"

section "listening ports 6653 / 8080 / 8001 / 5000"
ss -ltnp 2>/dev/null | grep -E ":(6653|8080|8001|5000) " || echo "none of the ports are listening"

section "OVS: bridges and controller connection"
sudo ovs-vsctl show 2>&1 | grep -E "Bridge|Controller|is_connected|fail_mode|ovs_version" || echo "ovs-vsctl failed"
sudo ovs-vsctl get bridge s1 protocols 2>&1

section "flow table of s1"
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 2>&1 | head -20

section "controller log (errors, switches, links)"
LOG="$(ls -t logs/controller_console_*.log 2>/dev/null | head -1)"
echo "file: ${LOG:-none}"
if [ -n "$LOG" ]; then
    grep -iE "error|exception|traceback|refused|switch|link|ready" "$LOG" | tail -25
    echo "----- last 15 lines -----"
    tail -n 15 "$LOG"
fi
