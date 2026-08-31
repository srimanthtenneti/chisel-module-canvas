# Usage guide

Everything `module_canvas` can do, in detail. For a guided walkthrough
that reproduces the screenshots in the main README, see `examples.md`
instead — this document is the reference.

## 1. Running the server

```bash
python3 server/server.py                          # http://127.0.0.1:8765
python3 server/server.py --host 0.0.0.0 --port 9000 # reachable on your network, different port
```

No `pip install` step — `server.py` is pure standard library on purpose,
so it never needs to touch whatever virtualenv or conda environment your
Chisel/Jupyter setup already uses. Leave it running in a terminal for as
long as you're working; state lives in `server/data/` (created on first
use, git-ignored) and survives restarts.

Open the printed URL in a browser and leave the tab around — it polls the
registry every 2.5 seconds, so newly registered modules appear in the
palette without a page reload.

## 2. Registering modules

The canvas only knows about a module after something calls
`ModuleRegistry.registerModule`. Nothing is auto-detected from your build
or notebook kernel — this is deliberate: you decide what's worth putting
on the canvas, and when.

### From a plain sbt project

Copy `examples/src/main/scala/modulecanvas/ModuleRegistry.scala` and
`HierarchyParser.scala` into your own `src/main/scala/`, keeping the
`package modulecanvas` line, then call it from anywhere that already
elaborates your modules:

```scala
import modulecanvas.ModuleRegistry

object RegisterMine extends App {
  ModuleRegistry.registerModule(new TopModule)
  ModuleRegistry.registerModule(new SomeSubmoduleYouAlsoWantOnItsOwn)
}
```

```bash
sbt "runMain RegisterMine"
```

Point at a different host/port if you're not using the default:

```scala
ModuleRegistry.registerModule(new TopModule, host = "localhost", port = 9000)
```

### From a Chisel/Almond Jupyter notebook

Almond/Ammonite notebooks don't share a classpath with a compiled sbt
project by default — cells define classes standalone. Paste this once,
after your existing `$ivy` import cell:

```scala
import chisel3._
import chisel3.experimental.BaseModule
import chisel3.reflect.DataMirror
import _root_.circt.stage.ChiselStage
import java.net.URI
import java.net.http.{HttpClient, HttpRequest, HttpResponse}
import java.time.Duration

private class RegistryProbe[T <: BaseModule](gen: => T) extends Module {
  val dut = Module(gen)
  DataMirror.modulePorts(dut).foreach { case (_, p) => p := DontCare }
  val portsSnapshot: Seq[(String, String, Int)] = ModuleRegistry.flattenPorts(dut)
  val dutName: String = dut.desiredName
}

private[this] object HierarchyParser {
  final case class HPort(name: String, dir: String, width: Int)
  final case class HRef(ref: String, port: String)
  final case class HWire(from: HRef, to: HRef)
  final case class HChild(instName: String, moduleType: String, ports: Seq[HPort], children: Seq[HChild], wires: Seq[HWire])
  final case class Hierarchy(children: Seq[HChild], wires: Seq[HWire])

  private val InstRe = """^\s*inst\s+(\S+)\s+of\s+(\S+)""".r
  private val ConnectRe = """^\s*connect\s+([^,]+?),\s*([^,@]+?)\s*(?:@.*)?$""".r
  private val ModuleHeaderRe = """^\s*(?:public\s+)?module\s+(\S+)\s*:""".r
  private val CleanRefRe = """[A-Za-z][\w]*(\.[A-Za-z_]\w*|\[\d+\])*""".r

  def resolveInstanceType(text: String, instName: String): Option[String] =
    text.split("\n", -1).iterator.flatMap(InstRe.findFirstMatchIn).find(_.group(1) == instName).map(_.group(2))

  def build(text: String, rootExactName: String, maxDepth: Int = 6): Hierarchy =
    extractModuleBody(text, rootExactName) match {
      case None => Hierarchy(Nil, Nil)
      case Some(body) => Hierarchy(children = buildChildren(text, body, maxDepth, Set(rootExactName)), wires = parseWires(body))
    }

  private def buildChildren(text: String, body: Seq[String], depth: Int, seen: Set[String]): Seq[HChild] = {
    if (depth <= 0) return Nil
    parseChildRefs(body).flatMap { case (instName, exactType) =>
      if (seen.contains(exactType)) None
      else extractModuleBody(text, exactType).map { childBody =>
        HChild(instName, displayName(exactType), parseIOFields(childBody), buildChildren(text, childBody, depth - 1, seen + exactType), parseWires(childBody))
      }
    }
  }

  private def extractModuleBody(text: String, exactName: String): Option[Seq[String]] = {
    val lines = text.split("\n", -1)
    val headerIdx = lines.indexWhere(l => ModuleHeaderRe.findFirstMatchIn(l).exists(_.group(1) == exactName))
    if (headerIdx < 0) None
    else {
      val headerIndent = lines(headerIdx).takeWhile(_ == ' ').length
      Some(lines.drop(headerIdx + 1).takeWhile(l => l.trim.isEmpty || l.takeWhile(_ == ' ').length > headerIndent).toSeq)
    }
  }

  private def parseChildRefs(body: Seq[String]): Seq[(String, String)] =
    body.flatMap(l => InstRe.findFirstMatchIn(l).map(m => (m.group(1), m.group(2))))

  private def parseWires(body: Seq[String]): Seq[HWire] =
    body.flatMap(l => ConnectRe.findFirstMatchIn(l).flatMap(m => for { to <- normalizeRef(m.group(1)); from <- normalizeRef(m.group(2)) } yield HWire(from, to)))

  private def isBoilerplate(s: String): Boolean = { val last = s.split('.').last; last == "clock" || last == "reset" }

  private def normalizeRef(raw: String): Option[HRef] = {
    val s = raw.trim
    if (isBoilerplate(s) || !CleanRefRe.matches(s)) None
    else {
      val bracketed = s.replaceAll("""\[(\d+)\]""", "_$1")
      bracketed.split("\\.").toList match {
        case "io" :: rest if rest.nonEmpty => Some(HRef("$self", "io_" + rest.mkString("_")))
        case inst :: "io" :: rest if rest.nonEmpty => Some(HRef(inst, "io_" + rest.mkString("_")))
        case _ => None
      }
    }
  }

  private def parseIOFields(body: Seq[String]): Seq[HPort] = {
    val ioLine = body.find(_.trim.startsWith("output io :")).getOrElse(return Nil)
    val idx = ioLine.indexOf("output io :")
    val braceStart = ioLine.indexOf('{', idx)
    val braceEnd = ioLine.lastIndexOf('}')
    if (braceStart < 0 || braceEnd < 0 || braceEnd <= braceStart) return Nil
    splitTopLevel(ioLine.substring(braceStart + 1, braceEnd)).flatMap(parseField)
  }

  private def splitTopLevel(s: String): Seq[String] = {
    val out = scala.collection.mutable.ArrayBuffer[String]()
    var depth = 0; var start = 0
    for (i <- s.indices) s(i) match {
      case '{' | '<' | '[' => depth += 1
      case '}' | '>' | ']' => depth -= 1
      case ',' if depth == 0 => out += s.substring(start, i); start = i + 1
      case _ =>
    }
    out += s.substring(start)
    out.map(_.trim).filter(_.nonEmpty).toSeq
  }

  private def parseField(fieldText: String): Seq[HPort] = {
    val flip = fieldText.startsWith("flip ")
    val body = if (flip) fieldText.stripPrefix("flip ").trim else fieldText
    val colon = body.indexOf(':')
    if (colon < 0) return Nil
    val name = body.substring(0, colon).trim
    val typeText = body.substring(colon + 1).trim
    if (typeText.startsWith("{")) return Nil
    val vecCount = """\[(\d+)\]\s*$""".r.findFirstMatchIn(typeText).map(_.group(1).toInt)
    val width = """<(\d+)>""".r.findFirstMatchIn(typeText).map(_.group(1).toInt).getOrElse(1)
    val dir = if (flip) "input" else "output"
    vecCount match {
      case Some(n) => (0 until n).map(i => HPort(s"io_${name}_$i", dir, width))
      case None => Seq(HPort(s"io_$name", dir, width))
    }
  }

  private def displayName(exact: String): String = exact.replaceAll("_\\d+$", "")

  private def esc(s: String): String = s.replace("\\", "\\\\").replace("\"", "\\\"")
  private def portJson(p: HPort): String = s"""{"name":"${esc(p.name)}","dir":"${p.dir}","width":${p.width}}"""
  private def refJson(r: HRef): String = s"""{"ref":"${esc(r.ref)}","port":"${esc(r.port)}"}"""
  private def wireJson(w: HWire): String = s"""{"from":${refJson(w.from)},"to":${refJson(w.to)}}"""
  private def childJson(c: HChild): String =
    s"""{"instName":"${esc(c.instName)}","moduleType":"${esc(c.moduleType)}","ports":[${c.ports.map(portJson).mkString(",")}],"children":[${c.children.map(childJson).mkString(",")}],"wires":[${c.wires.map(wireJson).mkString(",")}]}"""
  def toJson(h: Hierarchy): String =
    s"""{"children":[${h.children.map(childJson).mkString(",")}],"wires":[${h.wires.map(wireJson).mkString(",")}]}"""
}

object ModuleRegistry {
  def registerModule[T <: BaseModule](gen: => T, host: String = "localhost", port: Int = 8765): Unit = {
    try {
      var probeRef: RegistryProbe[T] = null
      val chirrtl = ChiselStage.emitCHIRRTL { val p = new RegistryProbe(gen); probeRef = p; p }
      val hierarchyJson = HierarchyParser.resolveInstanceType(chirrtl, "dut") match {
        case Some(exactType) => HierarchyParser.toJson(HierarchyParser.build(chirrtl, exactType))
        case None => """{"children":[],"wires":[]}"""
      }
      postRegistration(probeRef.dutName, probeRef.portsSnapshot, hierarchyJson, host, port)
    } catch { case e: Exception => println(s"[ModuleRegistry] elaboration failed: ${e.getMessage}") }
  }

  private[this] def flatten(prefix: String, data: Data): Seq[(String, String, Int)] = data match {
    case rec: Record => rec.elements.toSeq.reverse.flatMap { case (n, d) => flatten(s"${prefix}_$n", d) }
    case vec: Vec[_]  => vec.zipWithIndex.flatMap { case (d, i) => flatten(s"${prefix}_$i", d) }
    case leaf         => Seq((prefix, dirString(DataMirror.directionOf(leaf)), leaf.getWidth))
  }
  def flattenPorts(mod: BaseModule): Seq[(String, String, Int)] =
    DataMirror.modulePorts(mod).flatMap { case (name, data) => flatten(name, data) }

  private def dirString(d: ActualDirection): String = d match {
    case ActualDirection.Input => "input"
    case ActualDirection.Output => "output"
    case _: ActualDirection.Bidirectional => "inout"
    case _ => "output"
  }

  def postRegistration(name: String, ports: Seq[(String, String, Int)], hierarchyJson: String, host: String, port: Int): Unit = {
    try {
      val portsJson = ports.map { case (n, d, w) => s"""{"name":"$n","dir":"$d","width":$w}""" }.mkString(",")
      val body = s"""{"name":"$name","ports":[$portsJson],"hierarchy":$hierarchyJson}"""
      val client = HttpClient.newBuilder().connectTimeout(Duration.ofMillis(800)).build()
      val req = HttpRequest.newBuilder().uri(URI.create(s"http://$host:$port/api/register"))
        .timeout(Duration.ofMillis(1500)).header("Content-Type", "application/json")
        .POST(HttpRequest.BodyPublishers.ofString(body)).build()
      val resp = client.send(req, HttpResponse.BodyHandlers.ofString())
      println(if (resp.statusCode() == 200) s"[ModuleRegistry] registered '$name' (${ports.length} ports)"
              else s"[ModuleRegistry] server responded ${resp.statusCode()}: ${resp.body()}")
    } catch { case e: Exception => println(s"[ModuleRegistry] could not reach $host:$port (${e.getMessage}).") }
  }
}
```

Then, after defining any module in a later cell:

```scala
ModuleRegistry.registerModule(new YourModule)
```

Re-run that line any time you change the module — see "Versioning" below.
This never throws: if the server isn't running, you get a printed note
and the notebook session continues unaffected.

### Registering many modules at once: `register_notebook.py`

If your modules already live in a Chisel/Almond notebook, you don't have
to write any `registerModule` calls by hand at all:

```bash
python3 server/register_notebook.py path/to/Notebook.ipynb --sbt-dir .
```

This scans the notebook for every top-level `class ... extends Module`
(or `RawModule`), generates a small standalone sbt sub-project
(`src/main/scala/notebookgen/` by default — override with `--pkg`) that
instantiates and registers each module whose constructor needs no
required arguments, and runs it. Modules with required constructor
parameters are skipped and listed, since the tool has no way to know what
value you'd want.

It also scans for any `simulate(new X) { dut => ... }` cell already in
the notebook — the same `chisel3.simulator` test style you'd write
interactively — and, for each one, generates a VCD-tracing runnable
wrapper registered with the server as that module's "Run test" action
(see "Tests & waveforms" below). A module can have more than one test
cell; all of them show up, each labeled from its leading `//` comment if
it has one.

Useful flags:

```bash
python3 server/register_notebook.py Notebook.ipynb --sbt-dir examples \
  --host localhost --port 8765 \
  --registry-pkg modulecanvas \   # package containing your ModuleRegistry
  --pkg notebookgen \             # package name for the generated driver
  --no-run                        # generate the Scala only, don't invoke sbt
```

`--registry-pkg` matters if you copied `ModuleRegistry.scala` into your
own project under a different package name than `modulecanvas` — it's
what the generated driver calls `registerModule` on. Re-run the whole
command any time the notebook changes; it regenerates and re-registers
everything from scratch, safely (existing versions on the server aren't
touched — see "Versioning" below).

### What gets sent

Every port on the module — including nested `Bundle`/`Vec` fields,
flattened to leaves exactly the way CIRCT flattens them for generated
SystemVerilog (`edata: Vec(4, UInt(8.W))` becomes four ports,
`io_edata_0`..`io_edata_3`) — plus, for a parent module, its direct
submodule instances and their interconnect (see "Internal wiring" below).

## 3. The canvas

### Placing blocks

Click a module in the left palette to place it at a default position. If
it has more than one registered version, a dropdown next to it lets you
place an older one instead of the latest.

### Moving, selecting, deleting

Drag a block's title bar or body to move it (not its pins or icon
buttons). Click a block or a wire to select it; `Delete`/`Backspace`
removes the current selection — for a block, its wires go with it.

### Wiring

Drag from a pin (the small circle on a block's edge — input pins on the
left, output on the right, inout along the bottom) to another pin. The
wire is checked immediately:

| attempted wire | result |
|---|---|
| output → input | allowed |
| output/inout → inout | allowed |
| inout → inout | allowed (drawn dashed — no single defined driver) |
| output → output | **blocked** — would short two drivers together |
| input → input | **blocked** — nothing would drive the wire |
| anything → an input that already has a wire | **blocked** — one driver per input |

A blocked attempt shows a toast explaining why and doesn't create a wire.

### Custom blocks

"+ Custom block" opens a form: a name, then repeatable rows for each port
(name, direction, width) before you place it. Duplicate port names within
one custom block are dropped with a warning rather than silently
colliding. Custom blocks have no internal structure — no `▸` — since
there's nothing elaborated behind them.

### Duplicating blocks

Select a block, `Ctrl`/`Cmd`+`C`, then `Ctrl`/`Cmd`+`V` — repeat the paste
for as many independent copies as you want. Each paste gets its own
ports and can be wired independently; it's auto-labeled `Name #2`,
`Name #3`, and so on. Double-click any block's title (top-level or,
for a module block, its own title only — not a nested child's) to give it
a more meaningful instance name, e.g. `Lane0`.

### Internal wiring (expand / collapse)

A block with a `▸` next to its title has submodules. Click it to expand
the block: its submodules are laid out inside it, with the wiring between
them drawn dashed and read-only. Any child that itself has submodules can
be expanded again, recursively. A block's own outer ports stay in the
same place whether it's expanded or not, so wires you've drawn to it from
elsewhere don't move.

**What's recovered, and what isn't.** This comes from parsing CIRCT's
CHIRRTL text output, not from a full expression evaluator. A direct
pass-through — `connect childA.io.x, childB.io.y` in FIRRTL terms, which
in Chisel is just `childA.io.x := childB.io.y` — is recovered correctly,
and covers most interconnect in a typical design. A signal computed
through a `node` — a `Mux`, a bit-slice, arithmetic, anything that isn't
a bare reference — isn't traced back to its true source, so that
particular port won't show an internal wire. This is why a leaf module
whose whole job *is* computation (an ALU, a priority encoder) usually
shows its ports but no internal wiring: its logic is exactly the kind of
expression this doesn't trace through.

A whole-`Vec` connect (`io.res := someChild.io.res`, written as one
assignment rather than element-by-element) is expanded into one wire per
matching flattened index on both sides; if the two sides don't have the
same number of indexed ports, nothing is drawn for that connect rather
than guessing a pairing.

### Tests & waveforms

A module block that has one or more `simulate(...)` cells registered
against it (via `register_notebook.py` — see above) shows a "Tests"
section in the Inspector tab when selected, one card per test, each with
its own "Run test" button and status pill (`queued` → `running` →
`pass`/`fail`/`error`). Runs go through `sbt` in the background: firing
off several at once queues them rather than racing sbt's own server
socket, so nothing silently fails from clicking around.

Once a run finishes, its card gets a "view waveform" link that switches
to the Waveform tab and renders the VCD trace `chisel3.simulator` wrote
during that run — signal names down the left, zoomable/scrollable
timeline, `0`/`1`/`x`/`z` and multi-bit values all rendered directly from
the trace file, not summarized. Internal testbench-only signals are
filtered out so what you see matches the module's actual IO plus any
named internal registers/wires it declares.

Blocks with no test cell — custom blocks, top-level IO markers, and any
module you haven't written a `simulate(...)` cell for — show a note in
the Inspector saying so instead of an empty test list.

### Notes

Every block (module, custom, or IO marker) has a free-text "Note" field
in the Inspector tab, saved with the rest of the canvas. It's not shown
on the block itself — it's a place to record context (why this version
is pinned, what's still TODO on this wiring, a link to the ticket) that
would otherwise only live in your head or a separate doc.

### Top-level IO markers

"+ Top-level IO" opens a small modal for exactly one named, directioned,
sized port — no repeatable rows, since a boundary marker only ever needs
one. The resulting block renders with a dashed amber outline instead of
the normal panel styling, so it reads at a glance as "this is the edge of
the system" rather than another internal block. Wire it to a pin the
same way you'd wire any two blocks together; the connection-direction
rules are unchanged (a marker's output pin drives an input pin
elsewhere, and so on).

### Auto-organize layout

The toolbar's "Auto-organize" button re-lays-out every block on the
canvas in one pass: it layers blocks left-to-right by data flow (a
block's layer is one more than the deepest layer of anything driving it)
and orders blocks within each layer by a barycenter heuristic against
their neighbors, the standard approach for minimizing edge crossings in a
layered graph. Useful after placing blocks in whatever order you clicked
them in the palette, or after a session of ad hoc dragging has left wires
crossing each other more than they need to. It repositions blocks only —
it doesn't change any wiring — and is itself undoable only by dragging
things back, so it's worth a glance before your next save if the new
layout isn't what you wanted.

### Multi-select

Right-click and drag anywhere on empty canvas to draw a selection box;
every block it overlaps is selected together (a tiny drag, under a few
pixels, just clears the selection instead — so an accidental right-click
doesn't select anything). `Shift`-click toggles one block into or out of
an existing selection without needing to redraw the box. With multiple
blocks selected:

- Drag any one of them to move the whole group together, preserving
  their relative positions.
- `Ctrl`/`Cmd`+`C` then `Ctrl`/`Cmd`+`V` copies the whole group — including
  the wires between selected blocks — as a new set of independent
  instances.
- `Delete`/`Backspace` removes every selected block (and their wires) at
  once.

### Versioning

Each `registerModule` call for a given module name adds a new version;
nothing is overwritten. The palette always offers the latest by default,
with older versions reachable from the per-module dropdown when there's
more than one. A block already on the canvas keeps whatever version it
was placed with — click the small refresh icon in its title bar to jump
it to whatever's currently latest, which preserves any wires attached to
ports that still exist by name and drops the rest.

### Pan, zoom, save, export

- Drag empty canvas to pan, scroll to zoom, or use the toolbar's
  `−` / `+` / `reset` controls.
- The canvas autosaves 8 seconds after your last change, or immediately
  on clicking "Save"; it reloads automatically on next page open. State
  lives in `server/data/canvas.json`.
- "Export SVG" downloads a static snapshot with its styling inlined, so
  it renders correctly outside the app (unlike copying the live page,
  which depends on `style.css`).
- "Clear canvas" removes every block and wire — it asks for confirmation
  first, and there's no undo.

## Known limits

- This is a wiring **diagram** tool, not a simulator: connections are
  checked for direction legality, not bit-width compatibility.
- `inout`/`Analog` handling is intentionally simple (any inout can pair
  with any inout or input) and is exercised mainly through hand-built
  custom blocks, since `Analog` is uncommon in ordinary Chisel designs.
- Module tracking is push-based, as described above — nothing is watched
  or auto-discovered.
