package modulecanvas

import chisel3._
import chisel3.experimental.BaseModule
import chisel3.reflect.DataMirror
import circt.stage.ChiselStage
import java.net.URI
import java.net.http.{HttpClient, HttpRequest, HttpResponse}
import java.time.Duration

/** Elaborates a module purely for inspection — every port tied off with
  * `DontCare`, since this circuit is never meant to be synthesized. Ports
  * are captured here (elaboration time, via `DataMirror`); the CHIRRTL text
  * needed for internal wiring isn't available until `ChiselStage.emitCHIRRTL`
  * returns, so that part happens afterward in `ModuleRegistry.registerModule`.
  */
private class RegistryProbe[T <: BaseModule](gen: => T) extends Module {
  val dut = Module(gen)
  DataMirror.modulePorts(dut).foreach { case (_, p) => p := DontCare }
  val portsSnapshot: Seq[(String, String, Int)] = ModuleRegistry.flattenPorts(dut)
  val dutName: String = dut.desiredName
}

/** Reports an elaborated module's flattened IO — and, best-effort, its
  * direct internal submodule wiring — to a locally running module_canvas
  * server (see `server/`) so it shows up as a draggable block in the
  * browser-based wiring canvas.
  *
  * Call once per module you want tracked:
  * {{{
  *   ModuleRegistry.registerModule(new DemoSystem)
  * }}}
  *
  * Never throws: if the server isn't running, it prints a note and the
  * calling program continues unaffected.
  */
object ModuleRegistry {

  def registerModule[T <: BaseModule](
      gen: => T,
      host: String = "localhost",
      port: Int = 8765
  ): Unit = {
    try {
      var probeRef: RegistryProbe[T] = null
      val chirrtl = ChiselStage.emitCHIRRTL {
        val p = new RegistryProbe(gen)
        probeRef = p
        p
      }
      val hierarchyJson = HierarchyParser.resolveInstanceType(chirrtl, "dut") match {
        case Some(exactType) => HierarchyParser.toJson(HierarchyParser.build(chirrtl, exactType))
        case None            => """{"children":[],"wires":[]}"""
      }
      postRegistration(probeRef.dutName, probeRef.portsSnapshot, hierarchyJson, host, port)
    } catch {
      case e: Exception =>
        println(s"[ModuleRegistry] elaboration failed: ${e.getMessage}")
    }
  }

  /** Flattens every top-level port of `mod` down to leaf signals, the same
    * way CIRCT flattens Bundle/Vec IO into individual SystemVerilog ports.
    */
  private[modulecanvas] def flattenPorts(mod: BaseModule): Seq[(String, String, Int)] =
    DataMirror.modulePorts(mod).flatMap { case (name, data) => flatten(name, data) }

  private def flatten(prefix: String, data: Data): Seq[(String, String, Int)] =
    data match {
      case rec: Record =>
        rec.elements.toSeq.reverse.flatMap { case (n, d) => flatten(s"${prefix}_$n", d) }
      case vec: Vec[_] =>
        vec.zipWithIndex.flatMap { case (d, i) => flatten(s"${prefix}_$i", d) }
      case leaf =>
        Seq((prefix, dirString(DataMirror.directionOf(leaf)), leaf.getWidth))
    }

  private def dirString(d: ActualDirection): String = d match {
    case ActualDirection.Input            => "input"
    case ActualDirection.Output           => "output"
    case _: ActualDirection.Bidirectional => "inout"
    case _                                => "output"
  }

  private[modulecanvas] def postRegistration(
      name: String,
      ports: Seq[(String, String, Int)],
      hierarchyJson: String,
      host: String,
      port: Int
  ): Unit = {
    try {
      val portsJson = ports
        .map { case (n, d, w) => s"""{"name":"${esc(n)}","dir":"$d","width":$w}""" }
        .mkString(",")
      val body = s"""{"name":"${esc(name)}","ports":[$portsJson],"hierarchy":$hierarchyJson}"""

      val client = HttpClient.newBuilder().connectTimeout(Duration.ofMillis(800)).build()
      val req = HttpRequest
        .newBuilder()
        .uri(URI.create(s"http://$host:$port/api/register"))
        .timeout(Duration.ofMillis(1500))
        .header("Content-Type", "application/json")
        .POST(HttpRequest.BodyPublishers.ofString(body))
        .build()
      val resp = client.send(req, HttpResponse.BodyHandlers.ofString())
      if (resp.statusCode() == 200)
        println(s"[ModuleRegistry] registered '$name' (${ports.length} ports) -> $host:$port")
      else
        println(s"[ModuleRegistry] server responded ${resp.statusCode()} for '$name': ${resp.body()}")
    } catch {
      case e: Exception =>
        println(
          s"[ModuleRegistry] could not reach $host:$port (${e.getMessage}). " +
            s"Is `python3 server/server.py` running?"
        )
    }
  }

  private def esc(s: String): String = s.replace("\\", "\\\\").replace("\"", "\\\"")
}
