package demo

import chisel3._
import chisel3.util._

/** ---------------------------------------------------------------------
  * Purely illustrative Chisel modules for module_canvas's own README and
  * screenshots. None of this is meant to be a real, verified design — it
  * exists to give the canvas a believable multi-level hierarchy to show
  * off: a leaf block or two, a couple of small parents, and one top-level
  * module tying it together.
  *
  *   DemoSystem
  *   ├── ButtonConditioner
  *   │   ├── Debouncer
  *   │   └── EdgeDetector
  *   ├── StreamRouter
  *   │   ├── SyncFifo   (x2)
  *   │   └── RoundRobinArbiter
  *   └── Prescaler
  * --------------------------------------------------------------------- */

/** Filters a noisy mechanical input down to a stable level, only flipping
  * once `cycles` consecutive samples agree. */
class Debouncer(cycles: Int = 8) extends Module {
  val io = IO(new Bundle {
    val raw = Input(Bool())
    val clean = Output(Bool())
  })

  val counter = RegInit(0.U(log2Ceil(cycles + 1).W))
  val state = RegInit(false.B)

  when(io.raw === state) {
    counter := 0.U
  }.elsewhen(counter === (cycles - 1).U) {
    state := io.raw
    counter := 0.U
  }.otherwise {
    counter := counter + 1.U
  }

  io.clean := state
}

/** Emits a single-cycle pulse on every rising edge of `level`. */
class EdgeDetector extends Module {
  val io = IO(new Bundle {
    val level = Input(Bool())
    val pulse = Output(Bool())
  })

  val prev = RegNext(io.level, false.B)
  io.pulse := io.level && !prev
}

/** Debounces a raw button line and turns it into one clean press pulse. */
class ButtonConditioner extends Module {
  val io = IO(new Bundle {
    val raw = Input(Bool())
    val pressed = Output(Bool())
  })

  val debouncer = Module(new Debouncer(cycles = 8))
  val edge = Module(new EdgeDetector)

  debouncer.io.raw := io.raw
  edge.io.level := debouncer.io.clean
  io.pressed := edge.io.pulse
}

/** Small synchronous FIFO, `depth` entries of `width` bits, ready/valid on
  * both sides. */
class SyncFifo(depth: Int = 8, width: Int = 8) extends Module {
  val io = IO(new Bundle {
    val enqValid = Input(Bool())
    val enqData = Input(UInt(width.W))
    val enqReady = Output(Bool())

    val deqValid = Output(Bool())
    val deqData = Output(UInt(width.W))
    val deqReady = Input(Bool())
  })

  val mem = Mem(depth, UInt(width.W))
  val enqPtr = RegInit(0.U(log2Ceil(depth).W))
  val deqPtr = RegInit(0.U(log2Ceil(depth).W))
  val count = RegInit(0.U((log2Ceil(depth) + 1).W))

  val doEnq = io.enqValid && io.enqReady
  val doDeq = io.deqValid && io.deqReady

  when(doEnq) {
    mem(enqPtr) := io.enqData
    enqPtr := enqPtr + 1.U
  }
  when(doDeq) {
    deqPtr := deqPtr + 1.U
  }
  count := count + doEnq.asUInt -& doDeq.asUInt

  io.enqReady := count =/= depth.U
  io.deqValid := count =/= 0.U
  io.deqData := mem(deqPtr)
}

/** A small fairness-rotating grant selector. Kept intentionally simple —
  * not a rigorous arbiter, just enough real logic to be a believable leaf
  * block for demo purposes. */
class RoundRobinArbiter(n: Int = 2) extends Module {
  val io = IO(new Bundle {
    val req = Input(Vec(n, Bool()))
    val grant = Output(Vec(n, Bool()))
    val valid = Output(Bool())
  })

  val pointer = RegInit(0.U(log2Ceil(n).W))
  val grants = Wire(Vec(n, Bool()))

  for (i <- 0 until n) {
    grants(i) := io.req(i) && (pointer === i.U)
  }

  io.grant := grants
  io.valid := grants.asUInt.orR

  when(io.req.asUInt.orR) {
    pointer := Mux(pointer === (n - 1).U, 0.U, pointer + 1.U)
  }
}

/** Merges two upstream byte streams into one downstream stream, arbitrated
  * between whichever FIFO has data. */
class StreamRouter extends Module {
  val io = IO(new Bundle {
    val enqAValid = Input(Bool())
    val enqAData = Input(UInt(8.W))
    val enqAReady = Output(Bool())

    val enqBValid = Input(Bool())
    val enqBData = Input(UInt(8.W))
    val enqBReady = Output(Bool())

    val deqValid = Output(Bool())
    val deqData = Output(UInt(8.W))
    val deqReady = Input(Bool())
  })

  val fifoA = Module(new SyncFifo(depth = 8, width = 8))
  val fifoB = Module(new SyncFifo(depth = 8, width = 8))
  val arb = Module(new RoundRobinArbiter(n = 2))

  fifoA.io.enqValid := io.enqAValid
  fifoA.io.enqData := io.enqAData
  io.enqAReady := fifoA.io.enqReady

  fifoB.io.enqValid := io.enqBValid
  fifoB.io.enqData := io.enqBData
  io.enqBReady := fifoB.io.enqReady

  arb.io.req(0) := fifoA.io.deqValid
  arb.io.req(1) := fifoB.io.deqValid

  fifoA.io.deqReady := arb.io.grant(0) && io.deqReady
  fifoB.io.deqReady := arb.io.grant(1) && io.deqReady

  io.deqValid := arb.io.valid
  io.deqData := Mux(arb.io.grant(0), fifoA.io.deqData, fifoB.io.deqData)
}

/** Divides the module clock down to a single-cycle enable pulse every
  * `divide` cycles. */
class Prescaler(divide: Int = 4) extends Module {
  val io = IO(new Bundle {
    val tick = Output(Bool())
  })

  val counter = RegInit(0.U(log2Ceil(divide).W))
  val wrap = counter === (divide - 1).U

  counter := Mux(wrap, 0.U, counter + 1.U)
  io.tick := wrap
}

/** Top-level demo design tying the pieces above together. Purely
  * illustrative — a button conditioner, two arbitrated FIFOs, and a
  * clock-divided tick output, nothing more. */
class DemoSystem extends Module {
  val io = IO(new Bundle {
    val buttonRaw = Input(Bool())
    val pressed = Output(Bool())

    val enqAValid = Input(Bool())
    val enqAData = Input(UInt(8.W))
    val enqAReady = Output(Bool())

    val enqBValid = Input(Bool())
    val enqBData = Input(UInt(8.W))
    val enqBReady = Output(Bool())

    val deqValid = Output(Bool())
    val deqData = Output(UInt(8.W))
    val deqReady = Input(Bool())

    val tick = Output(Bool())
  })

  val button = Module(new ButtonConditioner)
  val router = Module(new StreamRouter)
  val clockDiv = Module(new Prescaler(divide = 16))

  button.io.raw := io.buttonRaw
  io.pressed := button.io.pressed

  router.io.enqAValid := io.enqAValid
  router.io.enqAData := io.enqAData
  io.enqAReady := router.io.enqAReady

  router.io.enqBValid := io.enqBValid
  router.io.enqBData := io.enqBData
  io.enqBReady := router.io.enqBReady

  router.io.deqReady := io.deqReady
  io.deqValid := router.io.deqValid
  io.deqData := router.io.deqData

  io.tick := clockDiv.io.tick
}

/** Registers every demo module with a locally running module_canvas server.
  * Run with: sbt "runMain demo.RegisterDemo"
  */
object RegisterDemo extends App {
  modulecanvas.ModuleRegistry.registerModule(new DemoSystem)
  modulecanvas.ModuleRegistry.registerModule(new ButtonConditioner)
  modulecanvas.ModuleRegistry.registerModule(new Debouncer())
  modulecanvas.ModuleRegistry.registerModule(new EdgeDetector)
  modulecanvas.ModuleRegistry.registerModule(new StreamRouter)
  modulecanvas.ModuleRegistry.registerModule(new SyncFifo())
  modulecanvas.ModuleRegistry.registerModule(new RoundRobinArbiter())
  modulecanvas.ModuleRegistry.registerModule(new Prescaler())
}
