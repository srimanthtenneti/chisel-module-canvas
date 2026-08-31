#!/usr/bin/env python3
"""module_canvas server — a small background utility to run alongside a
Chisel project, whether that's a plain sbt build or a Chisel/Almond Jupyter
notebook session.

It does two things:

  1. Accepts POSTed IO descriptions from Chisel modules as they're
     elaborated (see modulecanvas.ModuleRegistry.registerModule) and keeps
     a version history per module name, so the browser palette always has
     a "latest" pick per module plus the ability to go back.
  2. Serves a browser-based canvas (static/) where those modules can be
     dragged out as blocks, wired together, or where you can hand-draw a
     custom rectangle with your own ports.

Zero third-party dependencies on purpose: `python3 server.py` should just
work next to whatever Python/Scala environment your project already has,
without adding anything to it.

Usage:
    python3 server.py [--port 8765] [--host 0.0.0.0]
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
MODULES_FILE = DATA_DIR / "modules.json"
CANVAS_FILE = DATA_DIR / "canvas.json"

VALID_DIRS = {"input", "output", "inout"}
EMPTY_HIERARCHY = {"children": [], "wires": []}

_lock = threading.Lock()


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _save_json(path: Path, obj) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    tmp.replace(path)


# registry shape: { moduleName: { "versions": [ {version, timestamp, ports}, ... ] } }
_registry: dict = _load_json(MODULES_FILE, {})
# canvas shape: { "blocks": [...], "wires": [...] }
_canvas: dict = _load_json(CANVAS_FILE, {"blocks": [], "wires": []})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _latest_modules() -> list:
    out = []
    for name, entry in _registry.items():
        versions = entry.get("versions", [])
        if not versions:
            continue
        latest = versions[-1]
        out.append(
            {
                "name": name,
                "version": latest["version"],
                "timestamp": latest["timestamp"],
                "ports": latest["ports"],
                "hierarchy": latest.get("hierarchy", EMPTY_HIERARCHY),
                "versionCount": len(versions),
            }
        )
    out.sort(key=lambda m: m["name"].lower())
    return out


def _validate_ports(ports) -> list:
    if not isinstance(ports, list):
        raise ValueError("ports must be a list")
    cleaned = []
    for p in ports:
        name = str(p.get("name", "")).strip()
        direction = str(p.get("dir", "")).strip().lower()
        width = p.get("width", 1)
        if not name:
            raise ValueError("port missing a name")
        if direction not in VALID_DIRS:
            raise ValueError(f"port '{name}' has invalid dir '{direction}'")
        try:
            width = int(width)
        except (TypeError, ValueError):
            raise ValueError(f"port '{name}' has non-integer width")
        if width < 1:
            width = 1
        cleaned.append({"name": name, "dir": direction, "width": width})
    return cleaned


def _sanitize_port_list(ports) -> list:
    """Lenient version of _validate_ports for nested hierarchy data: drop
    anything malformed instead of rejecting the whole registration over it —
    the top-level `ports` field is the one that must be strict."""
    if not isinstance(ports, list):
        return []
    out = []
    for p in ports:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name", "")).strip()
        direction = str(p.get("dir", "")).strip().lower()
        if not name or direction not in VALID_DIRS:
            continue
        try:
            width = max(1, int(p.get("width", 1)))
        except (TypeError, ValueError):
            width = 1
        out.append({"name": name, "dir": direction, "width": width})
    return out


def _sanitize_ref(ref) -> dict | None:
    if not isinstance(ref, dict):
        return None
    r = str(ref.get("ref", "")).strip()
    port = str(ref.get("port", "")).strip()
    if not r or not port:
        return None
    return {"ref": r, "port": port}


def _sanitize_wires(wires) -> list:
    if not isinstance(wires, list):
        return []
    out = []
    for w in wires:
        if not isinstance(w, dict):
            continue
        f, t = _sanitize_ref(w.get("from")), _sanitize_ref(w.get("to"))
        if f and t:
            out.append({"from": f, "to": t})
    return out


def _sanitize_hierarchy(h, depth=6) -> dict:
    if depth <= 0 or not isinstance(h, dict):
        return EMPTY_HIERARCHY
    children = h.get("children", [])
    out_children = []
    if isinstance(children, list):
        for c in children:
            if not isinstance(c, dict):
                continue
            inst = str(c.get("instName", "")).strip()
            mtype = str(c.get("moduleType", "")).strip()
            if not inst or not mtype:
                continue
            sub = _sanitize_hierarchy(c, depth - 1)
            out_children.append(
                {
                    "instName": inst,
                    "moduleType": mtype,
                    "ports": _sanitize_port_list(c.get("ports", [])),
                    "children": sub["children"],
                    "wires": sub["wires"],
                }
            )
    return {"children": out_children, "wires": _sanitize_wires(h.get("wires", []))}


def register_module(name: str, ports: list, hierarchy=None) -> dict:
    name = str(name).strip()
    if not name:
        raise ValueError("module name is required")
    cleaned_ports = _validate_ports(ports)
    cleaned_hierarchy = _sanitize_hierarchy(hierarchy) if hierarchy is not None else EMPTY_HIERARCHY
    with _lock:
        entry = _registry.setdefault(name, {"versions": []})
        version = len(entry["versions"]) + 1
        record = {
            "version": version,
            "timestamp": _now(),
            "ports": cleaned_ports,
            "hierarchy": cleaned_hierarchy,
        }
        entry["versions"].append(record)
        _save_json(MODULES_FILE, _registry)
    return {"name": name, **record}


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ModuleCanvas/1.0"

    def log_message(self, fmt, *args):  # quieter, timestamped one-liners
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")

    # -- helpers ----------------------------------------------------------

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message, status=400):
        self._send_json({"error": message}, status=status)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send_file(self, path: Path):
        if not path.is_file():
            self._send_error_json("not found", 404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _static_path_for(self, url_path: str) -> Path | None:
        rel = url_path.lstrip("/") or "index.html"
        candidate = (STATIC_DIR / rel).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return None
        return candidate

    # -- routing ------------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlsplit(self.path).path

        if path == "/api/health":
            self._send_json({"status": "ok", "time": _now()})
            return

        if path == "/api/modules":
            self._send_json(_latest_modules())
            return

        if path == "/api/modules/all":
            self._send_json(_registry)
            return

        if path == "/api/canvas":
            self._send_json(_canvas)
            return

        # static assets (index.html, app.js, style.css, ...)
        target = self._static_path_for(path)
        if target is None:
            self._send_error_json("forbidden", 403)
            return
        self._send_file(target)

    def do_POST(self):
        path = urlsplit(self.path).path

        if path == "/api/register":
            try:
                body = self._read_json_body()
                result = register_module(body.get("name"), body.get("ports", []), body.get("hierarchy"))
                print(
                    f"  registered '{result['name']}' v{result['version']} "
                    f"({len(result['ports'])} ports)"
                )
                self._send_json(result)
            except (ValueError, json.JSONDecodeError) as e:
                self._send_error_json(str(e), 400)
            return

        if path == "/api/canvas":
            try:
                body = self._read_json_body()
                if "blocks" not in body or "wires" not in body:
                    raise ValueError("canvas payload needs 'blocks' and 'wires'")
                with _lock:
                    global _canvas
                    _canvas = body
                    _save_json(CANVAS_FILE, _canvas)
                self._send_json({"status": "saved", "time": _now()})
            except (ValueError, json.JSONDecodeError) as e:
                self._send_error_json(str(e), 400)
            return

        self._send_error_json("not found", 404)

    def do_DELETE(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/modules/"):
            name = path[len("/api/modules/") :]
            with _lock:
                existed = _registry.pop(name, None) is not None
                if existed:
                    _save_json(MODULES_FILE, _registry)
            if existed:
                self._send_json({"status": "deleted", "name": name})
            else:
                self._send_error_json("unknown module", 404)
            return
        self._send_error_json("not found", 404)


def main():
    parser = argparse.ArgumentParser(description="module_canvas background server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"module_canvas listening on http://{args.host}:{args.port}")
    print(f"  open the canvas at http://{'localhost' if args.host == '0.0.0.0' else args.host}:{args.port}/")
    print(f"  {len(_registry)} module(s) loaded from {MODULES_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
