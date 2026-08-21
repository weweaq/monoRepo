"""
dev-console server.py - local dev-service control console (stdlib only).

Serves a dashboard and a small JSON API to start/stop/inspect local dev
services defined in services.json. Binds to loopback ONLY (127.0.0.1) so the
console cannot be reached from the LAN. Mutating endpoints require a token.
"""

import os
import re
import json
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

RUN_DIR = None  # current run's log directory, set in main()

PROC_CACHE = None
PROC_CACHE_TS = 0.0


def get_processes():
    """Return (pid, cmdline) list, using a short-lived cache to avoid spawning PowerShell per call."""
    global PROC_CACHE, PROC_CACHE_TS
    now = time.monotonic()
    if PROC_CACHE is not None and (now - PROC_CACHE_TS) < 5.0:
        return PROC_CACHE
    PROC_CACHE = list_processes()
    PROC_CACHE_TS = now
    return PROC_CACHE


def service_log_file(service_id):
    """Absolute path for a service's stdout/stderr log, or None if no run dir."""
    if not RUN_DIR:
        return None
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(service_id))
    return os.path.join(RUN_DIR, "services", safe + ".log")


def now_cst():
    """Return current time in Asia/Shanghai (UTC+8), falling back to a fixed offset."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=8)))


class JsonlLogger:
    """Append-only JSONL logger under <log_dir>/<runstamp>/app.jsonl (AI-debuggable)."""

    def __init__(self, log_dir):
        self._lock = threading.Lock()
        self._path = None
        self.run_dir = None
        try:
            runstamp = now_cst().strftime("%Y-%m-%d-%H%M%S")
            run_dir = os.path.join(log_dir, runstamp)
            os.makedirs(run_dir, exist_ok=True)
            self._path = os.path.join(run_dir, "app.jsonl")
            self.run_dir = run_dir
        except Exception:
            self._path = None
            self.run_dir = None

    def log(self, level, message, **extra):
        if not self._path:
            return
        rec = {
            "timestamp": now_cst().isoformat(timespec="milliseconds"),
            "level": level,
            "module": "server",
            "message": message,
        }
        for k, v in extra.items():
            rec[k] = v
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass


def load_config():
    path = os.path.join(SCRIPT_DIR, "services.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_processes():
    """Return list of (pid, commandline) for all running processes via a single PowerShell call."""
    ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
          "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        logger.log("ERROR", "list_processes failed", error_type=type(e).__name__,
                   context={"error": str(e)})
        return []
    txt = (out.stdout or "").strip()
    if not txt:
        return []
    try:
        data = json.loads(txt)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    procs = []
    for p in data:
        if not isinstance(p, dict):
            continue
        cl = p.get("CommandLine") or ""
        try:
            pid = int(p.get("ProcessId"))
        except Exception:
            continue
        procs.append((pid, cl))
    return procs


def detect_pids(service, procs=None):
    """Return list of PIDs whose command line matches any of service['match'] substrings."""
    if procs is None:
        procs = get_processes()
    needles = [m.lower() for m in service.get("match", [])]
    if not needles:
        return []
    pids = []
    for pid, cl in procs:
        cll = (cl or "").lower()
        if any(n in cll for n in needles):
            pids.append(pid)
    return pids


def _free_service_ports(service):
    """Kill any process still listening on the service's declared ports (zombie from a crash).

    A cheap pure-Python connect probe decides whether a port is actually occupied; the
    heavier PowerShell call only runs when a zombie is detected, so a clean start stays fast.
    """
    import socket
    ports = [int(p) for p in service.get("ports", []) if str(p).isdigit()]
    if not ports:
        return
    occupied = []
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", p))
            occupied.append(p)
        except OSError:
            pass
        finally:
            s.close()
    if not occupied:
        return
    port_list = ",".join(str(p) for p in occupied)
    ps = ("($ports) | ForEach-Object { Get-NetTCPConnection -LocalPort $_ -State Listen "
          "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique }")
    ps = ps.replace("$ports", "(" + port_list + ")")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        logger.log("WARNING", "free ports failed", error_type=type(e).__name__,
                   context={"service": service.get("id"), "error": str(e)})
        return
    killed = 0
    for tok in (out.stdout or "").split():
        try:
            pid = int(tok)
        except ValueError:
            continue
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Stop-Process -Id %d -Force -ErrorAction SilentlyContinue" % pid],
                capture_output=True, text=True, timeout=10,
            )
            killed += 1
        except Exception:
            pass
    if killed:
        logger.log("INFO", "freed ports", context={"service": service.get("id"), "ports": occupied, "killed": killed})


def start_service(service):
    sid = service.get("id", "unknown")
    # Single-instance guard: never spawn a duplicate of an already-running service.
    existing = detect_pids(service)
    if existing:
        logger.log("INFO", "already running, skip start", context={"service": sid, "pid": existing[0]})
        return True, "已在运行 (pid %d)" % existing[0]

    cmd = [service["interpreter"]] + list(service.get("args", []))
    env = dict(os.environ)
    env.update(service.get("env", {}))
    cwd = service.get("cwd") or None
    log_path = service_log_file(sid)
    stdout_fh = stderr_fh = None
    try:
        if log_path:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            stdout_fh = open(log_path, "w", encoding="utf-8", buffering=1)
            stderr_fh = stdout_fh
    except Exception as e:
        logger.log("ERROR", "cannot open service log", error_type=type(e).__name__,
                   context={"service": sid, "error": str(e)})
    # Clear any zombie still holding the port from a previous crash, so bind won't fail.
    _free_service_ports(service)
    # DETACHED_PROCESS avoids inheriting the parent console, but console-subsystem
    # interpreters (python.exe, opencode.cmd -> cmd.exe) still allocate their OWN
    # window. CREATE_NO_WINDOW suppresses that so every service runs headless.
    creationflags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                     | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env, creationflags=creationflags,
            stdout=stdout_fh, stderr=stderr_fh,
        )
        logger.log("INFO", "start issued", context={"service": sid, "cmd": " ".join(cmd), "pid": proc.pid})
    except OSError as e:
        if stdout_fh:
            stdout_fh.close()
        logger.log("ERROR", "start failed", error_type=type(e).__name__,
                   context={"service": sid, "error": str(e)})
        return False, str(e)

    # Verify the launched child is still alive. Catches immediate exits (missing APK,
    # port already in use, import error) that DEVNULL + a blind "started" would hide.
    # Use poll() on the direct child (no full process re-scan) — fast and exact.
    time.sleep(1.2)
    if proc.poll() is None:
        return True, "started (pid %d)" % proc.pid
    reason = "进程启动后未存活"
    if log_path and os.path.isfile(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                tail = "".join(fh.readlines()[-8:])
        except Exception:
            tail = ""
        if tail.strip():
            reason += "；日志尾部: " + tail.strip()[-300:]
    logger.log("WARNING", "service exited immediately", context={"service": sid, "log": log_path})
    return False, reason


def stop_service(service):
    procs = list_processes()
    pids = detect_pids(service, procs)
    if not pids:
        return False, "no matching process found"
    killed = 0
    for pid in pids:
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Stop-Process -Id %d -Force -ErrorAction SilentlyContinue" % pid],
                capture_output=True, text=True, timeout=15,
            )
            killed += 1
        except Exception as e:
            logger.log("ERROR", "stop pid failed", error_type=type(e).__name__,
                       context={"pid": pid, "error": str(e)})
    logger.log("INFO", "stop issued", context={"service": service.get("id"), "killed": killed})
    return True, "stopped %d process(es)" % killed


class Handler(BaseHTTPRequestHandler):
    server_version = "dev-console/1.0"

    def log_message(self, fmt, *args):
        return  # runtime logging goes to JSONL, not stderr

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _token_ok(self):
        h = self.headers.get("X-DevConsole-Token")
        token = CONFIG.get("token", "")
        # A missing/empty header is never authorized; an empty configured token
        # disables auth entirely, so we refuse to treat it as valid.
        return bool(token) and h is not None and h == token

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_index()
            return
        if self.path == "/api/status":
            self._serve_status()
            return
        self._send_json(404, {"ok": False, "message": "not found"})

    def _serve_index(self):
        path = os.path.join(SCRIPT_DIR, "public", "index.html")
        if not os.path.isfile(path):
            self._send_json(404, {"ok": False, "message": "index.html missing"})
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_status(self):
        services = []
        procs = get_processes()
        for s in CONFIG.get("services", []):
            pids = detect_pids(s, procs)
            services.append({
                "id": s.get("id"),
                "name": s.get("name"),
                "running": len(pids) > 0,
                "pid": pids[0] if pids else None,
                "ports": s.get("ports", []),
                "log": service_log_file(s.get("id", "")) or "",
            })
        self._send_json(200, {"services": services})

    def do_POST(self):
        if self.path not in ("/api/start", "/api/stop"):
            self._send_json(404, {"ok": False, "message": "not found"})
            return
        if not self._token_ok():
            logger.log("WARNING", "unauthorized attempt", context={"path": self.path, "remote": self.client_address[0]})
            self._send_json(401, {"ok": False, "message": "unauthorized"})
            return
        body = self._read_body()
        sid = body.get("service")
        service = next((s for s in CONFIG.get("services", []) if s.get("id") == sid), None)
        if not service:
            self._send_json(404, {"ok": False, "message": "unknown service"})
            return
        if self.path == "/api/start":
            ok, msg = start_service(service)
        else:
            ok, msg = stop_service(service)
        self._send_json(200, {"ok": ok, "message": msg})


def main():
    global CONFIG, logger
    CONFIG = load_config()
    log_dir = os.path.join(SCRIPT_DIR, CONFIG.get("log_dir", "logs"))
    logger = JsonlLogger(log_dir)
    global RUN_DIR
    RUN_DIR = logger.run_dir

    bind_host = CONFIG.get("bind_host", "127.0.0.1")
    bind_port = int(CONFIG.get("bind_port", 8787))
    token = CONFIG.get("token", "")

    if not token:
        logger.log("ERROR", "token is empty; refusing to start (mutating endpoints would be unauthenticated)")
        print("[FATAL] services.json 'token' must be a non-empty string; refusing to start for security.")
        return

    import ipaddress
    try:
        if not ipaddress.ip_address(bind_host).is_loopback:
            logger.log("ERROR", "refusing to bind non-loopback host", context={"bind_host": bind_host})
            print("[FATAL] bind_host must be loopback (127.0.0.1); refusing to start for security.")
            return
    except ValueError:
        logger.log("ERROR", "invalid bind_host", context={"bind_host": bind_host})
        return

    if token == "dev-console-local":
        logger.log("WARNING", "token is the default insecure value; set a strong token in services.json")

    logger.log("INFO", "dev-console starting",
               context={"bind": "%s:%d" % (bind_host, bind_port), "services": len(CONFIG.get("services", []))})

    server = ThreadingHTTPServer((bind_host, bind_port), Handler)
    logger.log("INFO", "listening", context={"bind": "%s:%d" % (bind_host, bind_port)})
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.log("INFO", "shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
