"""Path computation helpers (pure Python + networkx, no Ryu imports).

The switch graph is an undirected networkx.Graph whose nodes are datapath ids.
Each edge carries:
    ports   {dpid_a: port_on_a, dpid_b: port_on_b}
    weight  routing cost (see link_weight)
"""
import networkx as nx


def link_weight(util_pct, predicted_pct=None, util_weight=4.0,
                congestion_threshold=80.0, prediction_penalty=10.0):
    """Routing cost of a link.

    cost = 1 (hop) + util_weight * utilisation fraction + penalty if the AI
    predicts the link will be congested. A hop count of 1 keeps paths short when
    the network is idle; the other terms steer flows around busy links.
    """
    util = max(0.0, min(100.0, float(util_pct or 0.0)))
    weight = 1.0 + util_weight * util / 100.0
    if predicted_pct is not None and predicted_pct > congestion_threshold:
        weight += prediction_penalty
    return weight


def shortest_path(graph, src, dst, weight="weight"):
    """Dijkstra path from src to dst (list of dpids) or None if unreachable."""
    if src == dst:
        return [src] if src in graph else None
    try:
        return nx.dijkstra_path(graph, src, dst, weight=weight)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None


def path_cost(graph, path, weight="weight"):
    return sum(graph[a][b].get(weight, 1.0) for a, b in zip(path, path[1:]))


def path_edges(path):
    """[(a, b), (b, c), ...] for a node path."""
    return list(zip(path, path[1:]))


def edge_key(a, b):
    """Canonical (min, max) key of an undirected edge."""
    return (a, b) if a <= b else (b, a)


def path_uses_edge(path, a, b):
    key = edge_key(a, b)
    return any(edge_key(x, y) == key for x, y in path_edges(path))


def spanning_tree_edges(graph):
    """Edges of a minimum spanning tree (by hop count) as a set of canonical keys.

    Broadcasts are only ever forwarded along these edges, so a looped topology
    can never produce a broadcast storm.
    """
    tree = nx.minimum_spanning_tree(graph, weight=None)
    return {edge_key(a, b) for a, b in tree.edges()}


def hop_ports(graph, path, last_port):
    """Turn a dpid path into [(dpid, out_port), ...]; the last hop exits last_port."""
    hops = []
    for i, dpid in enumerate(path):
        if i + 1 < len(path):
            hops.append((dpid, graph[dpid][path[i + 1]]["ports"][dpid]))
        else:
            hops.append((dpid, last_port))
    return hops
