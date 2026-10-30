#!/usr/bin/env python3
"""
Mininet topology for the Intelligent SDN Load Balancer.

  * 6 OpenFlow 1.3 switches in a partial mesh (multiple client->server paths)
  * 4 clients h1-h4, 3 active servers srv1-srv3, 1 standby server srv4
  * every server runs an HTTP server (port 80) and iperf servers (TCP+UDP 5001)
  * links are TCLinks with configurable bandwidth/delay
  * a small HTTP control API (see control_api.py) lets the dashboard, demo and
    experiment scripts bring links up/down, stop/start servers and start traffic

Run (needs root):  sudo python3 topology/topology.py [--no-cli]
"""
import argparse
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

from mininet.cli import CLI  # noqa: E402
from mininet.link import TCLink  # noqa: E402
from mininet.log import info, setLogLevel  # noqa: E402
from mininet.net import Mininet  # noqa: E402
from mininet.node import OVSKernelSwitch, RemoteController  # noqa: E402
from mininet.topo import Topo  # noqa: E402

from control_api import ControlServer, NetworkControl  # noqa: E402


class LoadBalancerTopo(Topo):
    """Partial-mesh topology described in config.py."""

    def build(self, bw=config.LINK_BW_MBPS, delay=config.LINK_DELAY,
              host_bw=config.HOST_LINK_BW_MBPS, host_delay=config.HOST_LINK_DELAY):
        for name in config.SWITCHES:
            dpid = int(name[1:])
            self.addSwitch(name, dpid="%016x" % dpid, protocols="OpenFlow13")

        for name, h in config.all_hosts().items():
            self.addHost(name, ip="%s/%d" % (h["ip"], config.SUBNET_PREFIX), mac=h["mac"])

        for a, b in config.SWITCH_LINKS:
            self.addLink(a, b, bw=bw, delay=delay, max_queue_size=config.LINK_MAX_QUEUE)

        for name, h in config.all_hosts().items():
            self.addLink(name, h["switch"], bw=host_bw, delay=host_delay,
                         max_queue_size=config.LINK_MAX_QUEUE)


def prepare_hosts(net):
    """Disable IPv6 noise and warm up ARP so the controller learns every host."""
    for name in config.all_hosts():
        host = net.get(name)
        for opt in ("all", "default", "lo"):
            host.cmd("sysctl -qw net.ipv6.conf.%s.disable_ipv6=1" % opt)
    # One ping per client towards the VIP makes the controller learn the client
    # locations; servers are discovered by the controller's own ARP/TCP probes.
    for name in config.CLIENTS:
        net.get(name).cmd("ping -c1 -W1 %s >/dev/null 2>&1 &" % config.VIP)


def main():
    parser = argparse.ArgumentParser(description="SDN load balancer topology")
    parser.add_argument("--no-cli", action="store_true",
                        help="run headless (until SIGINT/SIGTERM) instead of opening the Mininet CLI")
    parser.add_argument("--bw", type=float, default=config.LINK_BW_MBPS, help="switch link bandwidth (Mbps)")
    parser.add_argument("--delay", default=config.LINK_DELAY, help="switch link delay, e.g. 5ms")
    parser.add_argument("--controller-ip", default=config.CONTROLLER_IP)
    parser.add_argument("--controller-port", type=int, default=config.OPENFLOW_PORT)
    args = parser.parse_args()

    setLogLevel("info")
    topo = LoadBalancerTopo(bw=args.bw, delay=args.delay)
    net = Mininet(topo=topo, switch=OVSKernelSwitch, link=TCLink, controller=None,
                  autoSetMacs=False, autoStaticArp=False)
    net.addController("c0", controller=RemoteController,
                      ip=args.controller_ip, port=args.controller_port)
    net.start()

    info("*** Waiting for switches to connect to the controller\n")
    if hasattr(net, "waitConnected"):
        net.waitConnected(timeout=30)
    # LLDP discovery needs a few seconds before the controller forwards traffic.
    time.sleep(3)

    control = NetworkControl(net)
    info("*** Starting HTTP and iperf servers on %s\n" % ", ".join(config.SERVERS))
    for name in config.SERVERS:
        control.start_services(name)

    prepare_hosts(net)

    server = ControlServer(control, config.MININET_API_HOST, config.MININET_API_PORT)
    server.start()
    info("*** Control API listening on %s\n" % config.MININET_API)

    stop_event = threading.Event()

    def _stop(*_):
        stop_event.set()

    try:
        if args.no_cli:
            signal.signal(signal.SIGTERM, _stop)
            signal.signal(signal.SIGINT, _stop)
            info("*** Running headless. Send SIGTERM/SIGINT (or ./stop.sh) to stop.\n")
            while not stop_event.is_set():
                stop_event.wait(1)
        else:
            CLI(net)
    finally:
        info("*** Stopping control API, traffic and network\n")
        server.stop()
        control.shutdown()
        net.stop()


if __name__ == "__main__":
    main()
