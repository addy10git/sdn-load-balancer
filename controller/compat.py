"""Import shim: use Ryu when installed, otherwise os-ken (its maintained fork).

Every controller module imports Ryu symbols from here, so switching framework
needs no other code change.
"""
try:
    from ryu.base import app_manager
    from ryu.controller import ofp_event
    from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                        MAIN_DISPATCHER, set_ev_cls)
    from ryu.ofproto import ofproto_v1_3
    from ryu.lib import hub
    from ryu.lib.packet import arp, ether_types, ethernet, icmp, ipv4, packet, tcp, udp
    from ryu.topology import event as topo_event
    from ryu.topology import api as topo_api  # noqa: F401  (registers ryu.topology.switches)
    from ryu.app.wsgi import ControllerBase, WSGIApplication, route
    BaseApp = app_manager.RyuApp
    BACKEND = "ryu"
except ImportError:  # pragma: no cover - depends on the installed framework
    from os_ken.base import app_manager
    from os_ken.controller import ofp_event
    from os_ken.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                           MAIN_DISPATCHER, set_ev_cls)
    from os_ken.ofproto import ofproto_v1_3
    from os_ken.lib import hub
    from os_ken.lib.packet import arp, ether_types, ethernet, icmp, ipv4, packet, tcp, udp
    from os_ken.topology import event as topo_event
    from os_ken.topology import api as topo_api  # noqa: F401
    try:
        from os_ken.app.wsgi import ControllerBase, WSGIApplication, route
    except ImportError:  # current os-ken releases ship no WSGI app
        from controller.wsgi_fallback import ControllerBase, WSGIApplication, route
    BaseApp = app_manager.OSKenApp  # os-ken renamed RyuApp
    BACKEND = "os-ken"

__all__ = [
    "app_manager", "ofp_event", "CONFIG_DISPATCHER", "DEAD_DISPATCHER", "MAIN_DISPATCHER",
    "set_ev_cls", "ofproto_v1_3", "hub", "arp", "ether_types", "ethernet", "icmp", "ipv4",
    "packet", "tcp", "udp", "topo_event", "topo_api", "ControllerBase", "WSGIApplication",
    "route", "BaseApp", "BACKEND",
]
