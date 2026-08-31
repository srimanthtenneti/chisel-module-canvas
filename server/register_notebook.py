#!/usr/bin/env python3
"""register_notebook.py — auto-register every Chisel module defined in a
notebook with a running module_canvas server, with no manual
`ModuleRegistry.registerModule(...)` calls required.

It scans the notebook for every top-level `class ... extends Module` (or
`RawModule`) definition, generates a small Scala driver that instantiates
and registers each one that can be built with no required constructor
arguments, and runs it via sbt. It also detects a `simulate(new X) {...}`
test cell associated with each module (used by the canvas's "Run test"
feature) and generates a runnable, VCD-tracing wrapper for each one.

Usage:
    python3 server/register_notebook.py examples/DemoNotebook.ipynb --sbt-dir examples
    python3 server/register_notebook.py path/to/Notebook.ipynb --port 8765 --sbt-dir .
    python3 server/register_notebook.py path/to/Notebook.ipynb --no-run   # codegen only
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

MODULE_HEADER_RE = re.compile(
    r"^\s*(?:private\s+|final\s+|sealed\s+|abstract\s+)*class\s+(\w+)"
    r"(?:\[[^\]]*\])?"
    r"(?:\s*(\([^)]*\)))?"
    r"\s+extends\s+(Module|RawModule)\b"
)
ANY_DEF_RE = re.compile(r"^\s*(?:private\s+|final\s+|sealed\s+|abstract\s+)*(?:class|object|trait|case class)\s+(\w+)")
IMPORT_RE = re.compile(r"^\s*import\s+(?!\$)(\S.*)$")
SIMULATE_NEW_RE = re.compile(r"simulate\s*\(\s*new\s+(\w+)")
INFRA_MARKERS = ("object ModuleRegistry", "object HierarchyParser", "class RegistryProbe")


def load_cells(nb_path: Path):
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    return [
        (i, "".join(c.get("source", [])))
        for i, c in enumerate(nb["cells"])
        if c.get("cell_type") == "code"
    ]


def strip_ammonite_magic(src: str) -> str:
    """Drop Ammonite-only `import $ivy...`/`$plugin...`/`$cp...` lines —
    not valid plain Scala; the sbt project already has the same deps."""
    return "\n".join(l for l in src.split("\n") if not re.match(r"^\s*import\s+\$", l))


def is_infra_cell(src: str) -> bool:
    return any(marker in src for marker in INFRA_MARKERS)


def collect_definitions(cells):
    """Returns (keep_cell_indices, module_classes, all_imports).
    module_classes: {name: (ctor_params_text_or_None, cell_index)}
    """
    latest_def_cell = {}
    module_classes = {}
    all_imports = []

    for i, src in cells:
        if is_infra_cell(src):
            continue
        for line in src.split("\n"):
            m = IMPORT_RE.match(line)
            if m and m.group(1) not in all_imports:
                all_imports.append(m.group(1))
        for line in src.split("\n"):
            mh = MODULE_HEADER_RE.match(line)
            if mh:
                name, params, _ = mh.groups()
                module_classes[name] = (params, i)
            ma = ANY_DEF_RE.match(line)
            if ma:
                latest_def_cell[ma.group(1)] = i

    # only the cell that most recently (re)defines a name should be emitted
    keep_cells = sorted(set(latest_def_cell.values()))
    # drop module classes whose defining cell was itself superseded (same
    # name redefined later, e.g. a class rewritten in a later cell)
    module_classes = {
        name: v for name, v in module_classes.items() if latest_def_cell.get(name) == v[1]
    }
    return keep_cells, module_classes, all_imports


def params_are_instantiable(params_text: str | None) -> bool:
    if params_text is None:
        return True
    inner = params_text.strip()[1:-1].strip()
    if not inner:
        return True
    depth = 0
    parts, start = [], 0
    for i, ch in enumerate(inner):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(inner[start:i])
            start = i + 1
    parts.append(inner[start:])
    return all("=" in p for p in parts)


def new_expr(name: str, params_text: str | None) -> str:
    return f"new {name}()" if params_text is not None else f"new {name}"


LEADING_COMMENT_RE = re.compile(r"^\s*//\s*(.+?)\s*$")


def find_test_cells(name: str, cells) -> list[str]:
    """Every distinct cell whose body calls simulate(new <name> ...), in
    notebook order — a module can have more than one test cell (e.g. a
    basic-ops test and a separate edge-case test), and all of them should
    show up on the canvas, not just whichever one appears last."""
    found = []
    for i, src in cells:
        if is_infra_cell(src):
            continue
        if any(m.group(1) == name for m in SIMULATE_NEW_RE.finditer(src)):
            found.append(src)
    return found


def test_label(src: str, index: int, total: int) -> str:
    """A human-readable label for one test cell: its leading `//` comment
    if it has one, else a generic "Test N of M"."""
    for line in src.split("\n"):
        if not line.strip():
            continue
        m = LEADING_COMMENT_RE.match(line)
        if m:
            return m.group(1)[:60]
        break
    return f"Test {index + 1}" if total > 1 else "Test"


def build_definitions_source(pkg: str, imports, keep_cells, cells_by_index) -> str:
    header_imports = "\n".join(f"import {imp}" for imp in imports if not imp.startswith("$"))
    body = "\n\n".join(strip_ammonite_magic(cells_by_index[i]) for i in keep_cells)
    return f"package {pkg}\n\n{header_imports}\n\n{body}\n"


def build_register_driver(
    pkg: str, registry_pkg: str, module_classes: dict, skip: set, host: str, port: int
) -> tuple[str, list[str]]:
    lines = [f"package {pkg}", "", "object RegisterFromNotebook extends App {"]
    registered = []
    for name, (params, _) in sorted(module_classes.items()):
        if name in skip:
            continue
        if not params_are_instantiable(params):
            print(f"  skip '{name}': constructor has required (non-default) parameters")
            continue
        lines.append(
            f'  {registry_pkg}.ModuleRegistry.registerModule({new_expr(name, params)}, host = "{host}", port = {port})'
        )
        registered.append(name)
    lines.append("}")
    return "\n".join(lines) + "\n", registered


def build_test_harness_source(pkg: str, imports, test_cells: dict) -> tuple[str, dict]:
    """One `object Test_<Name>[_<N>] extends App` per detected test cell,
    using a VCD-tracing simulate() instead of EphemeralSimulator's, driven
    by the -DtestSubdir system property so the server can find the
    resulting trace.vcd afterward. test_cells: {name: [cellSrc, ...]}.
    Returns (source, {name: [{"id": objectName, "label": ...}, ...]})."""
    header_imports = "\n".join(
        f"import {imp}" for imp in imports if not imp.startswith("$") and "EphemeralSimulator" not in imp
    )
    harness = f"""package {pkg}

{header_imports}
import chisel3.simulator._
import chisel3.simulator.PeekPokeAPI._
import chisel3.testing.HasTestingDirectory
import svsim.verilator.Backend.CompilationSettings.{{TraceStyle, TraceKind}}
import java.nio.file.Paths

private[{pkg}] object TestHarness extends SimulatorAPI {{
  implicit val vcdMod: svsim.BackendSettingsModifications = new svsim.BackendSettingsModifications {{
    def apply(s: svsim.Backend.Settings): svsim.Backend.Settings = s match {{
      case c: svsim.verilator.Backend.CompilationSettings =>
        c.copy(traceStyle = Some(TraceStyle(TraceKind.Vcd)))
      case other => other
    }}
  }}
  // Fixed, predictable output path (no random timestamp folder) so the
  // server can find trace.vcd without globbing: build/canvas-tests/<subdir>/.
  // subdir is per *test object*, not just per module, so sibling tests for
  // the same module don't clobber each other's waveform.
  def simulate[T <: chisel3.Module](gen: => T)(body: T => Unit): Unit = {{
    val subdir = sys.props.getOrElse("testSubdir", "default")
    implicit val dir: HasTestingDirectory = new HasTestingDirectory {{
      override def getDirectory = Paths.get("build", "canvas-tests")
    }}
    val settings = Settings.default[T].copy(enableWavesAtTimeZero = true)
    this.simulate(module = gen, settings = settings, subdirectory = Some(subdir))(body)
  }}
}}
import {pkg}.TestHarness.simulate
"""
    manifest: dict = {}
    blocks = [harness]
    for name, srcs in sorted(test_cells.items()):
        entries = []
        multi = len(srcs) > 1
        for idx, src in enumerate(srcs):
            obj_name = f"Test_{name}_{idx + 1}" if multi else f"Test_{name}"
            clean = strip_ammonite_magic(src)
            clean = re.sub(r"^\s*import\s+chisel3\.simulator\.EphemeralSimulator\._\s*$", "", clean, flags=re.M)
            blocks.append(f"object {obj_name} extends App {{\n{clean}\n}}")
            entries.append({"id": obj_name, "label": test_label(src, idx, len(srcs))})
        manifest[name] = entries
    return "\n\n".join(blocks) + "\n", manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("notebook", type=Path)
    ap.add_argument("--sbt-dir", type=Path, default=Path("."), help="sbt project root (default: cwd)")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pkg", default="notebookgen", help="generated Scala package name")
    ap.add_argument(
        "--registry-pkg", default="modulecanvas",
        help="Scala package containing ModuleRegistry.registerModule (default: modulecanvas)",
    )
    ap.add_argument("--no-run", action="store_true", help="only generate the Scala files, don't invoke sbt")
    args = ap.parse_args()

    cells = load_cells(args.notebook)
    cells_by_index = dict(cells)
    keep_cells, module_classes, imports = collect_definitions(cells)

    test_cells = {}
    for name in module_classes:
        srcs = find_test_cells(name, cells)
        if srcs:
            test_cells[name] = srcs

    gen_dir = args.sbt_dir / "src" / "main" / "scala" / args.pkg
    gen_dir.mkdir(parents=True, exist_ok=True)

    defs_src = build_definitions_source(args.pkg, imports, keep_cells, cells_by_index)
    (gen_dir / "NotebookDefinitions.scala").write_text(defs_src, encoding="utf-8")

    driver_src, registered = build_register_driver(
        args.pkg, args.registry_pkg, module_classes, skip=set(), host=args.host, port=args.port
    )
    (gen_dir / "RegisterFromNotebook.scala").write_text(driver_src, encoding="utf-8")

    harness_src, test_manifest = build_test_harness_source(args.pkg, imports, test_cells)
    (gen_dir / "TestHarness.scala").write_text(harness_src, encoding="utf-8")

    total_test_objects = sum(len(v) for v in test_manifest.values())
    manifest = {
        "package": args.pkg,
        "modules": sorted(module_classes.keys()),
        "registered": registered,
        "skipped": sorted(set(module_classes.keys()) - set(registered)),
        "tests": test_manifest,
    }
    (gen_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Found {len(module_classes)} module class(es) in {args.notebook.name}:")
    print(f"  registering: {', '.join(registered) if registered else '(none)'}")
    if manifest["skipped"]:
        print(f"  skipped (required constructor args): {', '.join(manifest['skipped'])}")
    if test_manifest:
        detail = ", ".join(f"{name} ({len(v)})" if len(v) > 1 else name for name, v in sorted(test_manifest.items()))
        print(f"  test cells detected for: {detail} — {total_test_objects} test object(s) total")
    else:
        print("  test cells detected for: (none)")
    print(f"Generated Scala under {gen_dir}")

    if args.no_run:
        print("--no-run given, not invoking sbt.")
        return

    print(f"\nRunning sbt \"runMain {args.pkg}.RegisterFromNotebook\" ...")
    result = subprocess.run(
        ["sbt", "-batch", f"runMain {args.pkg}.RegisterFromNotebook"],
        cwd=args.sbt_dir,
        capture_output=True,
        text=True,
    )
    print(result.stdout[-4000:])
    if result.returncode != 0:
        print(result.stderr[-2000:], file=sys.stderr)
        sys.exit(result.returncode)

    if test_manifest:
        payload = json.dumps(
            {"sbtDir": str(args.sbt_dir.resolve()), "pkg": args.pkg, "tests": test_manifest}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://{args.host}:{args.port}/api/register-tests",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            print(
                f"Registered {total_test_objects} test object(s) across {len(test_manifest)} module(s) "
                f"with the canvas — 'Run test' is now available for: {', '.join(sorted(test_manifest))}"
            )
        except urllib.error.URLError as e:
            print(f"Warning: could not register tests with the canvas server ({e}). "
                  f"'Run test' won't be available until this succeeds.", file=sys.stderr)


if __name__ == "__main__":
    main()
