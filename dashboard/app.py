#!/usr/bin/env python3
"""
Dashboard backend (Flask + Flask-SocketIO).

* polls the controller REST API and the Mininet control API every
  DASHBOARD_PUSH_INTERVAL seconds and pushes one "update" message to browsers;
* proxies control actions (strategy, unblock -> controller; links, servers,
  traffic, attacks -> Mininet control API);
* streams CSV exports from the SQLite history database.

When the controller (or Mininet) is unreachable, the update says so and the page
shows "controller disconnected" instead of failing. No value is ever invented
here: everything shown comes from the two APIs or the database.
"""
import io
import logging
import os
import sys
import time

import requests
from flask import Flask, Response, jsonify, render_template, request
from flask_socketio import SocketIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from common.db import EXPORTABLE_TABLES, Store  # noqa: E402
from common.logsetup import setup_logging  # noqa: E402

LOG = logging.getLogger("dashboard")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("DASHBOARD_SECRET", "sdn-lb-dashboard")
socketio = SocketIO(app, async_mode="threading")

CONTROLLER_ENDPOINTS = ("summary", "topology", "stats", "servers", "predictions", "security")
_session = requests.Session()
_state = {"last_event_id": 0, "events": [], "controller_ok": False}


# ===================================================================== polling
def _get(base, path):
    resp = _session.get(base + path, timeout=config.API_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def collect_update():
    update = {"ts": time.time(), "controller": {"connected": False, "error": None},
              "mininet": {"connected": False, "error": None}}
    try:
        for name in CONTROLLER_ENDPOINTS:
            update[name] = _get(config.CONTROLLER_API, "/api/" + name)
        events = _get(config.CONTROLLER_API, "/api/events?since=%d&limit=200"
                      % _state["last_event_id"])["events"]
        if events:
            _state["last_event_id"] = events[-1]["id"]
            _state["events"] = (_state["events"] + events)[-300:]
        update["events"] = _state["events"][-150:]
        update["controller"]["connected"] = True
        if not _state["controller_ok"]:
            LOG.info("controller connected")
        _state["controller_ok"] = True
    except (requests.RequestException, ValueError) as exc:
        update["controller"]["error"] = str(exc.__class__.__name__)
        if _state["controller_ok"]:
            LOG.warning("controller disconnected: %s", exc)
        _state["controller_ok"] = False
        # Controller restarted? its event ids may start lower again.
        _state["last_event_id"] = 0
        _state["events"] = []
    try:
        update["mininet_status"] = _get(config.MININET_API, "/status")
        update["mininet"]["connected"] = True
    except (requests.RequestException, ValueError) as exc:
        update["mininet"]["error"] = str(exc.__class__.__name__)
    return update


def poller():
    while True:
        try:
            _state["last_update"] = collect_update()
            socketio.emit("update", _state["last_update"])
        except Exception:
            LOG.exception("dashboard update failed")
        socketio.sleep(config.DASHBOARD_PUSH_INTERVAL)


# ===================================================================== routes
@app.route("/")
def index():
    return render_template("index.html", strategies=config.LB_STRATEGIES,
                           clients=sorted(config.CLIENTS), servers=sorted(config.SERVERS),
                           hosts=sorted(config.all_hosts()),
                           switch_links=["%s-%s" % l for l in config.SWITCH_LINKS],
                           tables=EXPORTABLE_TABLES, push_interval=config.DASHBOARD_PUSH_INTERVAL)


def _forward(base, path, payload, timeout=10):
    try:
        resp = _session.post(base + path, json=payload, timeout=timeout)
        try:
            body = resp.json()
        except ValueError:
            body = {"error": resp.text[:200]}
        return jsonify(body), resp.status_code
    except requests.RequestException as exc:
        target = "controller" if base == config.CONTROLLER_API else "Mininet control API"
        return jsonify({"ok": False, "error": "%s unreachable: %s" % (target, exc.__class__.__name__)}), 503


CONTROLLER_ACTIONS = {"strategy": "/api/strategy", "unblock": "/api/unblock"}
MININET_ACTIONS = {"link": "/link", "server": "/server", "traffic": "/traffic",
                   "attack": "/attack", "stop_traffic": "/stop_traffic"}


@app.route("/control/<action>", methods=["POST"])
def control(action):
    payload = request.get_json(silent=True) or {}
    if action in CONTROLLER_ACTIONS:
        return _forward(config.CONTROLLER_API, CONTROLLER_ACTIONS[action], payload)
    if action in MININET_ACTIONS:
        return _forward(config.MININET_API, MININET_ACTIONS[action], payload)
    return jsonify({"ok": False, "error": "unknown action"}), 404


@app.route("/export/<table>.csv")
def export_csv(table):
    if table not in EXPORTABLE_TABLES:
        return jsonify({"error": "unknown table"}), 404
    if not os.path.exists(config.DB_PATH):
        return jsonify({"error": "no database yet (start the controller first)"}), 404
    store = Store(config.DB_PATH, readonly=True)
    try:
        buf = io.StringIO()
        store.export_csv(table, buf)
    finally:
        store.close()
    name = "%s_%s.csv" % (table, time.strftime("%Y%m%d_%H%M%S"))
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=%s" % name})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "controller_connected": _state["controller_ok"]})


@socketio.on("connect")
def on_connect():
    # A new browser gets the latest snapshot immediately instead of waiting a cycle.
    if _state.get("last_update"):
        socketio.emit("update", _state["last_update"], to=request.sid)


def main():
    setup_logging("dashboard")
    socketio.start_background_task(poller)
    LOG.info("dashboard on http://%s:%d (controller %s, mininet %s)", config.DASHBOARD_HOST,
             config.DASHBOARD_PORT, config.CONTROLLER_API, config.MININET_API)
    socketio.run(app, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT,
                 allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
