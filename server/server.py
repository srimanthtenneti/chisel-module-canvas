#!/usr/bin/env python3
"""module_canvas server — a small background utility to run alongside a
Chisel project, whether that's a plain sbt build or a Chisel/Almond
Jupyter notebook session.

It does two things:

  1. Accepts POSTed IO descriptions from Chisel modules as they're
     elaborated (see modulecanvas.ModuleRegistry.registerModule) and
     keeps a version history per module name, so the browser palette
     always has a "latest" pick per module plus the ability to go back.
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
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vcd_parser  # noqa: E402

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
MODULES_FILE = DATA_DIR / "modules.json"
CANVAS_FILE = DATA_DIR / "canvas.json"
TESTS_FILE = DATA_DIR / "tests.json"

VALID_DIRS = {"input", "output", "inout"}
EMPTY_HIERARCHY = {"children": [], "wires": []}
TEST_TIMEOUT_SECONDS = 180
TEST_SIGNAL_SCOPE_SUFFIX = ".svsimTestbench"
TEST_HARNESS_INTERNAL_SIGNALS = {"simulationState", "traceSupported"}

_lock = threading.Lock()
_tests_lock = threading.Lock()
_runs_lock = threading.Lock()
# sbt runs one server-socket per project directory; two concurrent `sbt`
# invocations against the same project race on it and fail outright ("Address
# already in use"). Serialize actual test execution through this lock so
# clicking "Run test" on several blocks in a row queues them instead of
# corrupting each other.
_sbt_lock = threading.Lock()


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
# canvas shape: { "blocks": [...], "wires": [...] }  (each block may carry a free-text "note")
_canvas: dict = _load_json(CANVAS_FILE, {"blocks": [], "wires": []})
# test registry shape:
#   { "sbtDir": "...", "pkg": "...",
#     "tests": { moduleName: [ {"id": "Test_Foo", "label": "..."}, ... ] } }
_test_registry: dict = _load_json(TESTS_FILE, {"sbtDir": None, "pkg": None, "tests": {}})
# in-memory only — a run's log/waveform is only useful while the server is up
_test_runs: dict = {}  # runId -> {"state": "running"|"done", "result": {...}|None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _latest_modules() -> list:
    out = []
    for name, entry in _registry.items():
        versions = entry.get("versions", [])
        if not versions:
            continue
        latest = versions[-1]
        tests = _test_registry.get("tests", {}).get(name, [])
        out.append(
            {
                "name": name,
                "version": latest["version"],
                "timestamp": latest["timestamp"],
                "ports": latest["ports"],
                "hierarchy": latest.get("hierarchy", EMPTY_HIERARCHY),
                "versionCount": len(versions),
                "hasTest": len(tests) > 0,
                "tests": tests,
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
# Test execution — driven by tools/module_canvas/register_notebook.py's
# generated notebookgen.Test_<Module> objects. Each run shells out to sbt,
# which can take anywhere from a few seconds to ~30s (Chisel elaboration +
# verilator compile + simulate), so this always runs in a background thread;
# the client polls for the result rather than holding one HTTP request open.
# --------------------------------------------------------------------------

def register_tests(sbt_dir: str, pkg: str, tests: dict) -> dict:
    if not sbt_dir or not pkg:
        raise ValueError("sbtDir and pkg are required")
    if not isinstance(tests, dict):
        raise ValueError("tests must be an object of {moduleName: [{id, label}, ...]}")
    cleaned = {}
    count = 0
    for name, entries in tests.items():
        name = str(name).strip()
        if not name or not isinstance(entries, list):
            continue
        clean_entries = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            test_id = str(e.get("id", "")).strip()
            label = str(e.get("label", "")).strip() or test_id
            if test_id:
                clean_entries.append({"id": test_id, "label": label})
        if clean_entries:
            cleaned[name] = clean_entries
            count += len(clean_entries)
    with _tests_lock:
        global _test_registry
        _test_registry = {"sbtDir": sbt_dir, "pkg": pkg, "tests": cleaned}
        _save_json(TESTS_FILE, _test_registry)
    return {"status": "ok", "count": count}


def _filter_waveform_signals(parsed: dict) -> dict:
    """Keep only the DUT's own boundary ports (the testbench-scope signals),
    dropping internal implementation state (Mem contents, loop counters,
    simulator-harness bookkeeping) that would otherwise clutter the view."""
    keep = [
        s
        for s in parsed["signals"]
        if s["scope"].endswith(TEST_SIGNAL_SCOPE_SUFFIX) and s["name"] not in TEST_HARNESS_INTERNAL_SIGNALS
    ]
    keep_ids = {s["id"] for s in keep}
    parsed["signals"] = keep
    parsed["changes"] = {vid: chs for vid, chs in parsed["changes"].items() if vid in keep_ids}
    return parsed


def _find_test(module_name: str, test_id: str):
    for entry in _test_registry.get("tests", {}).get(module_name, []):
        if entry["id"] == test_id:
            return entry
    return None


def _run_test_worker(run_id: str, module_name: str, test_id: str) -> None:
    with _tests_lock:
        reg = dict(_test_registry)
        entry = _find_test(module_name, test_id)
    sbt_dir = reg.get("sbtDir")
    pkg = reg.get("pkg")

    result = {"module": module_name, "testId": test_id, "status": "error", "log": "", "waveform": None}
    try:
        if not sbt_dir or not pkg or not entry:
            result["log"] = f"No test '{test_id}' registered for '{module_name}'. Re-run register_notebook.py."
        else:
            # state starts "queued" (set by start_test_run); flips to "running"
            # only once this run actually holds the lock and sbt is launched,
            # so concurrent "Run test" clicks visibly queue instead of racing.
            # subdir is keyed by test_id (not just module) so sibling tests
            # for the same module don't overwrite each other's waveform.
            cmd = ["sbt", "-batch", f"-DtestSubdir={test_id}", f"runMain {pkg}.{test_id}"]
            with _sbt_lock:
                with _runs_lock:
                    _test_runs[run_id] = {"state": "running", "result": None}
                proc = subprocess.run(
                    cmd, cwd=sbt_dir, capture_output=True, text=True, timeout=TEST_TIMEOUT_SECONDS
                )
            log = proc.stdout[-8000:]
            if proc.returncode != 0 and proc.stderr:
                log += "\n" + proc.stderr[-2000:]
            result["log"] = log
            result["status"] = "pass" if proc.returncode == 0 else "fail"

            vcd_path = Path(sbt_dir) / "build" / "canvas-tests" / test_id / "workdir-verilator" / "trace.vcd"
            if vcd_path.exists():
                try:
                    parsed = vcd_parser.parse_vcd_file(str(vcd_path))
                    result["waveform"] = _filter_waveform_signals(parsed)
                except Exception as e:  # a parse failure shouldn't hide the pass/fail result
                    result["log"] += f"\n[waveform parse failed: {type(e).__name__}: {e}]"
    except subprocess.TimeoutExpired:
        result["status"] = "error"
        result["log"] = f"Test run timed out after {TEST_TIMEOUT_SECONDS}s."
    except Exception as e:
        result["status"] = "error"
        result["log"] = f"{type(e).__name__}: {e}"

    with _runs_lock:
        _test_runs[run_id] = {"state": "done", "result": result}


def start_test_run(module_name: str, test_id: str) -> str:
    run_id = uuid.uuid4().hex
    with _runs_lock:
        _test_runs[run_id] = {"state": "queued", "result": None}
    threading.Thread(target=_run_test_worker, args=(run_id, module_name, test_id), daemon=True).start()
    return run_id


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

        if path.startswith("/api/run-test/"):
            run_id = path[len("/api/run-test/") :]
            with _runs_lock:
                run = _test_runs.get(run_id)
            if run is None:
                self._send_error_json("unknown run id", 404)
            else:
                self._send_json(run)
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

        if path == "/api/register-tests":
            try:
                body = self._read_json_body()
                result = register_tests(body.get("sbtDir"), body.get("pkg"), body.get("tests", {}))
                print(f"  registered {result['count']} test(s) for sbt project {body.get('sbtDir')}")
                self._send_json(result)
            except (ValueError, json.JSONDecodeError) as e:
                self._send_error_json(str(e), 400)
            return

        if path == "/api/run-test":
            try:
                body = self._read_json_body()
                module_name = str(body.get("module", "")).strip()
                if not module_name:
                    raise ValueError("module is required")
                entries = _test_registry.get("tests", {}).get(module_name, [])
                if not entries:
                    raise ValueError(f"no test registered for '{module_name}'")
                test_id = str(body.get("testId", "")).strip() or entries[0]["id"]
                if not any(e["id"] == test_id for e in entries):
                    raise ValueError(f"'{test_id}' is not a registered test for '{module_name}'")
                run_id = start_test_run(module_name, test_id)
                print(f"  starting test run '{run_id}' for '{module_name}' ({test_id})")
                self._send_json({"runId": run_id, "state": "queued"})
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
