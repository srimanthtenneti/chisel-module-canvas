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
import _root_.circt.stage.ChiselStage   // chisel3._ shadows the bare `circt` package — keep the _root_ prefix
import java.net.URI
import java.net.http.{HttpClient, HttpRequest, HttpResponse}
import java.time.Duration

private class RegistryProbe[T <: BaseModule](gen: => T) extends Module {
  val dut = Module(gen)
  DataMirror.modulePorts(dut).foreach { case (_, p) => p := DontCare }
  val portsSnapshot: Seq[(String, String, Int)] = ModuleRegistry.flattenPorts(dut)
  val dutName: String = dut.desiredName
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
