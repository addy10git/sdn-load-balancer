#!/usr/bin/env bash
# Guided ~5 minute demo. Starts the system headless if it is not running, then:
#   normal traffic -> heavy load (auto-scaling) -> link failure (reroute)
#   -> server failure (removed from pool) -> DDoS (blocked) -> recovery
# Keep the dashboard open at http://localhost:5000 while it runs.
set -uo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source venv/bin/activate
export PYTHONPATH="$(pwd)"

CTL="$(python -c 'import config; print(config.CONTROLLER_API)')"
MN="$(python -c 'import config; print(config.MININET_API)')"

step() { printf '\n\033[1;35m======== STEP %s: %s ========\033[0m\n' "$1" "$2"; }
info() { printf '\033[0;36m  %s\033[0m\n' "$*"; }
post() { curl -s -X POST -H 'Content-Type: application/json' -d "$2" "$1"; echo; }
pause() { info "waiting $1 s ..."; sleep "$1"; }
summary() {
    curl -s "$CTL/api/summary" | python -c '
import json, sys
s = json.load(sys.stdin)
print("  throughput %.2f Mbps | flows %d | healthy %d/%d | pool %s | standby %s | attacks blocked %d | last recovery %s ms"
      % (s["total_mbps"], s["active_flows"], s["healthy_servers"], s["servers_total"], s["pool"],
         "ON" if s["standby_active"] else "off", s["attacks_blocked"], s["last_recovery_ms"]))'
}
events() {
    curl -s "$CTL/api/events?limit=$1" | python -c '
import json, sys, time
for e in json.load(sys.stdin)["events"]:
    print("  %s  %-16s %s" % (time.strftime("%H:%M:%S", time.localtime(e["ts"])), e["type"], e["message"]))'
}

if ! curl -sf "$MN/status" >/dev/null || ! curl -sf "$CTL/api/health" >/dev/null; then
    step 0 "Starting controller, dashboard and topology (headless)"
    ./run.sh --headless || exit 1
    pause 15
fi

step 1 "Normal traffic (round_robin): each client fetches the VIP"
post "$CTL/api/strategy" '{"strategy":"round_robin"}'
for h in h1 h2 h3 h4; do
    info "$h -> http://10.0.0.100/ answered by: $(post "$MN/http" "{\"src\":\"$h\",\"count\":3}" | python -c 'import json,sys; print(json.load(sys.stdin)["responses"])')"
done
for h in h1 h2; do post "$MN/traffic" "{\"src\":\"$h\",\"mode\":\"udp\",\"bw\":\"3M\",\"duration\":30}" >/dev/null; done
pause 20
summary

step 2 "Heavy load: all clients send parallel TCP streams (auto-scaling adds srv4 after 15 s > 75%)"
post "$CTL/api/strategy" '{"strategy":"least_load"}'
for h in h1 h2 h3 h4; do post "$MN/traffic" "{\"src\":\"$h\",\"mode\":\"tcp\",\"parallel\":4,\"duration\":60}" >/dev/null; done
pause 45
summary
events 5

step 3 "Link failure: s3-s5 goes down while traffic is flowing (flows rerouted)"
post "$MN/link" '{"a":"s3","b":"s5","state":"down"}'
pause 10
summary
events 4
post "$MN/link" '{"a":"s3","b":"s5","state":"up"}'
post "$MN/stop_traffic" '{}'
pause 10

step 4 "Server failure: HTTP/iperf on srv1 stopped (health check removes it from the pool)"
post "$CTL/api/strategy" '{"strategy":"ai_predictive"}'
post "$MN/server" '{"name":"srv1","action":"stop"}'
pause 12
summary
for h in h1 h3; do
    info "$h -> VIP answered by: $(post "$MN/http" "{\"src\":\"$h\",\"count\":3}" | python -c 'import json,sys; print(json.load(sys.stdin)["responses"])')"
done
events 4

step 5 "DDoS: SYN flood from h4 to the VIP (detected and blocked at h4's edge switch)"
post "$MN/attack" '{"src":"h4","type":"syn","duration":20}'
pause 12
summary
curl -s "$CTL/api/security" | python -c '
import json, sys
for b in json.load(sys.stdin)["blocked"]:
    print("  BLOCKED %s (%s) %ss left: %s" % (b["ip"], b["host"], b["remaining"], "; ".join(b["reasons"])))'
events 3

step 6 "Recovery: restore srv1, unblock h4, network back to normal"
post "$MN/server" '{"name":"srv1","action":"start"}'
post "$CTL/api/unblock" '{"ip":"10.0.0.4"}'
post "$MN/stop_traffic" '{}'
post "$CTL/api/strategy" '{"strategy":"round_robin"}'
pause 12
summary
events 6
printf '\n\033[1;32mDemo complete. Explore the dashboard, or run ./stop.sh to shut everything down.\033[0m\n'
