# Examples

A guided walkthrough using the dummy modules bundled in `examples/`. None
of this is a real, verified design — it exists purely to give the canvas
something with genuine multi-level structure to show off, and to give you
a working example to copy from when you wire up your own project.

## The example design

`examples/src/main/scala/demo/DemoModules.scala` defines a small,
intentionally simple hierarchy:

```text
DemoSystem
├── ButtonConditioner        (debounces + edge-detects a raw button line)
│   ├── Debouncer
│   └── EdgeDetector
├── StreamRouter              (merges two byte streams into one, arbitrated)
│   ├── SyncFifo               (fifoA)
│   ├── SyncFifo               (fifoB)   <- same module, two instances
│   └── RoundRobinArbiter      (arb)
└── Prescaler                 (clockDiv — a clock-cycle divider)
```

This shape was picked deliberately: a top module with three children, one
of which (`StreamRouter`) itself has three children including two
instances of the *same* leaf module (`SyncFifo`) — enough variety to
exercise placement, expansion, and the "same module, multiple instances"
case in one design.

## 1. Launch and register

```bash
python3 server/server.py &
cd examples
sbt "runMain demo.RegisterDemo"
```

`RegisterDemo` is just eight `ModuleRegistry.registerModule(...)` calls —
open `examples/src/main/scala/demo/DemoModules.scala` and look at the
bottom of the file. Open `http://127.0.0.1:8765/` and you should see all
eight in the left palette, each showing its in/out/inout port counts and
— for the three with submodules — an "N internal" count.

## 2. Place and wire a small scene

This reproduces the main README's hero screenshot.

1. Click **DemoSystem** in the palette to place it.
2. Click **ButtonConditioner** to place a second, independent instance —
   drag it below and to the left so the two don't overlap.
3. Click **+ Custom block**. Name it `LED Driver`, and add two ports:
   `pressed_in` (input) and `led_out` (output). Click **Place block**.
4. Drag from `ButtonConditioner`'s `io_pressed` output pin to
   `LED Driver`'s `pressed_in` input pin.
5. Drag from `LED Driver`'s `led_out` output pin to `DemoSystem`'s
   `io_deqReady` input pin (any spare input works — this is just to show
   a second wire; it isn't meant to be functionally meaningful).

You now have two registered modules and one hand-drawn block, wired
together with direction-checked, color-coded connections:

![Two registered modules and a custom block, wired](images/palette-and-canvas.png)

Try wiring `LED Driver`'s `led_out` to `ButtonConditioner`'s `io_raw`
input too — it's allowed, since `io_raw` doesn't have a driver yet. Then
try wiring it to `io_pressed` (an output) instead: you'll get a toast
explaining that two outputs can't be wired together.

## 3. Expand DemoSystem's internal wiring

Clear the canvas ("Clear canvas" in the left panel) and place a fresh
**DemoSystem**. Click the `▸` next to its title.

Three children appear inside it — `button` (`ButtonConditioner`), `router`
(`StreamRouter`), and `clockDiv` (`Prescaler`) — connected by dashed,
read-only wires recovered from the actual elaborated design: DemoSystem's
`io_buttonRaw` feeding `button.io_raw`, its `io_pressed` output coming
from `button.io_pressed`, and so on.

Click the `▸` on the `router` child too — it expands to show `fifoA`,
`fifoB` (both `SyncFifo`, both correctly labeled by their distinct
instance names even though they're the same module), and `arb`
(`RoundRobinArbiter`), each wired to the others and to `router`'s own
boundary:

![DemoSystem expanded two levels deep](images/expanded-hierarchy.png)

Notice what's *not* wired: inside `RoundRobinArbiter`, the `pointer`
register and the grant logic involve a `Mux` and an equality comparison,
so `io_grant`/`io_valid` show as ports but nothing feeds them internally
in the diagram — that's the "direct connections only" limit described in
`usage-guide.md`, not a bug. Compare that to `StreamRouter` itself, whose
connections to `fifoA`/`fifoB`/`arb` are all straight pass-throughs and
are fully recovered.

## 4. Make four copies of a lane

A common reason to want duplication: a design with several identical
instances of one block, like `StreamRouter`'s two `SyncFifo`s, or a SIMD
design with four identical execution lanes. Try it:

1. Place a single `SyncFifo`.
2. Select it, `Ctrl`/`Cmd`+`C`, then `Ctrl`/`Cmd`+`V` three times.
3. You now have four independent `SyncFifo` blocks, auto-labeled
   `SyncFifo #1`.. `#4`. Double-click each title to rename them
   `fifo0`..`fifo3`, drag them into a row, and wire each one up
   independently — exactly the workflow for laying out a multi-lane
   design by hand.

## 5. Bump a version and refresh

Edit `Debouncer` in `DemoModules.scala` — add a port, say — then:

```bash
sbt "runMain demo.RegisterDemo"
```

Back in the browser, `Debouncer`'s palette entry now shows `latest v2`
with a version dropdown. If you already had a v1 `Debouncer` placed on
the canvas, its refresh icon (in its title bar) is now live — click it to
jump that instance to v2, keeping any wires on ports that still exist by
name.
