"""Event log: in-memory ring buffer backed by the SQLite events table."""
# Stored on the app as `event_log`: RyuApp/OSKenApp use `self.events` for their own event queue.
import collections
import logging
import time

LOG = logging.getLogger("sdn.events")
_LEVELS = {"info": logging.INFO, "success": logging.INFO, "warning": logging.WARNING,
           "error": logging.ERROR, "critical": logging.CRITICAL}


class EventLog(object):
    def __init__(self, store, maxlen=500):
        self.store = store
        self.events = collections.deque(maxlen=maxlen)
        self._local_id = 0
        try:
            for ev in store.recent_events(limit=200):
                self.events.append(ev)
                self._local_id = max(self._local_id, ev["id"])
        except Exception as exc:  # a broken DB must not stop the controller
            LOG.warning("could not load event history: %s", exc)

    def add(self, etype, severity, message, **data):
        ts = time.time()
        try:
            event_id = self.store.add_event(ts, etype, severity, message, data)
        except Exception as exc:
            LOG.warning("could not persist event: %s", exc)
            event_id = self._local_id + 1
        self._local_id = max(self._local_id, event_id)
        event = {"id": event_id, "ts": ts, "type": etype, "severity": severity,
                 "message": message, "data": data}
        self.events.append(event)
        LOG.log(_LEVELS.get(severity, logging.INFO), "[%s] %s", etype, message)
        return event

    def recent(self, since_id=0, limit=100):
        items = [e for e in self.events if e["id"] > since_id]
        return items[-limit:]
