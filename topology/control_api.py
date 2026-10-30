"""
Local HTTP control interface that runs inside the Mininet process.

It lets the dashboard, demo.sh, the AI data collector and the experiment runner
change the emulated network without a Mininet CLI. It binds to 127.0.0.1 only.

Endpoints (JSON in / JSON out):
  GET  /status                       links, servers, running traffic jobs
  POST /link      {"a","b","state": "up"|"down"}
  POST /server    {"name", "action": "stop"|"start"}
  POST /traffic   {"src","dst"?, "mode": "tcp"|"udp", "bw"?, "duration"?, "parallel"?}
  POST /attack    {"src", "type": "syn"|"udp"|"icmp", "duration"?, "target"?}
  POST /stop_traffic {"src"?}
  POST /measure   {"type": "iperf"|"ping", "src", "dst"?, "duration"?, "udp"?, "bw"?, "count"?}
  POST /http      {"src", "count"?}   curl the VIP and return which server answered
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import config

LOG = logging.getLogger("mininet.control")
WWW_ROOT = "/tmp/sdnlb_www"


class NetworkControl(object):
    """Actions on the running Mininet network. All methods are thread-safe."""

    def __init__(self, net):
        self.net = net
        self.lock = threading.RLock()
        self.services = {}        # server -> {"http": Popen, "iperf_tcp": Popen, "iperf_udp": Popen}
        self.server_state = {name: "stopped" for name in config.SERVERS}
        self.link_state = {self._link_key(a, b): "up" for a, b in config.SWITCH_LINKS}
        self.jobs = {}            # id -> job dict
        self._next_job = 1

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _link_key(a, b):
        return "%s-%s" % tuple(sorted((a, b), key=lambda n: (len(n), n)))

    def _host(self, name):
        if name not in config.all_hosts():
            raise ValueError("unknown host %r" % name)
        return self.net.get(name)

    def _resolve_dst(self, dst):
        if not dst or dst == "vip":
            return config.VIP
        hosts = config.all_hosts()
        return hosts[dst]["ip"] if dst in hosts else dst

    # ------------------------------------------------------------ servers
    def start_services(self, name):
        with self.lock:
            host = self._host(name)
            if name not in config.SERVERS:
                raise ValueError("%s is not a server" % name)
            self.stop_services(name, quiet=True)
            www = os.path.join(WWW_ROOT, name)
            os.makedirs(www, exist_ok=True)
            with open(os.path.join(www, "index.html"), "w") as fh:
                fh.write("%s\n" % name)
            devnull = subprocess.DEVNULL
            self.services[name] = {
                "http": host.popen(["python3", "-m", "http.server", str(config.HTTP_PORT),
                                    "--directory", www], stdout=devnull, stderr=devnull),
                "iperf_tcp": host.popen(["iperf", "-s", "-p", str(config.IPERF_PORT)],
                                        stdout=devnull, stderr=devnull),
                "iperf_udp": host.popen(["iperf", "-s", "-u", "-p", str(config.IPERF_PORT)],
                                        stdout=devnull, stderr=devnull),
            }
            self.server_state[name] = "running"
            return {"server": name, "state": "running"}

    def stop_services(self, name, quiet=False):
        with self.lock:
            if name not in config.SERVERS:
                raise ValueError("%s is not a server" % name)
            for proc in self.services.pop(name, {}).values():
                _terminate(proc)
            self.server_state[name] = "stopped"
            if not quiet:
                LOG.info("services on %s stopped", name)
            return {"server": name, "state": "stopped"}

    # ------------------------------------------------------------ links
    def set_link(self, a, b, state):
        if state not in ("up", "down"):
            raise ValueError("state must be up or down")
        key = self._link_key(a, b)
        if key not in self.link_state:
            raise ValueError("unknown switch link %s" % key)
        with self.lock:
            self.net.configLinkStatus(a, b, state)
            self.link_state[key] = state
        return {"link": key, "state": state, "ts": time.time()}

    # ------------------------------------------------------------ traffic
    def _add_job(self, kind, src, proc, duration, detail):
        with self.lock:
            job_id = self._next_job
            self._next_job += 1
            self.jobs[job_id] = {"id": job_id, "kind": kind, "src": src, "proc": proc,
                                 "started": time.time(), "duration": duration, "detail": detail}
            return job_id

    def start_traffic(self, src, dst=None, mode="tcp", bw="5M", duration=20, parallel=1):
        host = self._host(src)
        duration = int(duration)
        target = self._resolve_dst(dst)
        cmd = ["iperf", "-c", target, "-p", str(config.IPERF_PORT), "-t", str(duration)]
        if mode == "udp":
            cmd += ["-u", "-b", str(bw)]
        elif mode == "tcp":
            cmd += ["-P", str(max(1, int(parallel)))]
        else:
            raise ValueError("mode must be tcp or udp")
        proc = host.popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        job = self._add_job("traffic", src, proc, duration,
                            "%s iperf %s -> %s" % (mode, src, target))
        return {"job": job, "cmd": " ".join(cmd)}

    def start_attack(self, src, kind="syn", duration=20, target=None):
        host = self._host(src)
        duration = int(duration)
        target = self._resolve_dst(target)
        interval = "u%d" % config.ATTACK_INTERVAL_US
        if kind == "syn":
            attack = ["hping3", "-S", "-p", str(config.HTTP_PORT), "-i", interval, target]
        elif kind == "udp":
            attack = ["hping3", "--udp", "-p", str(config.IPERF_PORT), "-i", interval, target]
        elif kind == "icmp":
            attack = ["hping3", "--icmp", "-i", interval, target]
        else:
            raise ValueError("type must be syn, udp or icmp")
        cmd = ["timeout", str(duration)] + attack
        proc = host.popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        job = self._add_job("attack", src, proc, duration,
                            "%s flood %s -> %s" % (kind, src, target))
        return {"job": job, "cmd": " ".join(cmd)}

    def stop_traffic(self, src=None):
        stopped = []
        with self.lock:
            for job_id, job in list(self.jobs.items()):
                if src and job["src"] != src:
                    continue
                _terminate(job["proc"])
                stopped.append(job_id)
                del self.jobs[job_id]
        return {"stopped": stopped}

    # ------------------------------------------------------------ measurement
    def measure_iperf(self, src, dst=None, duration=10, udp=False, bw="5M", parallel=1):
        host = self._host(src)
        target = self._resolve_dst(dst)
        cmd = ["iperf", "-c", target, "-p", str(config.IPERF_PORT), "-t", str(int(duration)), "-f", "m"]
        if udp:
            cmd += ["-u", "-b", str(bw)]
        elif int(parallel) > 1:
            cmd += ["-P", str(int(parallel))]
        out = _run(host, cmd, timeout=int(duration) + 20)
        return dict(parse_iperf(out, udp), src=src, dst=target, udp=bool(udp))

    def measure_ping(self, src, dst=None, count=20, interval=0.2):
        host = self._host(src)
        target = self._resolve_dst(dst)
        cmd = ["ping", "-c", str(int(count)), "-i", str(interval), "-W", "1", target]
        out = _run(host, cmd, timeout=int(count * interval) + 15)
        return dict(parse_ping(out), src=src, dst=target)

    def http_get(self, src, count=1):
        host = self._host(src)
        answers = []
        for _ in range(int(count)):
            out = _run(host, ["curl", "-s", "-m", "2", "http://%s/" % config.VIP], timeout=5)
            answers.append(out.strip() or "no-response")
        return {"src": src, "responses": answers}

    # ------------------------------------------------------------ status
    def status(self):
        with self.lock:
            jobs = []
            for job_id, job in list(self.jobs.items()):
                if job["proc"].poll() is not None:
                    del self.jobs[job_id]
                    continue
                jobs.append({k: v for k, v in job.items() if k != "proc"})
            return {
                "links": [{"link": k, "state": v} for k, v in sorted(self.link_state.items())],
                "servers": [{"name": k, "state": v} for k, v in sorted(self.server_state.items())],
                "jobs": jobs,
                "hosts": sorted(config.all_hosts()),
                "ts": time.time(),
            }

    def shutdown(self):
        self.stop_traffic()
        for name in list(self.services):
            self.stop_services(name, quiet=True)


# ---------------------------------------------------------------- parsing
def _terminate(proc):
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
    except OSError:
        pass


def _run(host, cmd, timeout):
    proc = host.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")


def parse_iperf(text, udp=False):
    """Extract throughput (Mbps) and, for UDP, jitter/loss from iperf2 output."""
    result = {"throughput_mbps": None, "jitter_ms": None, "loss_pct": None, "raw_ok": False}
    rates = re.findall(r"([\d.]+)\s+Mbits/sec", text)
    if rates:
        # With -P n the [SUM] line comes last; otherwise the last line is the total.
        result["throughput_mbps"] = float(rates[-1])
        result["raw_ok"] = True
    if udp:
        m = re.findall(r"([\d.]+)\s+ms\s+(\d+)/\s*(\d+)\s+\(([\d.e+-]+)%\)", text)
        if m:
            jitter, _lost, _total, pct = m[-1]
            result["jitter_ms"] = float(jitter)
            result["loss_pct"] = float(pct)
    return result


def parse_ping(text):
    result = {"avg_ms": None, "mdev_ms": None, "loss_pct": None}
    loss = re.search(r"([\d.]+)% packet loss", text)
    if loss:
        result["loss_pct"] = float(loss.group(1))
    rtt = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", text)
    if rtt:
        result["avg_ms"] = float(rtt.group(2))
        result["mdev_ms"] = float(rtt.group(4))
    return result


# ---------------------------------------------------------------- HTTP server
class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(control):
    routes_post = {
        "/link": lambda b: control.set_link(b["a"], b["b"], b["state"]),
        "/server": lambda b: (control.start_services(b["name"]) if b["action"] == "start"
                              else control.stop_services(b["name"])),
        "/traffic": lambda b: control.start_traffic(
            b["src"], b.get("dst"), b.get("mode", "tcp"), b.get("bw", "5M"),
            b.get("duration", 20), b.get("parallel", 1)),
        "/attack": lambda b: control.start_attack(
            b["src"], b.get("type", "syn"), b.get("duration", 20), b.get("target")),
        "/stop_traffic": lambda b: control.stop_traffic(b.get("src")),
        "/measure": lambda b: (control.measure_ping(b["src"], b.get("dst"), b.get("count", 20))
                               if b.get("type") == "ping" else
                               control.measure_iperf(b["src"], b.get("dst"), b.get("duration", 10),
                                                     b.get("udp", False), b.get("bw", "5M"),
                                                     b.get("parallel", 1))),
        "/http": lambda b: control.http_get(b["src"], b.get("count", 1)),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the Mininet console clean
            LOG.debug(fmt, *args)

        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0] == "/status":
                self._send(200, control.status())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            handler = routes_post.get(self.path.split("?")[0])
            if handler is None:
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                self._send(200, dict(handler(body), ok=True))
            except (KeyError, ValueError) as exc:
                self._send(400, {"ok": False, "error": str(exc)})
            except Exception as exc:  # report, never kill the Mininet process
                LOG.exception("control API error")
                self._send(500, {"ok": False, "error": str(exc)})

    return Handler


class ControlServer(object):
    def __init__(self, control, host, port):
        self.httpd = _ThreadingHTTPServer((host, port), _make_handler(control))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
