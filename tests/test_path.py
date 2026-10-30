"""Path computation on the project topology (no Ryu needed)."""
import networkx as nx

import config
from controller.path_utils import (edge_key, hop_ports, link_weight, path_uses_edge,
                                   shortest_path, spanning_tree_edges)
from controller.topology_manager import TopologyManager


def build_graph():
    g = nx.Graph()
    next_port = {}
    for a, b in config.SWITCH_LINKS:
        da, db = int(a[1:]), int(b[1:])
        pa = next_port[da] = next_port.get(da, 0) + 1
        pb = next_port[db] = next_port.get(db, 0) + 1
        g.add_edge(da, db, weight=1.0, ports={da: pa, db: pb})
    return g


def test_shortest_path_is_three_hops_when_idle():
    g = build_graph()
    path = shortest_path(g, 1, 5)
    assert len(path) == 3 and path[0] == 1 and path[-1] == 5


def test_busy_link_is_avoided():
    g = build_graph()
    first = shortest_path(g, 1, 5)
    a, b = first[1], first[2]
    g[a][b]["weight"] = link_weight(95.0)
    second = shortest_path(g, 1, 5)
    assert not path_uses_edge(second, a, b)
    assert len(second) == 3  # an equally short alternative exists in the mesh


def test_prediction_penalty_steers_away():
    g = build_graph()
    for a, b in g.edges():
        g[a][b]["weight"] = link_weight(10.0)
    g[3][5]["weight"] = link_weight(10.0, predicted_pct=90.0)
    g[1][4]["weight"] = link_weight(10.0)
    path = shortest_path(g, 1, 5)
    assert not path_uses_edge(path, 3, 5)


def test_link_weight_formula():
    assert link_weight(0) == 1.0
    assert link_weight(50, util_weight=4.0) == 3.0
    assert link_weight(50, predicted_pct=85, util_weight=4.0, prediction_penalty=10) == 13.0
    assert link_weight(150, util_weight=4.0) == 5.0  # utilisation is clamped to 100 %


def test_unreachable_returns_none():
    g = build_graph()
    g.add_node(99)
    assert shortest_path(g, 1, 99) is None
    assert shortest_path(g, 1, 42) is None


def test_reroute_after_link_removal():
    g = build_graph()
    g.remove_edge(3, 5)
    g.remove_edge(4, 5)
    path = shortest_path(g, 1, 5)
    assert path[-2] == 6  # only s6 still reaches s5


def test_spanning_tree_has_n_minus_1_edges():
    g = build_graph()
    tree = spanning_tree_edges(g)
    assert len(tree) == g.number_of_nodes() - 1
    assert all(edge_key(a, b) in {edge_key(x, y) for x, y in g.edges()} for a, b in tree)


def test_hop_ports_uses_link_ports_and_host_port():
    g = build_graph()
    path = [1, 3, 5]
    hops = hop_ports(g, path, last_port=7)
    assert hops[0] == (1, g[1][3]["ports"][1])
    assert hops[1] == (3, g[3][5]["ports"][3])
    assert hops[2] == (5, 7)


def test_topology_manager_ignores_hosts_on_inter_switch_ports():
    topo = TopologyManager(expected_links=1)
    topo.add_switch(1, [1, 2, 3])
    topo.add_switch(3, [1, 2])
    topo.add_link(1, 3, 3, 1)
    assert topo.ready
    assert not topo.learn_host("00:00:00:00:00:99", "10.0.0.99", 1, 3)
    assert topo.learn_host("00:00:00:00:00:01", "10.0.0.1", 1, 1)
    assert topo.host_by_ip("10.0.0.1")["port"] == 1
    assert topo.edge_ports(1) == [1, 2]
    assert topo.link_at_port(1, 3) == 3
