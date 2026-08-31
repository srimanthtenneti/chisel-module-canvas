ThisBuild / scalaVersion := "2.13.18"
ThisBuild / organization := "io.github.srimanthtenneti"
ThisBuild / version := "0.1.0"

lazy val chiselVersion = "7.13.0"

lazy val root = (project in file("."))
  .settings(
    name := "chisel-module-canvas-examples",
    libraryDependencies ++= Seq(
      "org.chipsalliance" %% "chisel" % chiselVersion,
      "org.scalatest" %% "scalatest" % "3.2.19" % Test
    ),
    addCompilerPlugin(
      "org.chipsalliance" % "chisel-plugin" % chiselVersion cross CrossVersion.full
    ),
    scalacOptions ++= Seq(
      "-deprecation",
      "-feature",
      "-unchecked",
      "-language:reflectiveCalls"
    ),
    Test / parallelExecution := false
  )
