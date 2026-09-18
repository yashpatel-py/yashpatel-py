#!/usr/bin/env python3
"""
gen_hero.py — renders assets/hero.svg, the profile README hero banner.

Standard library only. Deterministic: the same inputs always produce the same
bytes, so re-running this never creates a noisy diff.

Design contract (see README.md):
  * viewBox 0 0 1000 200, self-painted opaque panel -> renders identically on
    GitHub light (#ffffff) and GitHub dark (#0d1117). No <picture> pair needed.
  * No external fonts, no <foreignObject>, no <script>, no external images.
    GitHub proxies README images through camo and will not fetch anything.
  * Every colour, size and position is a presentation attribute, never a CSS
    class, so SVG sanitisation cannot flatten the design.
  * Motion is SMIL only, and purely additive. GitHub serves README images
    through camo as <img>, where Chromium applies the SMIL timeline's t=0
    value and never advances it — so every animation's t=0 value (values[0],
    or from=) must equal the element's resting attribute, and NOTHING may
    animate in from a hidden state (opacity 0, zero-width clip). The banner
    must be complete and correct frozen at t=0.
  * Monospace text widths are hand-computed at 0.60 em per character and
    locked with textLength so OS font substitution cannot overflow a column.
  * The market motif is decorative texture, not data: a driftless random walk.
    It is never a performance claim about any real strategy.

Usage
-----
    python3 scripts/gen_hero.py
    python3 scripts/gen_hero.py --out-dir /tmp/preview
"""

from __future__ import annotations

import argparse
import random
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# --------------------------------------------------------------------------
# Canvas
# --------------------------------------------------------------------------
W, H = 1000, 200

# Palette. Mid-tone accents only: nothing is pure black or pure white, so the
# panel reads correctly whether it sits on #ffffff or on #0d1117.
GROUND = "#0B0F16"  # panel fill
EDGE = "#1E2A38"  # panel border + hairlines
GRID = "#151F2B"  # chart grid
INK = "#E9EFF5"  # primary text
MUTE = "#9BA8B8"  # secondary text
DIM = "#7E8C9C"  # tertiary text (>= 4.5:1 on GROUND)
GREEN = "#2EE6A8"  # signal accent: up candles, status, tick marker
SLATE = "#4B6B87"  # down candles (cool slate, not alarm red)
AMBER = "#F5B544"  # second accent: the moving-average line + name rule

MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"

# Layout
PAD_L = 36
DIVIDER_X = 530
CX0, CX1 = 560, 912  # candle plot area
CY0, CY1 = 34, 144
CHART_L, CHART_R = 548, 964  # chart band (grid + scanline) extents
AXIS_X = 954  # right-hand price axis, where the "last print" guide lands

N_BARS = 28
MA_WINDOW = 5
# Chosen for the SHAPE of the walk only. The candidate seeds were scored for
# directionlessness (net change ~= 0 over the window, and the moving average
# changing direction repeatedly); 221 is the flattest, choppiest of them.
SEED = 221

# Copy. Every line below is drawn from the verified fact sheet.
EYEBROW = "SOFTWARE ENGINEER · DISTRIBUTED SYSTEMS · CONCORD, NC"
NAME = "YASH PATEL"
ROLE = "Python-first · 4+ years · MS CS, Illinois Tech"
PILL = "OPEN TO SWE / ML / DATA ROLES"
CAPTION = "BACKTEST · WEBSOCKET FEEDS · BROKER APIS"

ALT = (
    "Yash Patel — software engineer, distributed systems, Concord NC. "
    "Python-first, 4+ years, MS Computer Science, Illinois Institute of Technology. "
    "Open to software engineering, ML engineering and data engineering roles."
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def mono_w(text: str, size: float, tracking: float = 0.0) -> float:
    """Advance width of a monospace run. 0.60 em/char covers SF Mono (0.600),
    Menlo (0.602), DejaVu Sans Mono (0.602); Consolas (0.550) is narrower and
    gets stretched to fit by textLength, which is the safe direction."""
    n = len(text)
    if n == 0:
        return 0.0
    return n * size * 0.60 + (n - 1) * tracking


def f(x: float) -> str:
    """Compact fixed-precision number — keeps diffs stable and the file small."""
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text_el(
    x: float,
    y: float,
    content: str,
    size: float,
    fill: str,
    weight: str = "400",
    tracking: float = 0.0,
    anchor: str = "start",
    lock: bool = True,
    opacity: float | None = None,
) -> str:
    """One <text> run. Width is locked with textLength unless told otherwise."""
    attrs = [
        f'x="{f(x)}"',
        f'y="{f(y)}"',
        f'font-family="{MONO}"',
        f'font-size="{f(size)}"',
        f'font-weight="{weight}"',
        f'fill="{fill}"',
    ]
    if tracking:
        attrs.append(f'letter-spacing="{f(tracking)}"')
    if anchor != "start":
        attrs.append(f'text-anchor="{anchor}"')
    if opacity is not None:
        attrs.append(f'opacity="{f(opacity)}"')
    if lock:
        attrs.append(f'textLength="{f(mono_w(content, size, tracking))}"')
        attrs.append('lengthAdjust="spacingAndGlyphs"')
    return f"<text {' '.join(attrs)}>{esc(content)}</text>"


# --------------------------------------------------------------------------
# Market motif: a seeded, DRIFTLESS OHLC random walk.
#
# This is texture, not a chart, and it sits next to trading vocabulary — so it
# must not read as an equity curve. The step distribution is zero-mean
# (no drift term), which makes the walk directionless: it wanders up and down
# and ends roughly where it started. Nothing here is a performance claim, and
# no number in this file comes from, or refers to, any real strategy.
# --------------------------------------------------------------------------
def build_series(n: int, seed: int) -> list[tuple[float, float, float, float]]:
    rnd = random.Random(seed)
    price = 100.0
    bars: list[tuple[float, float, float, float]] = []
    for _ in range(n):
        step = rnd.gauss(0.0, 1.85)  # zero mean: no trend, in either direction
        o = price
        c = price + step
        hi = max(o, c) + abs(rnd.gauss(0, 0.9))
        lo = min(o, c) - abs(rnd.gauss(0, 0.9))
        bars.append((o, hi, lo, c))
        price = c
    return bars


def smooth_path(points: list[tuple[float, float]]) -> str:
    """Catmull-Rom -> cubic bezier. Reads as a moving average, not a zigzag."""
    if len(points) < 2:
        return ""
    d = [f"M{f(points[0][0])} {f(points[0][1])}"]
    for i in range(len(points) - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1, p2 = points[i], points[i + 1]
        p3 = points[i + 2] if i + 2 < len(points) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6.0, p1[1] + (p2[1] - p0[1]) / 6.0)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6.0, p2[1] - (p3[1] - p1[1]) / 6.0)
        d.append(
            f"C{f(c1[0])} {f(c1[1])} {f(c2[0])} {f(c2[1])} {f(p2[0])} {f(p2[1])}"
        )
    return "".join(d)


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------
def render() -> str:
    bars = build_series(N_BARS, SEED)
    lo = min(b[2] for b in bars)
    hi = max(b[1] for b in bars)
    span = (hi - lo) or 1.0
    pad = span * 0.10
    lo, hi = lo - pad, hi + pad
    span = hi - lo

    def price_y(p: float) -> float:
        return CY1 - (p - lo) / span * (CY1 - CY0)

    slot = (CX1 - CX0) / N_BARS
    body_w = slot * 0.58

    # --- candles -----------------------------------------------------------
    candles: list[str] = []
    for i, (o, h_, l_, c) in enumerate(bars):
        cx = CX0 + slot * (i + 0.5)
        up = c >= o
        colour = GREEN if up else SLATE
        y_top, y_bot = price_y(max(o, c)), price_y(min(o, c))
        body_h = max(y_bot - y_top, 1.6)
        candles.append(
            f'<rect x="{f(cx - 0.5)}" y="{f(price_y(h_))}" width="1" '
            f'height="{f(price_y(l_) - price_y(h_))}" fill="{colour}" '
            f'opacity="{0.62 if up else 0.55}"/>'
        )
        candles.append(
            f'<rect x="{f(cx - body_w / 2)}" y="{f(y_top)}" width="{f(body_w)}" '
            f'height="{f(body_h)}" rx="0.8" fill="{colour}" '
            f'opacity="{0.95 if up else 0.8}"/>'
        )

    # --- moving average ----------------------------------------------------
    closes = [b[3] for b in bars]
    ma_pts: list[tuple[float, float]] = []
    for i in range(MA_WINDOW - 1, N_BARS):
        window = closes[i - MA_WINDOW + 1 : i + 1]
        ma_pts.append((CX0 + slot * (i + 0.5), price_y(sum(window) / MA_WINDOW)))
    ma_d = smooth_path(ma_pts)
    end_x, end_y = ma_pts[-1]
    area_d = (
        f"{ma_d}L{f(ma_pts[-1][0])} {f(CY1 + 6)}"
        f"L{f(ma_pts[0][0])} {f(CY1 + 6)}Z"
    )

    # --- chart grid --------------------------------------------------------
    grid: list[str] = []
    for i in range(1, 4):
        gy = CY0 + (CY1 - CY0) * i / 4
        grid.append(
            f'<path d="M{CHART_L} {f(gy)}H{CHART_R}" stroke="{GRID}" '
            f'stroke-width="1" stroke-dasharray="2 5"/>'
        )
    for i in range(0, 6):
        gx = CHART_L + (CHART_R - CHART_L) * i / 5
        grid.append(
            f'<path d="M{f(gx)} {CY0 - 4}V{CY1 + 8}" stroke="{GRID}" '
            f'stroke-width="1" opacity="0.55"/>'
        )

    # --- status pill -------------------------------------------------------
    pill_x, pill_y, pill_h = PAD_L, 148, 26
    pill_text_size, pill_track = 13, 1.1
    pill_text_w = mono_w(PILL, pill_text_size, pill_track)
    pill_w = 18 + 7 + 10 + pill_text_w + 16

    # --- assemble ----------------------------------------------------------
    parts: list[str] = []
    A = parts.append

    A(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
        f'aria-labelledby="heroTitle heroDesc">'
    )
    A(f"<title id=\"heroTitle\">{esc(NAME.title())} — {esc(ROLE)}</title>")
    A(f'<desc id="heroDesc">{esc(ALT)}</desc>')

    A("<defs>")
    A(
        f'<linearGradient id="maFill" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{AMBER}" stop-opacity="0.17"/>'
        f'<stop offset="1" stop-color="{AMBER}" stop-opacity="0"/>'
        f"</linearGradient>"
    )
    A(
        f'<linearGradient id="sweep" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{GREEN}" stop-opacity="0"/>'
        f'<stop offset="0.5" stop-color="{GREEN}" stop-opacity="0.13"/>'
        f'<stop offset="1" stop-color="{GREEN}" stop-opacity="0"/>'
        f"</linearGradient>"
    )
    # Soft edges on the area fill, so it reads as shading rather than a block
    # with a hard vertical wall where the series happens to stop.
    A(
        f'<linearGradient id="fade" gradientUnits="userSpaceOnUse" '
        f'x1="{f(ma_pts[0][0])}" y1="0" x2="{f(end_x)}" y2="0">'
        f'<stop offset="0" stop-color="#000000"/>'
        f'<stop offset="0.10" stop-color="#ffffff"/>'
        f'<stop offset="0.86" stop-color="#ffffff"/>'
        f'<stop offset="1" stop-color="#000000"/>'
        f"</linearGradient>"
    )
    A(
        f'<mask id="areaFade" maskUnits="userSpaceOnUse" x="{CHART_L}" '
        f'y="{CY0 - 10}" width="{CHART_R - CHART_L}" '
        f'height="{f(CY1 - CY0 + 20)}">'
        f'<rect x="{CHART_L}" y="{CY0 - 10}" width="{CHART_R - CHART_L}" '
        f'height="{f(CY1 - CY0 + 20)}" fill="url(#fade)"/>'
        f"</mask>"
    )
    A(
        f'<clipPath id="chartClip">'
        f'<rect x="{CHART_L}" y="{CY0 - 6}" width="{CHART_R - CHART_L}" '
        f'height="{f(CY1 - CY0 + 14)}"/>'
        f"</clipPath>"
    )
    A("</defs>")

    # Opaque self-painted panel: the whole light/dark strategy in one rect.
    A(
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="14" '
        f'fill="{GROUND}" stroke="{EDGE}" stroke-width="1"/>'
    )

    # Chart band
    A(f'<g clip-path="url(#chartClip)">')
    A("".join(grid))
    A(f'<path d="{area_d}" fill="url(#maFill)" mask="url(#areaFade)"/>')
    A("".join(candles))
    A(
        f'<path d="{ma_d}" fill="none" stroke="{AMBER}" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round" opacity="0.92"/>'
    )
    # Right-hand price axis + "last print" guide: gives the series a terminus
    # and the eye a stop, instead of the chart looking cropped at the edge.
    A(
        f'<path d="M{AXIS_X} {CY0 - 2}V{CY1 + 8}" stroke="{EDGE}" '
        f'stroke-width="1"/>'
    )
    for i in range(1, 4):
        ty = CY0 + (CY1 - CY0) * i / 4
        A(
            f'<path d="M{AXIS_X - 4} {f(ty)}h4" stroke="{EDGE}" '
            f'stroke-width="1"/>'
        )
    A(
        f'<path d="M{f(end_x)} {f(end_y)}H{AXIS_X}" stroke="{GREEN}" '
        f'stroke-width="1" stroke-dasharray="3 4" opacity="0.40"/>'
    )
    A(
        f'<path d="M{AXIS_X - 5} {f(end_y)}h11" stroke="{GREEN}" '
        f'stroke-width="2" stroke-linecap="round" opacity="0.85"/>'
    )
    # "Last print" tick marker, parked at the end of the line. It does NOT
    # travel: GitHub serves README SVGs through camo as <img>, where Chromium
    # applies the SMIL timeline at t=0 and never advances it, so anything whose
    # t=0 state differs from its resting state renders wrong (or, for a marker
    # that faded in from opacity 0, not at all). The only motion here is the
    # halo pulse below, which is purely additive: its t=0 values (r=4,
    # opacity=0.45) are exactly the static attributes, so the frozen frame and
    # the animated frame agree.
    A("<g>")
    A(
        f'<circle cx="{f(end_x)}" cy="{f(end_y)}" r="4" fill="none" '
        f'stroke="{GREEN}" stroke-width="1.2" opacity="0.45">'
        f'<animate attributeName="r" values="4;11;4" dur="3.6s" '
        f'repeatCount="indefinite" calcMode="spline" '
        f'keyTimes="0;0.55;1" keySplines="0.4 0 0.2 1;0.4 0 0.2 1"/>'
        f'<animate attributeName="opacity" values="0.45;0;0.45" dur="3.6s" '
        f'repeatCount="indefinite"/>'
        f"</circle>"
    )
    A(
        f'<circle cx="{f(end_x)}" cy="{f(end_y)}" r="3.4" fill="{GREEN}"/>'
    )
    A("</g>")
    # Scanline sweep. Base x is off the left edge of the clip, so if SMIL is
    # stripped the sweep is simply not visible.
    A(
        f'<rect x="{CHART_L - 150}" y="{CY0 - 6}" width="150" '
        f'height="{f(CY1 - CY0 + 14)}" fill="url(#sweep)">'
        f'<animateTransform attributeName="transform" type="translate" '
        f'from="0 0" to="{CHART_R - CHART_L + 300} 0" dur="11s" '
        f'repeatCount="indefinite"/>'
        f"</rect>"
    )
    A("</g>")

    # Column divider
    A(
        f'<path d="M{DIVIDER_X} 34V166" stroke="{EDGE}" stroke-width="1" '
        f'opacity="0.85"/>'
    )

    # Left column
    A(text_el(PAD_L, 46, EYEBROW, 13, DIM, "500", 1.2))
    A(text_el(PAD_L, 98, NAME, 46, INK, "700", 3.0))
    A(f'<rect x="{PAD_L}" y="109" width="58" height="3" rx="1.5" fill="{AMBER}"/>')
    A(text_el(PAD_L, 134, ROLE, 15, MUTE, "400", 0.5))

    A(
        f'<rect x="{f(pill_x)}" y="{pill_y}" width="{f(pill_w)}" '
        f'height="{pill_h}" rx="13" fill="{GREEN}" fill-opacity="0.07" '
        f'stroke="{GREEN}" stroke-opacity="0.42" stroke-width="1"/>'
    )
    A(
        f'<circle cx="{f(pill_x + 18)}" cy="{f(pill_y + pill_h / 2)}" r="3.5" '
        f'fill="{GREEN}"/>'
    )
    A(
        text_el(
            pill_x + 18 + 3.5 + 10,
            pill_y + pill_h / 2 + 4.6,
            PILL,
            pill_text_size,
            GREEN,
            "500",
            pill_track,
        )
    )

    # Caption under the chart
    # Baseline-aligned with the status pill's text, so the two columns
    # close on the same line.
    A(text_el(CHART_R, 168, CAPTION, 13, DIM, "400", 1.0, anchor="end"))

    A("</svg>")
    return "".join(parts)


def validate(svg: str, label: str) -> None:
    """Same gate as gen_cards.validate(): a render that fails this is never
    allowed to reach assets/, so a bad run can't clobber a good banner."""
    try:
        ET.fromstring(svg)
    except ET.ParseError as exc:
        raise ValueError(f"{label}: generated SVG is not well-formed XML: {exc}") from exc
    if len(svg) < 500:
        raise ValueError(f"{label}: generated SVG is suspiciously small ({len(svg)}B)")
    if len(svg) > 120_000:
        raise ValueError(f"{label}: generated SVG is too large ({len(svg)}B)")
    # Nothing that GitHub's sanitiser strips, and no external fetch of any kind:
    # the only permitted URL in the whole file is the SVG namespace.
    for banned in ("<script", "<foreignObject", "<image", "@import",
                   "<style", 'href="http', "url(http"):
        if banned in svg:
            raise ValueError(f"{label}: contains banned construct {banned!r}")
    if svg.count("http") != svg.count("http://www.w3.org/2000/svg"):
        raise ValueError(f"{label}: contains an unexpected external URL")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the profile hero banner.")
    ap.add_argument("--offline", action="store_true",
                    help="accepted for symmetry with gen_cards.py; no-op, the "
                         "hero is rendered entirely from local constants")
    ap.add_argument("--out-dir", default=None,
                    help="output directory (default: <repo>/assets)")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir) if args.out_dir else \
        Path(__file__).resolve().parents[1] / "assets"
    fname = "hero.svg"

    # Render and validate BEFORE touching the filesystem: the previously
    # committed hero.svg stays the last known-good version if anything fails.
    try:
        svg = render()
        validate(svg, fname)
    except Exception as exc:  # noqa: BLE001 - any failure must be terminal
        sys.stderr.write(f"[gen_hero] FATAL render/validate: "
                         f"{type(exc).__name__}: {exc}\n"
                         "[gen_hero] Nothing was written.\n")
        return 1

    # Atomic publish: write a sibling .tmp, then rename over the target, so a
    # reader never sees a half-written file.
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / (fname + ".tmp")
    tmp.write_text(svg + "\n", encoding="utf-8")
    tmp.replace(out_dir / fname)
    sys.stderr.write(f"[gen_hero] wrote {out_dir / fname} "
                     f"({len(svg)}B, valid XML)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
