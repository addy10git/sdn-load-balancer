"""SQLite persistence for stats, predictions, server samples and events.

The controller writes; the dashboard opens the same file read-only for history
and CSV export. WAL mode lets both run at the same time.
"""
import csv
import json
import logging
import os
import sqlite3
import time

LOG = logging.getLogger("sdn.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS link_stats (
    ts REAL, link TEXT, util REAL, mbps REAL, drops INTEGER, flows INTEGER
);
CREATE INDEX IF NOT EXISTS idx_link_stats_ts ON link_stats(ts);
CREATE TABLE IF NOT EXISTS predictions (
    ts REAL, link TEXT, predicted REAL, target_ts REAL, actual REAL
);
CREATE INDEX IF NOT EXISTS idx_predictions_ts ON predictions(ts);
CREATE TABLE IF NOT EXISTS server_stats (
    ts REAL, server TEXT, status TEXT, in_pool INTEGER, load REAL, flows INTEGER
);
CREATE INDEX IF NOT EXISTS idx_server_stats_ts ON server_stats(ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, type TEXT, severity TEXT, message TEXT, data TEXT
);
"""

EXPORTABLE_TABLES = ("link_stats", "predictions", "server_stats", "events")


class Store(object):
    """Thin wrapper around a single SQLite connection."""

    def __init__(self, path, readonly=False):
        self.path = path
        if readonly:
            uri = "file:%s?mode=ro" % path
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.conn = sqlite3.connect(path, check_same_thread=False, timeout=5)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ---------------------------------------------------------------- writes
    def add_event(self, ts, etype, severity, message, data=None):
        cur = self.conn.execute(
            "INSERT INTO events (ts, type, severity, message, data) VALUES (?,?,?,?,?)",
            (ts, etype, severity, message, json.dumps(data or {})))
        self.conn.commit()
        return cur.lastrowid

    def add_link_stats(self, rows):
        """rows: iterable of (ts, link, util, mbps, drops, flows)."""
        self.conn.executemany("INSERT INTO link_stats VALUES (?,?,?,?,?,?)", rows)

    def add_predictions(self, rows):
        """rows: iterable of (ts, link, predicted, target_ts, actual_or_None)."""
        self.conn.executemany("INSERT INTO predictions VALUES (?,?,?,?,?)", rows)

    def fill_prediction_actual(self, link, target_ts, actual):
        self.conn.execute(
            "UPDATE predictions SET actual=? WHERE link=? AND target_ts=? AND actual IS NULL",
            (actual, link, target_ts))

    def add_server_stats(self, rows):
        """rows: iterable of (ts, server, status, in_pool, load, flows)."""
        self.conn.executemany("INSERT INTO server_stats VALUES (?,?,?,?,?,?)", rows)

    def commit(self):
        try:
            self.conn.commit()
        except sqlite3.Error as exc:
            LOG.warning("SQLite commit failed: %s", exc)

    def prune(self, days):
        cutoff = time.time() - days * 86400
        for table in ("link_stats", "predictions", "server_stats", "events"):
            self.conn.execute("DELETE FROM %s WHERE ts < ?" % table, (cutoff,))
        self.conn.commit()

    # ----------------------------------------------------------------- reads
    def recent_events(self, limit=200, since_id=0):
        cur = self.conn.execute(
            "SELECT id, ts, type, severity, message, data FROM events WHERE id > ? "
            "ORDER BY id DESC LIMIT ?", (since_id, limit))
        rows = cur.fetchall()
        rows.reverse()
        return [{"id": r[0], "ts": r[1], "type": r[2], "severity": r[3],
                 "message": r[4], "data": json.loads(r[5] or "{}")} for r in rows]

    def link_history(self, link, since_ts):
        cur = self.conn.execute(
            "SELECT ts, util, mbps FROM link_stats WHERE link=? AND ts>=? ORDER BY ts",
            (link, since_ts))
        return cur.fetchall()

    def export_csv(self, table, fileobj):
        if table not in EXPORTABLE_TABLES:
            raise ValueError("unknown table %s" % table)
        cur = self.conn.execute("SELECT * FROM %s ORDER BY ts" % table)
        writer = csv.writer(fileobj)
        writer.writerow([d[0] for d in cur.description])
        for row in cur:
            writer.writerow(row)

    def close(self):
        self.conn.close()
