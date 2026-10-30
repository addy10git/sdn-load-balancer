"""Minimal stand-in for ryu.app.wsgi, used only when running on os-ken.

os-ken dropped Ryu's WSGI application, so this module offers the same three
names (ControllerBase, WSGIApplication, route) on top of webob and the
framework's own green WSGI server. It listens on config.CONTROLLER_REST_PORT.
"""
import logging

import eventlet
import eventlet.wsgi
import webob

import config

LOG = logging.getLogger("sdn.wsgi")

try:
    from os_ken.lib import hub
except ImportError:  # pragma: no cover
    from ryu.lib import hub


def route(name, path, methods=None, requirements=None):
    """Decorator recording the URL path and HTTP methods of a handler."""
    def decorator(func):
        func._sdn_route = (path, tuple(methods or ("GET",)))
        return func
    return decorator


class ControllerBase(object):
    def __init__(self, req, link, data, **config):
        self.req = req
        self.link = link
        self.data = data


class WSGIApplication(object):
    """Created by the app manager through _CONTEXTS; serves registered controllers."""

    def __init__(self, **_kwargs):
        self._routes = {}          # (path, method) -> (controller class, attr name, data)
        sock = eventlet.listen(("0.0.0.0", config.CONTROLLER_REST_PORT))
        hub.spawn(eventlet.wsgi.server, sock, self, log_output=False)
        LOG.info("REST API (fallback WSGI) on port %d", config.CONTROLLER_REST_PORT)

    def register(self, controller, data=None):
        for attr in dir(controller):
            info = getattr(getattr(controller, attr), "_sdn_route", None)
            if info:
                path, methods = info
                for method in methods:
                    self._routes[(path, method)] = (controller, attr, data)

    def __call__(self, environ, start_response):
        req = webob.Request(environ)
        target = self._routes.get((req.path, req.method))
        if target is None:
            resp = webob.Response(status=404, content_type="application/json",
                                  body=b'{"error": "not found"}')
        else:
            controller, attr, data = target
            resp = getattr(controller(req, None, data), attr)(req)
        return resp(environ, start_response)
