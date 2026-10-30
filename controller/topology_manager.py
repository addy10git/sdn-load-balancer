"""Network view: switch graph (networkx), switch ports and host locations.

Updated from Ryu topology events (LLDP discovery) and PacketIn host learning.
No Ryu imports, so it is easy to test.
"""
import logging
import time

import networkx as nx

import config
from controller.path_utils import edge_key

LOG = logging.getLogger("sdn.topology")


class TopologyManager(object):
    def __init__(self, expected_links=None, settle_seconds=config.TOPOLOGY_SETTLE_SECONDS):
        self.graph = nx.Graph()
        self.switch_ports = {}        # dpid -> set(port_no)
        self.hosts = {}               # mac -> {"ip", "dpid", "port", "last_seen"}
        self.ip_to_mac = {}
        self.expected_links = expected_links if expected_links is not None else len(config.SWITCH_LINKS)
        self.settle_seconds = settle_seconds
        self.first_switch_ts = None
        self.last_change = time.time()

    # ------------------------------------------------------------ switches
    def add_switch(self, dpid, ports):
        self.graph.add_node(dpid)
        self.switch_ports[dpid] = set(ports)
        if self.first_switch_ts is None:
            self.first_switch_ts = time.time()
        self.last_change = time.time()
        LOG.info("switch s%d joined with ports %s", dpid, sorted(ports))

    def remove_switch(self, dpid):
        if dpid in self.graph:
            self.graph.remove_node(dpid)
        self.switch_ports.pop(dpid, None)
        for mac in [m for m, h in self.hosts.items() if h["dpid"] == dpid]:
            self._forget_host(mac)
        self.last_change = time.time()
        LOG.info("switch s%d left", dpid)

    def add_port(self, dpid, port):
        self.switch_ports.setdefault(dpid, set()).add(port)

    def remove_port(self, dpid, port):
        self.switch_ports.get(dpid, set()).discard(port)

    # ------------------------------------------------------------ links
    def add_link(self, src_dpid, src_port, dst_dpid, dst_port):
        """Add/refresh an inter-switch link. Returns True if it is new."""
        is_new = not self.graph.has_edge(src_dpid, dst_dpid)
        if is_new:
            self.graph.add_edge(src_dpid, dst_dpid, weight=1.0,
                                ports={src_dpid: src_port, dst_dpid: dst_port})
            self.last_change = time.time()
            LOG.info("link s%d:%d <-> s%d:%d discovered", src_dpid, src_port, dst_dpid, dst_port)
        else:
            self.graph[src_dpid][dst_dpid]["ports"].update({src_dpid: src_port, dst_dpid: dst_port})
        # Anything previously learnt as a host on these ports was a mistake.
        for dpid, port in ((src_dpid, src_port), (dst_dpid, dst_port)):
            for mac in [m for m, h in self.hosts.items() if h["dpid"] == dpid and h["port"] == port]:
                self._forget_host(mac)
        return is_new

    def remove_link(self, a, b):
        """Remove the link between two switches. Returns True if it existed."""
        if self.graph.has_edge(a, b):
            self.graph.remove_edge(a, b)
            self.last_change = time.time()
            LOG.warning("link s%d <-> s%d removed", a, b)
            return True
        return False

    def link_at_port(self, dpid, port):
        """The neighbour dpid of the inter-switch link using (dpid, port), or None."""
        if dpid not in self.graph:
            return None
        for nbr in self.graph.neighbors(dpid):
            if self.graph[dpid][nbr]["ports"].get(dpid) == port:
                return nbr
        return None

    def is_inter_switch_port(self, dpid, port):
        return self.link_at_port(dpid, port) is not None

    def edge_ports(self, dpid):
        """Ports of a switch that are not inter-switch links (where hosts live)."""
        ports = self.switch_ports.get(dpid, set())
        return sorted(p for p in ports if p < 0xffffff00 and not self.is_inter_switch_port(dpid, p))

    def links(self):
        """[(a, b, data)] with a < b."""
        return [(min(a, b), max(a, b), d) for a, b, d in self.graph.edges(data=True)]

    @property
    def ready(self):
        """True once LLDP has found every expected link or the settle time passed."""
        if self.graph.number_of_edges() >= self.expected_links:
            return True
        return (self.first_switch_ts is not None
                and time.time() - self.first_switch_ts >= self.settle_seconds)

    # ------------------------------------------------------------ hosts
    def learn_host(self, mac, ip, dpid, port):
        """Record where a host lives. Returns True if it is new or moved."""
        if self.is_inter_switch_port(dpid, port):
            return False
        now = time.time()
        known = self.hosts.get(mac)
        moved = known is None or known["dpid"] != dpid or known["port"] != port
        entry = known or {}
        entry.update({"dpid": dpid, "port": port, "last_seen": now})
        if ip:
            entry["ip"] = ip
            self.ip_to_mac[ip] = mac
        entry.setdefault("ip", None)
        self.hosts[mac] = entry
        if moved:
            LOG.info("host %s (%s) at s%d port %d", mac, entry["ip"], dpid, port)
        return moved

    def _forget_host(self, mac):
        host = self.hosts.pop(mac, None)
        if host and host.get("ip") and self.ip_to_mac.get(host["ip"]) == mac:
            self.ip_to_mac.pop(host["ip"], None)

    def host_by_ip(self, ip):
        mac = self.ip_to_mac.get(ip)
        if mac is None:
            return None
        host = self.hosts.get(mac)
        return dict(host, mac=mac) if host else None

    def host_by_mac(self, mac):
        host = self.hosts.get(mac)
        return dict(host, mac=mac) if host else None

    # ------------------------------------------------------------ weights
    def set_weight(self, a, b, weight):
        if self.graph.has_edge(a, b):
            self.graph[a][b]["weight"] = weight

    @staticmethod
    def link_id(a, b):
        a, b = edge_key(a, b)
        return "s%d-s%d" % (a, b)
