# Likely viva questions with answers

**1. What is SDN and why use it for load balancing?**
SDN separates the control plane (the controller) from the data plane (the
switches). The controller has a global view of the topology and live statistics,
so it can choose a server *and* the path to it together, which a hardware load
balancer at one point in the network cannot do.

**2. Why OpenFlow 1.3?**
It supports multiple tables, `set_field` actions (needed to rewrite IP/MAC for
the VIP), per-port and per-flow statistics, meters, and cookies with masks, all of
which this project uses. It is also the best-supported version in OVS and Ryu.

**3. How does the controller discover the topology?**
Ryu's `switches` app (enabled with `--observe-links`) sends LLDP frames out of
every switch port via PacketOut. When a neighbouring switch receives one it sends
it to the controller as a PacketIn, which reveals the link (dpid/port on both
sides). Those events update a networkx graph.

**4. How do you avoid broadcast storms in a looped topology?**
Broadcasts never travel over inter-switch links. The controller answers ARP
itself (proxy ARP), and for unknown targets sends the request only out of
host-facing ports with PacketOut. The controller acts as the single root of the
broadcast tree, which has the same effect as flooding over a spanning tree.

**5. How is the VIP implemented?**
The controller answers ARP for 10.0.0.100 with a virtual MAC. For each new
connection it picks a server and installs rules: at the client's edge switch the
destination is rewritten to the server's IP/MAC, and on the return path the
source is rewritten back to the VIP. Clients only ever see the VIP.

**6. Why rewrite at the edge switch and not at the server side?**
The core then routes on real addresses, so normal shortest-path rules work, and
the reverse path can take a different route than the forward one if that is
cheaper.

**7. What is the difference between the three strategies?**
Round robin ignores load. Least load uses current measured access-link load (plus
active connections as a tie-breaker). AI-predictive uses the predicted load 10 s
ahead for both the server and the links on the path, so it avoids congestion
that is about to happen.

**8. How is link utilisation measured?**
Every 2 s the controller requests port statistics. Byte-counter differences
divided by the interval give bits per second; utilisation is the busier direction
divided by link capacity (10 Mbit/s).

**9. What are idle_timeout and hard_timeout?**
`idle_timeout=10` removes a rule after 10 s without traffic; `hard_timeout=60`
removes it after 60 s regardless, so paths are periodically recomputed with fresh
load information. The ingress rule has `SEND_FLOW_REM`, so the controller learns
when a path expired.

**10. How do you handle a link failure, and how is recovery time measured?**
PortStatus (link down) or LLDP link-delete triggers removal of the link from the
graph, deletion of every affected path's rules (by cookie), and installation of
new paths. A BarrierRequest is sent to each switch that got new rules; the
recovery time is from detection to the last BarrierReply, i.e. until all new
rules are confirmed installed.

**11. Why use cookies?**
Each installed path has a unique cookie, so it can be deleted precisely across
all switches, its flow statistics can be mapped back to a client/server, and a
restarted controller can remove only its own stale rules.

**12. How do health checks work without an IP stack on the controller?**
The controller crafts a TCP SYN from a probe address (10.0.0.254) and injects it
at the server's switch port with PacketOut. The SYN-ACK (healthy) or RST
(service down) comes back as a PacketIn; no answer means the host or link is down.
It tests the real data-plane path and the HTTP port, not just ARP.

**13. Explain the auto-scaling rule. Why hysteresis?**
Scale up when average pool load > 75 % for 15 s; scale down when < 30 % for 30 s.
Different thresholds and durations prevent flapping when load hovers near one
threshold.

**14. How is a DDoS attack detected?**
Three thresholds per source: packets per second (from flow counters), new flows
per second (from PacketIns) and the SYN-to-completed-handshake ratio. A SYN flood
from hping3 uses a new source port per packet, so it creates many new flows and
almost no completed handshakes.

**15. How is it mitigated, and why at the edge?**
A priority-1000 drop rule matching the attacker's port and source IP is
installed on its own access switch with `hard_timeout=60`. Dropping at the edge
stops the traffic before it uses any core bandwidth or reaches the controller,
and the hard timeout unblocks it automatically.

**16. Could the attack overload the controller itself?**
Each new flow causes a PacketIn, so a flood does load the controller. That is
why detection also runs inline on every PacketIn: the source is blocked within
about one second, after which the switch drops the packets without PacketIns.
OpenFlow meters to rate-limit PacketIns would be a further improvement.

**17. What does the AI model predict, and with what features?**
The utilisation of each link 10 s ahead, from the last five utilisation samples
and the number of active flows on the link. Linear Regression and Random Forest
are trained and compared on MAE/RMSE; the better one is saved.

**18. Why a time-ordered train/test split?**
The data is a time series. A random split would put samples from the same
moment in both sets and overestimate accuracy; training on the past and testing
on the future matches how the model is used.

**19. What if no model is trained?**
The predictor falls back to a moving average plus a damped trend, so routing
and the AI strategy still work.

**20. How does the prediction influence routing?**
A link predicted above 80 % gets +10 added to its Dijkstra cost, so new flows
avoid it. The AI strategy also avoids servers whose access link or path is
predicted to be congested.

**21. Why Ryu and what are its limitations?**
Ryu is Python, simple to extend, and has a topology API and WSGI REST support.
It is single-threaded (eventlet green threads) and no longer actively developed,
which is why it needs old pins (eventlet 0.30.2) and why the project supports
os-ken as a fallback.

**22. What is the role of TCLink?**
It applies Linux `tc` shaping (bandwidth, delay, queue size) to each virtual link,
so congestion, delay and drops behave like real 10 Mbit/s links.

**23. How do you measure load distribution fairness?**
The standard deviation of average server load and of connections assigned per
server during a test. Lower means a more even distribution.

**24. What happens to existing connections when a server dies?**
Its rules are deleted; new packets of those connections go to the controller and
are mapped to a healthy server. TCP connections to the dead server are lost
(no state transfer), but new connections succeed immediately.

**25. Why is the data plane fast if the controller is involved?**
Only the first packet of each flow goes to the controller. After the rules are
installed, all further packets are forwarded by the switches at line rate.

**26. Limitations and future work?**
Single controller (no high availability), per-connection rules do not scale to
millions of flows (wildcard or group-table based balancing would), threshold-based
DDoS detection can be evaded by slow attacks, and QoS with meters/queues is left
as an optional extension.
