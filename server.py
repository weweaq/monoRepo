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
from urllib.parse import urlparse, parse_qs


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

OVERRIDE_PATH = os.path.join(SCRIPT_DIR, "services.local.json")
EDITABLE_FIELDS = ("interpreter", "args", "cwd", "env", "ports")

RUN_DIR = None  # current run's log directory, set in main()

PROC_CACHE = None
PROC_CACHE_TS = 0.0

# The server runs under pythonw.exe (no console). Any console-subsystem child
# (powershell.exe, python.exe, cmd.exe) spawned without CREATE_NO_WINDOW would
# allocate its OWN console window -> black window flash on every status poll /
# start / stop. Apply this flag to every child we spawn so all helpers run
# headless. On non-Windows the flag does not exist and is a no-op.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def invalidate_process_cache():
    """Drop the process-list cache so the next status poll re-scans (after start/stop)."""
    global PROC_CACHE, PROC_CACHE_TS
    PROC_CACHE = None
    PROC_CACHE_TS = 0.0


def get_processes():
    """Return (pid, cmdline) list, using a short-lived cache to avoid spawning PowerShell per call."""
    global PROC_CACHE, PROC_CACHE_TS
    now = time.monotonic()
    if PROC_CACHE is not None and (now - PROC_CACHE_TS) < 10.0:
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


def tail_file(path, n=500):
    """Return the last up-to-n lines of a UTF-8 text file (binary tail read, no full load)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            pos = size
            data = b""
            while pos > 0 and data.count(b"\n") <= n:
                take = min(block, pos)
                pos -= take
                f.seek(pos)
                data = f.read(take) + data
        text = data.decode("utf-8", errors="replace")
        return text.splitlines()[-n:]
    except OSError:
        return None


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
    """Load services.json and overlay user edits from services.local.json.

    The local file is gitignored and holds only the fields the user edited
    (interpreter/args/cwd/env/ports), so services.json stays the template and
    '恢复默认' simply drops the override.
    """
    with open(os.path.join(SCRIPT_DIR, "services.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    overrides = {}
    if os.path.isfile(OVERRIDE_PATH):
        try:
            with open(OVERRIDE_PATH, "r", encoding="utf-8") as f:
                overrides = json.load(f) or {}
        except Exception:
            overrides = {}
    for s in cfg.get("services", []):
        ov = overrides.get(s.get("id"))
        if isinstance(ov, dict):
            for k in EDITABLE_FIELDS:
                if k in ov:
                    s[k] = ov[k]
    return cfg


def save_service_override(sid, fields, reset=False):
    """Persist a service's edited fields to services.local.json (or remove them)."""
    overrides = {}
    if os.path.isfile(OVERRIDE_PATH):
        try:
            with open(OVERRIDE_PATH, "r", encoding="utf-8") as f:
                overrides = json.load(f) or {}
        except Exception:
            overrides = {}
    if reset or not fields:
        overrides.pop(sid, None)
    else:
        patch = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS}
        if patch:
            overrides[sid] = patch
        else:
            overrides.pop(sid, None)
    with open(OVERRIDE_PATH, "w", encoding="utf-8") as f:
        json.dump(overrides, f, ensure_ascii=False, indent=2)


def sync_port_args(args, old_port, new_port):
    """If args contain '--port <old_port>', rewrite the value to new_port."""
    if str(old_port) == str(new_port):
        return args
    out = list(args)
    for i, a in enumerate(out):
        if a == "--port" and i + 1 < len(out) and str(out[i + 1]) == str(old_port):
            out[i + 1] = str(new_port)
    return out


def list_processes():
    """Return list of (pid, commandline) for all running processes via a single PowerShell call."""
    ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
          "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
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
            creationflags=CREATE_NO_WINDOW,
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
                creationflags=CREATE_NO_WINDOW,
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
        invalidate_process_cache()  # drop stale status cache so the next poll sees the new process
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
                creationflags=CREATE_NO_WINDOW,
            )
            killed += 1
        except Exception as e:
            logger.log("ERROR", "stop pid failed", error_type=type(e).__name__,
                       context={"pid": pid, "error": str(e)})
    logger.log("INFO", "stop issued", context={"service": service.get("id"), "killed": killed})
    invalidate_process_cache()  # status poll right after stop must re-scan, not reuse the pre-kill list
    return True, "stopped %d process(es)" % killed


class SingleInstanceHTTPServer(ThreadingHTTPServer):
    # http.server.HTTPServer sets allow_reuse_address = 1; on Windows that lets a
    # SECOND instance bind the same port and silently shadow the first (both serve,
    # requests race between them, logs split). Force it off so the OSError guard in
    # _main() actually fires and a duplicate launch exits cleanly.
    allow_reuse_address = False


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
        if self.path.startswith("/api/log"):
            self._serve_log()
            return
        if self.path == "/api/config":
            self._serve_config()
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

    def _serve_log(self):
        q = parse_qs(urlparse(self.path).query)
        sid = (q.get("service") or [""])[0]
        try:
            lines = max(1, min(2000, int((q.get("lines") or ["500"])[0])))
        except ValueError:
            lines = 500
        service = next((s for s in CONFIG.get("services", []) if s.get("id") == sid), None)
        if not service:
            self._send_json(404, {"ok": False, "message": "unknown service"})
            return
        path = service_log_file(sid)
        running = len(detect_pids(service)) > 0
        if not path or not os.path.isfile(path):
            self._send_json(200, {
                "ok": True, "service": sid, "running": running, "path": path or "",
                "content": "", "hint": "暂无日志文件（服务未启动或从未输出）",
            })
            return
        tail = tail_file(path, lines)
        if tail is None:
            self._send_json(200, {
                "ok": True, "service": sid, "running": running, "path": path,
                "content": "", "hint": "日志文件不可读",
            })
            return
        self._send_json(200, {
            "ok": True, "service": sid, "running": running, "path": path,
            "content": "\n".join(tail) if tail else "", "hint": "",
        })

    def _serve_config(self):
        """Full merged service definitions (template + local overrides) for the editor."""
        services = []
        for s in CONFIG.get("services", []):
            services.append({
                "id": s.get("id"),
                "name": s.get("name"),
                "notes": s.get("notes", ""),
                "interpreter": s.get("interpreter", ""),
                "args": s.get("args", []),
                "cwd": s.get("cwd", ""),
                "env": s.get("env", {}),
                "ports": s.get("ports", []),
            })
        self._send_json(200, {"services": services})

    def _save_config(self):
        global CONFIG
        body = self._read_body()
        sid = body.get("service")
        fields = body.get("fields") or {}
        reset = bool(body.get("reset"))
        service = next((s for s in CONFIG.get("services", []) if s.get("id") == sid), None)
        if not service:
            self._send_json(404, {"ok": False, "message": "unknown service"})
            return
        # Editing the port should also rewrite '--port <old>' inside args.
        if isinstance(fields.get("ports"), list) and fields["ports"]:
            old = (service.get("ports") or [None])[0]
            new = fields["ports"][0]
            if old is not None and str(old) != str(new):
                args = list(fields.get("args", service.get("args", [])))
                fields["args"] = sync_port_args(args, old, new)
        save_service_override(sid, fields, reset=reset)
        CONFIG = load_config()
        logger.log("INFO", "config saved", context={"service": sid, "reset": reset})
        self._send_json(200, {"ok": True, "message": "已保存（重启服务后生效）"})

    def do_POST(self):
        if self.path not in ("/api/start", "/api/stop", "/api/config"):
            self._send_json(404, {"ok": False, "message": "not found"})
            return
        if not self._token_ok():
            logger.log("WARNING", "unauthorized attempt", context={"path": self.path, "remote": self.client_address[0]})
            self._send_json(401, {"ok": False, "message": "unauthorized"})
            return
        if self.path == "/api/config":
            self._save_config()
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


def startup_error_log_path():
    """Errors that happen under pythonw.exe are invisible (no console); write them to a file."""
    return os.path.join(SCRIPT_DIR, CONFIG.get("log_dir", "logs"), "startup-error.log")


def main():
    global CONFIG, logger
    try:
        _main()
    except Exception:
        # pythonw has no console, so an unhandled traceback would vanish.
        # Persist it so double-clicking start.bat can never fail silently.
        import traceback
        tb = traceback.format_exc()
        try:
            with open(startup_error_log_path(), "a", encoding="utf-8") as f:
                f.write("\n[%s] dev-console crashed during startup:\n%s\n"
                        % (now_cst().isoformat(timespec="seconds"), tb))
        except Exception:
            pass
        try:
            logger.log("ERROR", "startup crashed", error_type="startup")
        except Exception:
            pass
        raise


def _main():
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

    try:
        server = SingleInstanceHTTPServer((bind_host, bind_port), Handler)
    except OSError as e:
        # Single-instance guard: a second launch (double-click start.bat again)
        # must exit quietly instead of lingering as an invisible zombie.
        msg = "bind %s:%d failed: %s (another dev-console instance already running?)" % (
            bind_host, bind_port, e)
        logger.log("ERROR", "bind failed", context={"bind": "%s:%d" % (bind_host, bind_port), "error": str(e)})
        try:
            with open(startup_error_log_path(), "a", encoding="utf-8") as f:
                f.write("\n[%s] %s\n" % (now_cst().isoformat(timespec="seconds"), msg))
        except Exception:
            pass
        return
    logger.log("INFO", "listening", context={"bind": "%s:%d" % (bind_host, bind_port)})
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.log("INFO", "shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
