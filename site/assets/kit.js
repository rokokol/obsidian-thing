// SPDX-License-Identifier: MIT
// The behaviour the vendored DDLC kit brings, wired to this site: the pointer cloud behind
// the page, the index lattice at the top of the hero, the neighbourhood pop-up and the theme
// button. Every canvas reads its colours from the --ddlc-* roles through the kit's color()
// probe, so a theme change recolours all of them. Without this module the pages still read
// and a stored theme still applies; only the canvases stay empty

import { initTheme } from "./vendor/ddlc/ddlc-theme.js"
import { startCloud, color, bayerThresholds } from "./vendor/ddlc/ddlc-cloud.js"

const THEME_KEY = "obsidian-thing-theme"
const STILL = matchMedia("(prefers-reduced-motion: reduce)")
const BAYER = bayerThresholds(8)

function role(name) {
  return color(name)
}

// ---------- the pointer cloud ----------

const cloudCanvas = document.querySelector(".cloud")
const cloud = cloudCanvas ? startCloud(cloudCanvas, { pitch: 6, radius: 170 }) : null

// ---------- the index lattice ----------

// The vault seen as an index: a grid of cells, most of them dark, with light travelling
// through it. Three sources drift on slow sine paths and one band sweeps across, so the
// picture reads as a scan that never finishes. The dithering phase steps a few times a
// second, which is what makes the light move without an element moving. Under reduced motion
// one frame is drawn and kept

const lattice = (() => {
  const canvas = document.querySelector(".lattice")
  if (!canvas) return { recolor() {}, start() {}, stop() {} }

  const ctx = canvas.getContext("2d")
  const PITCH = 6
  const SOURCES = [
    { x: 0.28, y: 0.34, drift: 0.05, speed: 0.11 },
    { x: 0.62, y: 0.62, drift: 0.04, speed: 0.09 },
    { x: 0.86, y: 0.26, drift: 0.05, speed: 0.13 },
  ]
  let frame = 0
  let started = 0
  let size = { w: 0, h: 0 }
  let fills = []

  function read() {
    fills = [role("--ddlc-accent"), role("--ddlc-accent-live"), role("--ddlc-grid")]
  }

  function paint(at) {
    const dpr = Math.min(devicePixelRatio || 1, 2)
    const w = canvas.clientWidth
    const h = canvas.clientHeight
    if (!w || !h) return
    if (size.w !== w || size.h !== h) {
      size = { w, h }
      canvas.width = Math.round(w * dpr)
      canvas.height = Math.round(h * dpr)
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    ctx.clearRect(0, 0, w, h)

    const k = at / 1000
    const phase = Math.floor(k * 1.6) & 7
    const columns = Math.ceil(w / PITCH)
    const rows = Math.ceil(h / PITCH)
    // Light is independent of the canvas size, so a resized window keeps the same picture
    const scale = Math.min(w, h) || 1
    const sweep = ((k * 0.12) % 1) * 1.6 - 0.3

    const centres = SOURCES.map((source, index) => ({
      x: w * (source.x + source.drift * Math.sin(k * source.speed + index * 1.7)),
      y: h * (source.y + source.drift * Math.cos(k * source.speed * 0.8 + index * 2.3)),
      radius: scale * (0.16 + 0.03 * Math.sin(k * 0.2 + index)),
    }))

    for (let column = 0; column < columns; column++) {
      for (let row = 0; row < rows; row++) {
        const x = column * PITCH
        const y = row * PITCH
        let value = 0
        for (const centre of centres) {
          const dx = x - centre.x
          const dy = y - centre.y
          value += 0.5 * Math.exp(-(dx * dx + dy * dy) / (2 * centre.radius * centre.radius))
        }
        // One band crosses the grid and lights whatever it passes
        const band = Math.exp(-Math.pow((x / w - sweep) * 6, 2))
        value = value * 0.7 + band * 0.22
        if (value * 1.5 > BAYER[row & 7][(column + phase) & 7]) {
          ctx.fillStyle = value > 0.62 ? fills[1] : value > 0.34 ? fills[0] : fills[2]
          ctx.fillRect(x, y, PITCH - 2, PITCH - 2)
        }
      }
    }
  }

  function loop(now) {
    if (!started) started = now
    paint(now - started)
    frame = requestAnimationFrame(loop)
  }

  function start() {
    if (!fills.length) read()
    if (STILL.matches) {
      paint(0)
      return
    }
    if (frame || document.hidden) return
    frame = requestAnimationFrame(loop)
  }

  function stop() {
    if (frame) cancelAnimationFrame(frame)
    frame = 0
  }

  addEventListener("resize", () => {
    if (STILL.matches) paint(0)
  })
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop()
    else start()
  })

  return {
    recolor() {
      read()
      paint(started ? performance.now() - started : 0)
    },
    start,
    stop,
  }
})()

// ---------- the neighbourhood pop-up ----------

// A drawing of what obsidian.map returns: the source file and its nearest neighbours on two
// axes. The coordinates are fixed here, the points are not measured, and nothing on the page
// contacts the server. The readout repeats the one fact that matters about the map: the rank
// comes from the vectors, never from the drawn position

const minimap = (() => {
  const dialog = document.getElementById("minimap")
  const canvas = document.getElementById("minimap-canvas")
  const trigger = document.querySelector("[data-dialog-open='minimap']")
  if (!dialog || !canvas || !trigger) return { recolor() {} }

  const ctx = canvas.getContext("2d")
  const POINTS = [
    { label: "Notes/Search.md", x: -0.62, y: -0.44, source: true, score: 1 },
    { label: "Notes/Index policy.md", x: -0.28, y: -0.12, score: 0.78 },
    { label: "Notes/Docker.md", x: 0.14, y: -0.36, score: 0.71 },
    { label: "Notes/Embedding costs.md", x: 0.52, y: -0.08, score: 0.66 },
    { label: "Notes/State dir.md", x: 0.36, y: 0.42, score: 0.58 },
    { label: "Notes/Providers.md", x: 0.76, y: 0.28, score: 0.51 },
    { label: "Notes/Plugin.md", x: -0.08, y: 0.5, score: 0.44 },
  ]
  const PAD = 28
  let palette = {}

  function read() {
    palette = {
      ground: role("--ddlc-code-ground"),
      axis: role("--ddlc-grid"),
      tick: role("--ddlc-muted"),
      text: role("--ddlc-text"),
      muted: role("--ddlc-muted"),
      source: role("--ddlc-accent"),
      point: role("--ddlc-accent-live"),
    }
  }

  function paint() {
    const dpr = Math.min(devicePixelRatio || 1, 2)
    const w = canvas.clientWidth
    if (!w) return
    const h = Math.round(Math.min(w * 0.6, 420))
    if (canvas.width !== Math.round(w * dpr)) {
      canvas.width = Math.round(w * dpr)
      canvas.height = Math.round(h * dpr)
      canvas.style.height = h + "px"
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    ctx.clearRect(0, 0, w, h)
    ctx.fillStyle = palette.ground
    ctx.fillRect(0, 0, w, h)

    const left = PAD
    const top = PAD
    const plotW = w - PAD * 2
    const plotH = h - PAD * 2

    // The axes cross at the origin, where a file whose vectors cancel out would sit
    ctx.fillStyle = palette.axis
    ctx.fillRect(left, top, 1, plotH)
    ctx.fillRect(left, top + plotH, plotW, 1)
    ctx.fillRect(left + plotW / 2, top, 1, plotH)
    ctx.fillRect(left, top + plotH / 2, plotW, 1)

    ctx.fillStyle = palette.tick
    ctx.font = '10px "Departure Mono", ui-monospace, monospace'
    ctx.textBaseline = "middle"
    ctx.fillText("−1", left + 2, top + plotH / 2 + 8)
    ctx.fillText("+1", left + plotW - 14, top + plotH / 2 + 8)
    ctx.fillText("v1", left + plotW / 2 + 6, top + plotH - 12)

    const project = (point) => ({
      x: left + plotW / 2 + (point.x * plotW) / 2,
      y: top + plotH / 2 - (point.y * plotH) / 2,
    })

    for (const point of POINTS) {
      const at = project(point)
      const lit = point.source ? palette.source : palette.point
      const cell = point.source ? 8 : 6
      const x = Math.round(at.x) - cell / 2
      const y = Math.round(at.y) - cell / 2
      ctx.fillStyle = lit
      ctx.fillRect(x, y, cell, cell)
      ctx.fillStyle = point.source ? palette.text : palette.muted
      const label = point.source ? point.label + " · source" : point.label
      ctx.fillText(label, Math.round(at.x) + 10, Math.round(at.y))
    }

    const readout = document.getElementById("minimap-readout")
    if (readout) {
      readout.textContent = `${POINTS.length} files · explained 0.34 / 0.21 · rank from the vectors`
    }
  }

  function open() {
    if (!palette.source) read()
    dialog.showModal()
    // A custom face arrives with the stylesheet, so the first frame waits for it
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(paint, paint)
    } else {
      paint()
    }
  }

  trigger.addEventListener("click", open)
  addEventListener("resize", () => {
    if (dialog.open) paint()
  })

  return {
    recolor() {
      read()
      if (dialog.open) paint()
    },
  }
})()

// ---------- the theme button ----------

const themeButton = document.getElementById("theme")
const themeIcon = document.getElementById("theme-icon")

function themeLabel(mode) {
  const ru = document.documentElement.lang === "ru"
  const label = ru ? `Тема: ${mode}` : `Theme: ${mode}`
  themeButton.setAttribute("aria-label", label)
  themeButton.title = label
}

const theme =
  themeButton && themeIcon
    ? initTheme({
        button: themeButton,
        icon: themeIcon,
        key: THEME_KEY,
        onChange: (mode) => {
          if (cloud) cloud.recolor()
          lattice.recolor()
          minimap.recolor()
          themeLabel(mode)
        },
      })
    : null

document.addEventListener("thing:lang", () => {
  if (theme) themeLabel(theme.mode)
})

lattice.start()
