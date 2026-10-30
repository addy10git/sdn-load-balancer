"""Controller REST API (Ryu WSGI), consumed by the dashboard and scripts.

GET  /api/health        liveness
GET  /api/topology      switches, hosts, links
GET  /api/stats         per-link utilisation, throughput, drops (+ rolling history)
GET  /api/servers       pool status, health, load, active flows, mapped clients
GET  /api/predictions   predicted vs actual utilisation per link
GET  /api/events        recent events (?since=<id>&limit=<n>)
GET  /api/security      blocked sources and per-source detector metrics
GET  /api/summary       headline numbers for the dashboard summary bar
POST /api/strategy      {"strategy": "round_robin" | "least_load" | "ai_predictive"}
POST /api/unblock       {"ip": "..."}
"""
import json
import logging

from webob import Response

from controller.compat import ControllerBase, route

LOG = logging.getLogger("sdn.rest")
APP_KEY = "sdn_app"


def _json(data, status=200):
    return Response(status=status, content_type="application/json", charset="utf-8",
                    body=json.dumps(data, default=str).encode("utf-8"))


def _body(req):
    try:
        return json.loads(req.body.decode("utf-8") or "{}")
    except ValueError:
        return None


class RestController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(RestController, self).__init__(req, link, data, **config)
        self.app = data[APP_KEY]

    def _safe(self, builder):
        try:
            return _json(builder())
        except Exception as exc:
            LOG.exception("API error")
            return _json({"error": str(exc)}, 500)

    @route("sdn", "/api/health", methods=["GET"])
    def health(self, req, **_):
        return self._safe(self.app.api_health)

    @route("sdn", "/api/topology", methods=["GET"])
    def topology(self, req, **_):
        return self._safe(self.app.api_topology)

    @route("sdn", "/api/stats", methods=["GET"])
    def stats(self, req, **_):
        return self._safe(self.app.api_stats)

    @route("sdn", "/api/servers", methods=["GET"])
    def servers(self, req, **_):
        return self._safe(self.app.api_servers)

    @route("sdn", "/api/predictions", methods=["GET"])
    def predictions(self, req, **_):
        return self._safe(self.app.api_predictions)

    @route("sdn", "/api/events", methods=["GET"])
    def events(self, req, **_):
        try:
            since = int(req.GET.get("since", 0))
            limit = int(req.GET.get("limit", 100))
        except ValueError:
            return _json({"error": "since/limit must be integers"}, 400)
        return self._safe(lambda: {"events": self.app.event_log.recent(since, limit)})

    @route("sdn", "/api/security", methods=["GET"])
    def security(self, req, **_):
        return self._safe(self.app.security.api_view)

    @route("sdn", "/api/summary", methods=["GET"])
    def summary(self, req, **_):
        return self._safe(self.app.api_summary)

    @route("sdn", "/api/strategy", methods=["POST"])
    def strategy(self, req, **_):
        body = _body(req)
        if not body or "strategy" not in body:
            return _json({"error": 'expected {"strategy": "..."}'}, 400)
        try:
            self.app.lb.set_strategy(body["strategy"])
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)
        return _json({"ok": True, "strategy": self.app.lb.strategy_name})

    @route("sdn", "/api/unblock", methods=["POST"])
    def unblock(self, req, **_):
        body = _body(req)
        if not body or "ip" not in body:
            return _json({"error": 'expected {"ip": "..."}'}, 400)
        if not self.app.security.unblock(body["ip"]):
            return _json({"ok": False, "error": "%s is not blocked" % body["ip"]}, 404)
        return _json({"ok": True, "ip": body["ip"]})
