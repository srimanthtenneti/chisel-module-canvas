package modulecanvas

/** Recovers a module's internal submodule instances and their direct
  * point-to-point wiring from CIRCT's CHIRRTL text dump.
  *
  * This is intentionally limited to *direct* connections — lines of the
  * shape `connect a.io.x, b.io.y`. Any signal that passes through a `node`
  * (bit-slices, muxes, arithmetic — e.g. a writeback mux or a priority
  * encoder) isn't traced back to its true fan-in; that child's own internal
  * logic simply won't show wires for that port. This covers the common
  * case — most interconnect between submodules is a straight pass-through —
  * without attempting a full FIRRTL expression evaluator.
  */
private[modulecanvas] object HierarchyParser {

  final case class HPort(name: String, dir: String, width: Int)
  final case class HRef(ref: String, port: String) // ref == "$self" for the containing module's own boundary
  final case class HWire(from: HRef, to: HRef)
  final case class HChild(
      instName: String,
      moduleType: String,
      ports: Seq[HPort],
      children: Seq[HChild],
      wires: Seq[HWire]
  )
  final case class Hierarchy(children: Seq[HChild], wires: Seq[HWire])

  private val InstRe = """^\s*inst\s+(\S+)\s+of\s+(\S+)""".r
  private val ConnectRe = """^\s*connect\s+([^,]+?),\s*([^,@]+?)\s*(?:@.*)?$""".r
  private val ModuleHeaderRe = """^\s*(?:public\s+)?module\s+(\S+)\s*:""".r
  private val CleanRefRe = """[A-Za-z][\w]*(\.[A-Za-z_]\w*|\[\d+\])*""".r

  /** Finds the exact (possibly disambiguation-suffixed) FIRRTL name that
    * `instName` resolves to within the text — e.g. `dut` might resolve to
    * `DemoSystem_2` if a prior elaboration in the same JVM session already
    * used the plain name.
    */
  def resolveInstanceType(text: String, instName: String): Option[String] =
    text
      .split("\n", -1)
      .iterator
      .flatMap(InstRe.findFirstMatchIn)
      .find(_.group(1) == instName)
      .map(_.group(2))

  def build(text: String, rootExactName: String, maxDepth: Int = 6): Hierarchy =
    extractModuleBody(text, rootExactName) match {
      case None => Hierarchy(Nil, Nil)
      case Some(body) =>
        Hierarchy(
          children = buildChildren(text, body, maxDepth, Set(rootExactName)),
          wires = parseWires(body)
        )
    }

  private def buildChildren(text: String, body: Seq[String], depth: Int, seen: Set[String]): Seq[HChild] = {
    if (depth <= 0) return Nil
    parseChildRefs(body).flatMap { case (instName, exactType) =>
      if (seen.contains(exactType)) None
      else
        extractModuleBody(text, exactType).map { childBody =>
          HChild(
            instName = instName,
            moduleType = displayName(exactType),
            ports = parseIOFields(childBody),
            children = buildChildren(text, childBody, depth - 1, seen + exactType),
            wires = parseWires(childBody)
          )
        }
    }
  }

  private def extractModuleBody(text: String, exactName: String): Option[Seq[String]] = {
    val lines = text.split("\n", -1)
    val headerIdx = lines.indexWhere(l => ModuleHeaderRe.findFirstMatchIn(l).exists(_.group(1) == exactName))
    if (headerIdx < 0) None
    else {
      val headerIndent = lines(headerIdx).takeWhile(_ == ' ').length
      val body = lines.drop(headerIdx + 1).takeWhile { l =>
        l.trim.isEmpty || l.takeWhile(_ == ' ').length > headerIndent
      }
      Some(body.toSeq)
    }
  }

  private def parseChildRefs(body: Seq[String]): Seq[(String, String)] =
    body.flatMap(l => InstRe.findFirstMatchIn(l).map(m => (m.group(1), m.group(2))))

  private def parseWires(body: Seq[String]): Seq[HWire] =
    body.flatMap { l =>
      ConnectRe.findFirstMatchIn(l).flatMap { m =>
        for {
          to <- normalizeRef(m.group(1))
          from <- normalizeRef(m.group(2))
        } yield HWire(from, to)
      }
    }

  private def isBoilerplate(s: String): Boolean = {
    val last = s.split('.').last
    last == "clock" || last == "reset"
  }

  private def normalizeRef(raw: String): Option[HRef] = {
    val s = raw.trim
    if (isBoilerplate(s) || !CleanRefRe.matches(s)) None
    else {
      val bracketed = s.replaceAll("""\[(\d+)\]""", "_$1")
      bracketed.split("\\.").toList match {
        case "io" :: rest if rest.nonEmpty          => Some(HRef("$self", "io_" + rest.mkString("_")))
        case inst :: "io" :: rest if rest.nonEmpty   => Some(HRef(inst, "io_" + rest.mkString("_")))
        case _                                       => None
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
    var depth = 0
    var start = 0
    for (i <- s.indices) {
      s(i) match {
        case '{' | '<' | '[' => depth += 1
        case '}' | '>' | ']' => depth -= 1
        case ',' if depth == 0 =>
          out += s.substring(start, i)
          start = i + 1
        case _ =>
      }
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
    if (typeText.startsWith("{")) return Nil // nested bundle fields: not supported, skip rather than crash
    val vecCount = """\[(\d+)\]\s*$""".r.findFirstMatchIn(typeText).map(_.group(1).toInt)
    val width = """<(\d+)>""".r.findFirstMatchIn(typeText).map(_.group(1).toInt).getOrElse(1)
    val dir = if (flip) "input" else "output"
    // Prefixed to match both normalizeRef's wire-endpoint naming and the
    // top-level DataMirror-based flattening in ModuleRegistry.flattenPorts.
    vecCount match {
      case Some(n) => (0 until n).map(i => HPort(s"io_${name}_$i", dir, width))
      case None    => Seq(HPort(s"io_$name", dir, width))
    }
  }

  private def displayName(exact: String): String = exact.replaceAll("_\\d+$", "")

  // -- JSON --------------------------------------------------------------

  private def esc(s: String): String = s.replace("\\", "\\\\").replace("\"", "\\\"")
  private def portJson(p: HPort): String = s"""{"name":"${esc(p.name)}","dir":"${p.dir}","width":${p.width}}"""
  private def refJson(r: HRef): String = s"""{"ref":"${esc(r.ref)}","port":"${esc(r.port)}"}"""
  private def wireJson(w: HWire): String = s"""{"from":${refJson(w.from)},"to":${refJson(w.to)}}"""
  private def childJson(c: HChild): String =
    s"""{"instName":"${esc(c.instName)}","moduleType":"${esc(c.moduleType)}",""" +
      s""""ports":[${c.ports.map(portJson).mkString(",")}],""" +
      s""""children":[${c.children.map(childJson).mkString(",")}],""" +
      s""""wires":[${c.wires.map(wireJson).mkString(",")}]}"""

  def toJson(h: Hierarchy): String =
    s"""{"children":[${h.children.map(childJson).mkString(",")}],"wires":[${h.wires.map(wireJson).mkString(",")}]}"""
}
