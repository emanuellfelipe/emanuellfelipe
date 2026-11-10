#!/usr/bin/env python3
"""Render the animated profile cards from profile.json + data/contributions.json.

    python scripts/render_cards.py             # animated cards -> cards/, refreshes the README blocks
    STATIC=1 python scripts/render_cards.py    # final frame only -> cards-static/ + preview.html of the README

The cards are shown through <img>, so they can't run scripts or load anything.
Each card therefore carries an inline <style> with CSS keyframes, SMIL
<animate> elements, and its own fonts: subset with pyftsubset to exactly the
glyphs that card uses and embedded as base64 WOFF data URIs.

Motion rules
  * Every delay is offset by the card's start time (TIMELINE) so the page plays
    top to bottom: hero, tiles, chart, repos, graph.
  * Keyframes only define a `from` state. The element's own attributes are the
    final frame, so switching animation off always lands on the final frame.
  * SMIL only lives inside <g class="m">. Anything it moves has a static twin in
    <g class="still">.
  * prefers-reduced-motion: reduce -> CSS animations off, .m hidden, .still shown.
  * STATIC=1 applies those same rules unconditionally, so a screenshot of the
    STATIC build is exactly what reduced-motion visitors see.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import math
import os
import re
import sys
import tempfile
from html import escape
from pathlib import Path

from fontTools import subset as pyftsubset  # `pyftsubset` is fontTools.subset.main
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
STATIC = os.environ.get("STATIC") == "1"
OUT = ROOT / os.environ.get("OUT", "cards-static" if STATIC else "cards")
PROFILE = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
DATA_FILE = ROOT / "data" / "contributions.json"

# ── Theme ────────────────────────────────────────────────────────────────────
# Flat neutrals and one accent. The accent only marks data: graph cells, the
# busiest month and day, the live streak. No gradients anywhere.
PANEL, LINE, CHIP = "#0F1012", "#25272C", "#16171A"
TEXT, SOFT, MUTED, FAINT = "#EDEDEF", "#B9BBC1", "#82858D", "#50535A"
BAR, GHOST = "#2B2D33", "#1A1B1F"
ACCENT = "#8B7CFF"
RAMP = ["#191A1E", "#2D2A4D", "#463F86", "#6659C2", "#8B7CFF"]  # graph levels 0-4, one hue

# Start time (s) of each card on the shared page timeline.
TIMELINE = {"hero": 0.0, "tile": 0.55, "tile_step": 0.15, "chart": 1.15,
            "repo": 1.85, "repo_step": 0.2, "graph_gap": 0.15}

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

MOTION_CSS = (
    "@keyframes rise{from{opacity:0;transform:translateY(14px)}}"
    "@keyframes fade{from{opacity:0}}"
    "@keyframes zoom{from{opacity:0;transform:scale(.9)}}"
    "@keyframes pop{from{opacity:0;transform:scale(.2)}}"
    "@keyframes grow{from{transform:scaleY(0)}}"
    "@keyframes growx{from{transform:scaleX(0)}}"
    "@keyframes draw{from{stroke-dashoffset:1}}"
    "@keyframes slide{from{opacity:0;transform:translateX(-16px)}}"
    "@keyframes nudge{from{opacity:0;transform:translate(-8px,8px)}}"
    ".rise{animation:rise .8s cubic-bezier(.2,.8,.2,1) both}"
    ".fade{animation:fade 1s ease-out both}"
    ".zoom{animation:zoom 1s cubic-bezier(.2,.8,.2,1) both;transform-box:fill-box;transform-origin:center}"
    ".pop{animation:pop .45s cubic-bezier(.3,1.5,.6,1) both;transform-box:fill-box;transform-origin:center}"
    ".grow{animation:grow .9s cubic-bezier(.2,.8,.2,1) both;transform-box:fill-box;transform-origin:50% 100%}"
    ".growc{animation:grow .9s cubic-bezier(.2,.8,.2,1) both;transform-box:fill-box;transform-origin:center}"
    ".growx{animation:growx 1.1s cubic-bezier(.2,.8,.2,1) both;transform-box:fill-box;transform-origin:0 50%}"
    ".draw{animation:draw 1.6s cubic-bezier(.4,0,.2,1) both}"
    ".slide{animation:slide .8s cubic-bezier(.2,.8,.2,1) both}"
    ".nudge{animation:nudge .7s cubic-bezier(.2,.8,.2,1) both}"
    ".still{display:none}"
)
STILL_RULES = "*{animation:none!important}.m{display:none}.still{display:inline}"

log = logging.getLogger("cards")


# ── Fonts ────────────────────────────────────────────────────────────────────
class Face:
    """One font file: measures text and builds a subset @font-face for a card."""

    def __init__(self, path: str, family: str, weight: int) -> None:
        self.path, self.family, self.weight = ROOT / path, family, weight
        font = TTFont(self.path)
        self.upm = font["head"].unitsPerEm
        self.cmap = font.getBestCmap()
        self.adv = {g: m[0] for g, m in font["hmtx"].metrics.items()}
        self._cache: dict[str, str] = {}

    def width(self, s: str, size: float, ls: float = 0.0) -> float:
        units = sum(self.adv[self.cmap[ord(ch)]] if ord(ch) in self.cmap else self.upm * 0.55 for ch in s)
        return units * size / self.upm + ls * len(s)

    def fit(self, s: str, size: float, max_w: float, ls: float = 0.0, floor: float = 9.0) -> float:
        while size > floor and self.width(s, size, ls) > max_w:
            size -= 0.25
        return size

    def wrap(self, s: str, size: float, max_w: float, max_lines: int) -> list[str]:
        lines: list[str] = []
        words = s.split()
        while words and len(lines) < max_lines:
            line = words.pop(0)
            while words and self.width(f"{line} {words[0]}", size) <= max_w:
                line += " " + words.pop(0)
            lines.append(line)
        if words:  # out of lines: ellipsize the last one
            last = lines[-1]
            while last and self.width(last + "…", size) > max_w:
                last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
            lines[-1] = last.rstrip(",;:") + "…"
        return lines

    def css(self, chars: set[str]) -> str:
        missing = sorted(ch for ch in chars if ch.strip() and ord(ch) not in self.cmap)
        if missing:
            log.warning("%s has no glyph for %s", self.path.name, "".join(missing))
        text = "".join(sorted(chars | {" "}))
        if text not in self._cache:
            with tempfile.TemporaryDirectory() as tmp:
                text_file, out = Path(tmp) / "text.txt", Path(tmp) / "subset.woff"
                text_file.write_text(text, encoding="utf-8")
                pyftsubset.main([
                    str(self.path), f"--text-file={text_file}", f"--output-file={out}",
                    "--flavor=woff", "--layout-features=kern", "--no-hinting", "--desubroutinize",
                ])
                self._cache[text] = base64.b64encode(out.read_bytes()).decode("ascii")
        return (f"@font-face{{font-family:{self.family};font-weight:{self.weight};"
                f"src:url(data:font/woff;base64,{self._cache[text]}) format('woff')}}")


FACES = {
    "display": Face("fonts/space-grotesk-latin-700-normal.woff", "SG", 700),
    "body": Face("fonts/space-grotesk-latin-400-normal.woff", "SG", 400),
    "mono": Face("fonts/jetbrains-mono-latin-500-normal.woff", "JB", 500),
}


# ── Card builder ─────────────────────────────────────────────────────────────
class Card:
    def __init__(self, name: str, w: int, h: int, t0: float, title: str) -> None:
        self.name, self.w, self.h, self.t0, self.title = name, w, h, t0, title
        self.parts: list[str] = []
        self.defs: list[str] = []
        self.css: list[str] = []
        self.glyphs: dict[str, set[str]] = {k: set() for k in FACES}
        self._n = 0

    def add(self, *svg: str) -> None:
        self.parts.extend(svg)

    def uid(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def at(self, d: float) -> str:
        """Absolute begin time for something `d` seconds into this card."""
        return f"{self.t0 + d:.2f}s"

    def anim(self, kind: str, d: float, dur: float | None = None) -> str:
        style = f"animation-delay:{self.at(d)}" + (f";animation-duration:{dur}s" if dur else "")
        return f' class="{kind}" style="{style}"'

    def text(self, x: float, y: float, s: str, face: str = "body", size: float = 14, fill: str = TEXT,
             ls: float = 0.0, anchor: str = "start", attrs: str = "") -> str:
        self.glyphs[face].update(s)
        f = FACES[face]
        extra = (f' letter-spacing="{ls}"' if ls else "") + (f' text-anchor="{anchor}"' if anchor != "start" else "")
        return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{f.family}" font-weight="{f.weight}" '
                f'font-size="{size:g}" fill="{fill}"{extra}{attrs}>{escape(s, quote=False)}</text>')

    def svg(self) -> str:
        fonts = "".join(FACES[k].css(chars) for k, chars in self.glyphs.items() if chars)
        reduced = STILL_RULES if STATIC else f"@media (prefers-reduced-motion:reduce){{{STILL_RULES}}}"
        label = escape(self.title)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{label}">'
            f"<title>{label}</title><style>{fonts}{MOTION_CSS}{''.join(self.css)}{reduced}</style>"
            f"<defs>{''.join(self.defs)}</defs>{''.join(self.parts)}</svg>\n"
        )


def panel(c: Card, radius: int = 16) -> None:
    c.add(f'<g{c.anim("fade", 0, 0.6)}><rect x="0.5" y="0.5" width="{c.w - 1}" height="{c.h - 1}" '
          f'rx="{radius}" fill="{PANEL}" stroke="{LINE}"/></g>')


# ── Data ─────────────────────────────────────────────────────────────────────
def fmt_date(d: dt.date) -> str:
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


class Stats:
    def __init__(self, data: dict) -> None:
        self.counts = {dt.date.fromisoformat(d["date"]): d["count"] for d in data.get("days", [])}
        self.levels = {dt.date.fromisoformat(d["date"]): d.get("level", 0) for d in data.get("days", [])}
        self.today = max(self.counts) if self.counts else dt.datetime.now(dt.UTC).date()
        start = self.today - dt.timedelta(days=364)
        year = [(d, c) for d, c in self.counts.items() if d >= start]
        self.total = sum(c for _, c in year)
        self.active = sum(1 for _, c in year if c)
        self.busiest = max(year, key=lambda dc: (dc[1], dc[0]), default=(self.today, 0))
        self.current = self._current_streak()
        self.longest = self._longest_streak()
        self.months = self._months()

    def _current_streak(self) -> int:
        day = self.today
        if not self.counts.get(day):  # today isn't over yet: a streak can still end yesterday
            day -= dt.timedelta(days=1)
        n = 0
        while self.counts.get(day):
            n += 1
            day -= dt.timedelta(days=1)
        return n

    def _longest_streak(self) -> int:
        best = run = 0
        prev = None
        for day in sorted(d for d, c in self.counts.items() if c):
            run = run + 1 if prev and (day - prev).days == 1 else 1
            best, prev = max(best, run), day
        return best

    def _months(self) -> list[tuple[int, int, int]]:
        out = []
        for back in range(11, -1, -1):
            idx = self.today.year * 12 + self.today.month - 1 - back
            y, m = divmod(idx, 12)
            total = sum(c for d, c in self.counts.items() if d.year == y and d.month == m + 1)
            out.append((y, m + 1, total))
        return out


# ── Hero ─────────────────────────────────────────────────────────────────────
def hero_card() -> Card:
    w, h = 840, 300
    name, tagline = PROFILE["name"], PROFILE["tagline"]
    c = Card("hero", w, h, TIMELINE["hero"], f"{name}: {tagline}")
    panel(c, 18)

    portrait = ROOT / PROFILE["portrait"] if PROFILE.get("portrait") else None
    if portrait is not None and not portrait.is_file():
        log.warning("portrait %s not found; rendering the hero without it", portrait)
        portrait = None
    has_portrait = portrait is not None
    left, max_w = 48, (548 if has_portrait else w - 96)

    kicker = PROFILE.get("kicker", "").upper()
    k_size = FACES["mono"].fit(kicker, 12, max_w, ls=2.2)
    # Without stack chips the text block moves down to sit centred beside the portrait.
    dy = 0 if PROFILE.get("stack") else 24
    c.add(f'<g{c.anim("rise", 0.25)}>{c.text(left, 88 + dy, kicker, "mono", k_size, MUTED, ls=2.2)}</g>')

    n_size = FACES["display"].fit(name, 56, max_w)
    c.add(f'<g{c.anim("rise", 0.4)}>{c.text(left, 148 + dy, name, "display", n_size, TEXT, ls=-0.5)}</g>')

    # Tagline: typed out one character at a time (SMIL, discrete), with a caret.
    t_size = FACES["body"].fit(tagline, 18.5, max_w)
    ty = 186 + dy
    tag_svg = c.text(left, ty, tagline, "body", t_size, SOFT)
    widths = [FACES["body"].width(tagline[:i], t_size) for i in range(len(tagline) + 1)]
    clip = c.uid("type")
    begin, dur = 0.75, round(len(tagline) * 0.028, 2)
    c.defs.append(
        f'<clipPath id="{clip}"><rect x="{left - 2}" y="{ty - t_size - 4}" width="0" height="{t_size * 1.7:.1f}">'
        f'<animate attributeName="width" values="{";".join(f"{v + 2:.1f}" for v in widths)}" calcMode="discrete" '
        f'begin="{c.at(begin)}" dur="{dur}s" fill="freeze"/></rect></clipPath>'
    )
    caret_x = ";".join(f"{left + v + 3:.1f}" for v in widths)
    c.add(
        f'<g class="m"><g clip-path="url(#{clip})">{tag_svg}</g>'
        f'<rect x="{left + 3}" y="{ty - t_size + 1:.1f}" width="2" height="{t_size + 3:.1f}" fill="{SOFT}" opacity="0">'
        f'<animate attributeName="opacity" values="0;1" dur=".01s" begin="{c.at(begin - 0.2)}" fill="freeze"/>'
        f'<animate attributeName="x" values="{caret_x}" calcMode="discrete" begin="{c.at(begin)}" dur="{dur}s" fill="freeze"/>'
        f'<animate attributeName="fill-opacity" values="1;0;1" dur="1.1s" begin="{c.at(begin + dur)}" repeatCount="indefinite"/>'
        "</rect></g>"
        f'<g class="still">{tag_svg}</g>'
    )

    # Stack chips.
    x, chip_h, cy = left, 26, 214
    for i, label in enumerate(PROFILE.get("stack", [])):
        cw = FACES["mono"].width(label, 11.5) + 24
        if x + cw > left + max_w:
            break
        c.add(f'<g{c.anim("rise", begin + dur * 0.6 + i * 0.07)}>'
              f'<rect x="{x:.1f}" y="{cy}" width="{cw:.1f}" height="{chip_h}" rx="13" fill="{CHIP}" stroke="{LINE}"/>'
              + c.text(x + cw / 2, cy + 17.5, label, "mono", 11.5, SOFT, anchor="middle") + "</g>")
        x += cw + 8

    if portrait is not None:
        cx, cy, r = 694, 150, 86
        clip = c.uid("face")
        img = base64.b64encode(portrait.read_bytes()).decode("ascii")
        mime = "image/png" if portrait.suffix.lower() == ".png" else "image/jpeg"
        c.defs.append(f'<clipPath id="{clip}"><circle cx="{cx}" cy="{cy}" r="{r}"/></clipPath>')
        c.add(
            f'<circle cx="{cx}" cy="{cy}" r="{r + 26}" fill="none" stroke="{LINE}" stroke-dasharray="2 7"'
            f'{c.anim("zoom", 0.3, 1.2)}/>',
            f'<g{c.anim("zoom", 0.35, 1.1)}><circle cx="{cx}" cy="{cy}" r="{r + 2}" fill="{CHIP}"/>'
            f'<image x="{cx - r}" y="{cy - r}" width="{2 * r}" height="{2 * r}" clip-path="url(#{clip})" '
            f'preserveAspectRatio="xMidYMid slice" xlink:href="data:{mime};base64,{img}"/></g>',
            f'<circle cx="{cx}" cy="{cy}" r="{r + 12}" fill="none" stroke="{MUTED}" stroke-width="1.2" '
            f'stroke-linecap="round" pathLength="1" stroke-dasharray="1" transform="rotate(-90 {cx} {cy})"'
            f'{c.anim("draw", 0.5, 1.8)}/>',
        )
        orbit = r + 26
        dot = f'<circle cx="{cx}" cy="{cy - orbit}" r="3.5" fill="{SOFT}"/>'
        c.add(
            f'<g class="m"><g opacity="0"><animate attributeName="opacity" values="0;1" dur=".6s" begin="{c.at(1.4)}" fill="freeze"/>'
            f'<animateTransform attributeName="transform" type="rotate" from="40 {cx} {cy}" to="400 {cx} {cy}" '
            f'dur="18s" begin="{c.at(1.4)}" repeatCount="indefinite"/>{dot}</g></g>',
            f'<g class="still" transform="rotate(40 {cx} {cy})">{dot}</g>',
        )
    return c


# ── Tiles ────────────────────────────────────────────────────────────────────
ICONS = {
    "linkedin": '<rect x="3" y="3" width="18" height="18" rx="4"/><circle cx="8" cy="8" r=".6" stroke-width="2"/>'
                '<path d="M8 11v6M12 17v-6M12 13.6a2.6 2.6 0 0 1 5.2 0V17"/>',
    "mail": '<rect x="3" y="5.5" width="18" height="13" rx="2.6"/><path d="M3.8 7.2 12 13l8.2-5.8"/>',
    "instagram": '<rect x="3" y="3" width="18" height="18" rx="5.5"/><circle cx="12" cy="12" r="4.1"/>'
                 '<circle cx="17.1" cy="6.9" r=".5" stroke-width="2"/>',
    "github": '<path d="M9 19c-4 1.4-4-2-6-2.5m12 4.5v-3.4a3 3 0 0 0-.8-2.3c2.7-.3 5.6-1.3 5.6-6a4.7 4.7 0 0 0-1.3-3.2 '
              '4.3 4.3 0 0 0-.1-3.2s-1-.3-3.4 1.3a11.6 11.6 0 0 0-6 0C6.6 1.6 5.6 1.9 5.6 1.9a4.3 4.3 0 0 0-.1 3.2A4.7 '
              '4.7 0 0 0 4.2 8.3c0 4.6 2.8 5.7 5.5 6a3 3 0 0 0-.8 2.3V20"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
}


def arrow(cx: float, cy: float, color: str = SOFT) -> str:
    return (f'<circle cx="{cx}" cy="{cy}" r="14" fill="none" stroke="{LINE}"/>'
            f'<path d="M{cx - 4} {cy + 4}l8-8m-6 0h6v6" fill="none" stroke="{color}" stroke-width="1.7" '
            'stroke-linecap="round" stroke-linejoin="round"/>')


def tile_card(tile: dict, i: int) -> Card:
    w, h = 272, 132
    c = Card(f"tile-{tile['id']}", w, h, TIMELINE["tile"] + i * TIMELINE["tile_step"],
             f"{tile['label']}: {tile['sub']}")
    panel(c, 14)
    icon = ICONS.get(tile.get("icon", ""), ICONS["link"])
    c.add(f'<g{c.anim("zoom", 0.05, 0.7)}><rect x="22" y="22" width="40" height="40" rx="11" fill="{CHIP}" stroke="{LINE}"/>'
          f'<g transform="translate(30 30)" fill="none" stroke="{SOFT}" stroke-width="1.7" '
          f'stroke-linecap="round" stroke-linejoin="round">{icon}</g></g>')
    label = tile["label"].upper()
    c.add(f'<g{c.anim("rise", 0.12)}>{c.text(76, 47, label, "mono", FACES["mono"].fit(label, 10.5, 140, 1.6), MUTED, ls=1.6)}</g>')
    c.add(f'<g{c.anim("nudge", 0.3)}>{arrow(w - 36, 42)}</g>')
    title = tile["title"]
    c.add(f'<g{c.anim("rise", 0.18)}>{c.text(22, 94, title, "display", FACES["display"].fit(title, 19, w - 44), TEXT)}</g>')
    sub = tile["sub"]
    c.add(f'<g{c.anim("rise", 0.26)}>{c.text(22, 115, sub, "mono", FACES["mono"].fit(sub, 11.5, w - 44), MUTED)}</g>')
    return c


# ── Contributions chart ──────────────────────────────────────────────────────
def chart_card(s: Stats) -> Card:
    w, h = 840, 372
    c = Card("contributions", w, h, TIMELINE["chart"],
             f"{plural(s.total, 'contribution')} in the last 12 months; current streak {plural(s.current, 'day')}, "
             f"{s.active} active days, busiest day {fmt_date(s.busiest[0])}")
    panel(c)
    left, right = 44, w - 44

    c.add(f'<g{c.anim("rise", 0.1)}>{c.text(left, 52, "CONTRIBUTIONS · LAST 12 MONTHS", "mono", 11, MUTED, ls=2)}</g>')
    big = f"{s.total:,}"
    big_w = FACES["display"].width(big, 42, -0.5)
    c.add(f'<g{c.anim("rise", 0.18)}>{c.text(left, 102, big, "display", 42, TEXT, ls=-0.5)}'
          f'{c.text(left + big_w + 10, 102, "contributions", "body", 16, MUTED)}</g>')

    stats = [
        ("STREAK", f"{s.current}", " day" if s.current == 1 else " days", f"best {s.longest}"),
        ("ACTIVE DAYS", f"{s.active}", "", "of the last 365"),
        ("BUSIEST DAY", f"{s.busiest[1]}", "", fmt_date(s.busiest[0]) if s.busiest[1] else "nothing yet"),
    ]
    for i, (label, value, unit, sub) in enumerate(stats):
        x = 452 + i * 128
        d = 0.25 + i * 0.1
        vw = FACES["display"].width(value, 26)
        c.add(f'<g{c.anim("rise", d)}>'
              + c.text(x, 52, label, "mono", 10, FAINT, ls=1.5)
              + c.text(x, 86, value, "display", 26, TEXT)
              + (c.text(x + vw + 3, 86, unit, "body", 13, MUTED) if unit else "")
              + c.text(x, 106, sub, "mono", 10.5, MUTED) + "</g>")
        if label == "STREAK" and s.current:
            px, py = x - 12, 48.5
            c.add(f'<circle cx="{px}" cy="{py}" r="3" fill="{ACCENT}"{c.anim("zoom", d + 0.2, 0.5)}/>',
                  f'<g class="m"><circle cx="{px}" cy="{py}" r="3" fill="none" stroke="{ACCENT}" opacity="0">'
                  f'<animate attributeName="r" values="3;10" dur="2s" begin="{c.at(1.0)}" repeatCount="indefinite"/>'
                  f'<animate attributeName="opacity" values=".8;0" dur="2s" begin="{c.at(1.0)}" repeatCount="indefinite"/>'
                  "</circle></g>")

    c.add(f'<g{c.anim("growx", 0.3)}><rect x="{left}" y="132" width="{right - left}" height="1" fill="{LINE}"/></g>')

    top, base = 170, 314
    peak = max((m[2] for m in s.months), default=0)
    grid = "".join(
        f'<line x1="{left}" y1="{base - (base - top) * frac:.1f}" x2="{right}" y2="{base - (base - top) * frac:.1f}" '
        f'stroke="{LINE}" stroke-dasharray="3 5"/>' for frac in (0.5, 1.0))
    c.add(f'<g{c.anim("fade", 0.35, 0.8)}>{grid}<line x1="{left}" y1="{base}" x2="{right}" y2="{base}" stroke="{LINE}"/></g>')

    slot = (right - left) / len(s.months)
    bw = min(36.0, slot * 0.56)
    best = max(range(len(s.months)), key=lambda k: s.months[k][2]) if peak else -1
    for i, (year, month, total) in enumerate(s.months):
        cx = left + slot * (i + 0.5)
        bh = max(3.0, (total / peak) * (base - top)) if peak else 3.0
        d = 0.45 + i * 0.06
        fill = ACCENT if i == best else BAR
        c.add(f'<rect x="{cx - bw / 2:.1f}" y="{base - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="6" fill="{fill}"'
              f'{c.anim("grow", d)}/>')
        c.add(f'<g{c.anim("fade", d + 0.55, 0.5)}>'
              + c.text(cx, base - bh - 9, f"{total:,}", "mono", 11, TEXT if i == best else MUTED, anchor="middle")
              + "</g>")
        is_now = i == len(s.months) - 1
        year_tag = (c.text(cx, base + 38, f"'{year % 100:02d}", "mono", 9.5, FAINT, anchor="middle")
                    if month == 1 or i == 0 else "")
        c.add(f'<g{c.anim("fade", d, 0.6)}>'
              + c.text(cx, base + 22, MONTHS[month - 1], "mono", 11, TEXT if is_now else FAINT, anchor="middle")
              + year_tag + "</g>")
    return c


# ── Featured repos ───────────────────────────────────────────────────────────
def star(x: float, y: float, r: float) -> str:
    pts = []
    for k in range(10):
        rad = r if k % 2 == 0 else r * 0.45
        a = -90 + k * 36
        pts.append(f"{x + rad * math.cos(math.radians(a)):.2f},{y + rad * math.sin(math.radians(a)):.2f}")
    return f'<polygon points="{" ".join(pts)}" fill="none" stroke="{MUTED}" stroke-width="1.3" stroke-linejoin="round"/>'


def fork(x: float, y: float) -> str:
    return (f'<g transform="translate({x - 6} {y - 7})" fill="none" stroke="{MUTED}" stroke-width="1.3" stroke-linecap="round">'
            '<circle cx="2.5" cy="2" r="1.7"/><circle cx="9.5" cy="2" r="1.7"/><circle cx="6" cy="12" r="1.7"/>'
            '<path d="M2.5 3.8v1.4a2 2 0 0 0 2 2h3a2 2 0 0 0 2-2V3.8M6 7.2v3"/></g>')


def repo_card(item: dict, meta: dict, i: int, t0: float) -> Card:
    w, h = 840, 164
    full = meta.get("nameWithOwner", item["repo"])
    owner, name = full.split("/", 1)
    desc = item.get("description") or meta.get("description") or "No description yet."
    lang = meta.get("primaryLanguage") or {}
    c = Card(f"repo-{i + 1}", w, h, t0, f"{full}: {desc}")
    panel(c)
    c.add(f'<g{c.anim("fade", 0.3, 1.2)}>{c.text(w - 40, h - 26, f"{i + 1:02d}", "display", 64, GHOST, anchor="end")}</g>')
    c.add(f'<g{c.anim("nudge", 0.35)}>{arrow(w - 54, 46)}</g>')

    c.add(f'<g{c.anim("slide", 0.08)}>{c.text(40, 46, f"{owner} /", "mono", 12, MUTED)}</g>')
    c.add(f'<g{c.anim("slide", 0.14)}>{c.text(40, 77, name, "display", FACES["display"].fit(name, 25, w - 160), TEXT)}</g>')
    lines = FACES["body"].wrap(desc, 14.5, w - 40 - 150, 2)
    c.add(f'<g{c.anim("slide", 0.22)}>'
          + "".join(c.text(40, 104 + k * 20, line, "body", 14.5, SOFT) for k, line in enumerate(lines)) + "</g>")

    y = h - 18 if len(lines) == 2 else h - 30
    x = 40.0
    bits = []
    if lang.get("name"):
        bits.append(f'<circle cx="{x + 5}" cy="{y - 4.5}" r="4.5" fill="none" stroke="{MUTED}" stroke-width="1.3"/>')
        bits.append(c.text(x + 16, y, lang["name"], "body", 13, MUTED))
        x += 16 + FACES["body"].width(lang["name"], 13) + 22
    for kind, count in (("star", meta.get("stargazerCount", 0)), ("fork", meta.get("forkCount", 0))):
        bits.append(star(x + 6, y - 4.5, 6.5) if kind == "star" else fork(x + 6, y - 4.5))
        bits.append(c.text(x + 17, y, f"{count:,}", "body", 13, MUTED))
        x += 17 + FACES["body"].width(f"{count:,}", 13) + 22
    if meta.get("pushedAt"):
        pushed = dt.date.fromisoformat(meta["pushedAt"][:10])
        bits.append(c.text(x, y, f"UPDATED {MONTHS[pushed.month - 1].upper()} {pushed.year}", "mono", 10.5, FAINT, ls=1.2))
        x += FACES["mono"].width(f"UPDATED {MONTHS[pushed.month - 1].upper()} {pushed.year}", 10.5, 1.2) + 18
    if meta.get("isArchived"):
        bits.append(c.text(x, y, "ARCHIVED", "mono", 10.5, SOFT, ls=1.2))
    c.add(f'<g{c.anim("slide", 0.3)}>{"".join(bits)}</g>')
    return c


# ── Contribution graph ───────────────────────────────────────────────────────
def graph_card(s: Stats, t0: float) -> Card:
    w, h = 840, 222
    c = Card("graph", w, h, t0, f"Contribution graph: {plural(s.total, 'contribution')} in the last year")
    panel(c)
    left, right = 44, w - 44
    cell, step = 10.5, 13.5
    cols = 53
    x0 = right - (cols * step - (step - cell))
    top = 82

    def sun_index(d: dt.date) -> int:
        return (d.weekday() + 1) % 7

    start = s.today - dt.timedelta(days=sun_index(s.today) + 7 * (cols - 1))
    c.add(f'<g{c.anim("rise", 0.05)}>{c.text(left, 44, "CONTRIBUTION GRAPH", "mono", 11, MUTED, ls=2)}'
          + c.text(right, 44, f"{plural(s.total, 'contribution')} in the last year", "body", 13, MUTED, anchor="end")
          + "</g>")
    c.add(f'<g{c.anim("fade", 0.1, 0.8)}>' + "".join(
        c.text(left, top + row * step + cell - 1.5, label, "mono", 9.5, FAINT)
        for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri"))) + "</g>")

    last_label_col = -9
    cells = []
    busiest_xy = None
    for col in range(cols):
        week_start = start + dt.timedelta(days=7 * col)
        first = week_start if col == 0 else next(
            (week_start + dt.timedelta(days=k) for k in range(7) if (week_start + dt.timedelta(days=k)).day == 1), None)
        if first and col - last_label_col >= 3 and (col == 0 and first.day <= 7 or col > 0):
            c.add(f'<g{c.anim("fade", 0.15 + col * 0.022, 0.5)}>'
                  + c.text(x0 + col * step, top - 10, MONTHS[first.month - 1], "mono", 9.5, FAINT) + "</g>")
            last_label_col = col
        for row in range(7):
            day = week_start + dt.timedelta(days=row)
            if day > s.today:
                continue
            x, y = x0 + col * step, top + row * step
            level = s.levels.get(day, 0)
            delay = 0.15 + col * 0.022 + row * 0.012
            cells.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell}" height="{cell}" rx="2.5" fill="{RAMP[level]}"'
                         f'{c.anim("pop", delay)}/>')
            if s.busiest[1] and day == s.busiest[0]:
                busiest_xy = (x + cell / 2, y + cell / 2)
    c.add("".join(cells))

    if busiest_xy:  # ring the busiest day; it pulses while motion is on
        bx, by = busiest_xy
        c.add(f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="9" fill="none" stroke="{SOFT}" stroke-width="1.2"'
              f'{c.anim("zoom", 1.6, 0.6)}/>',
              f'<g class="m"><circle cx="{bx:.1f}" cy="{by:.1f}" r="9" fill="none" stroke="{SOFT}" opacity="0">'
              f'<animate attributeName="r" values="9;18" dur="2.2s" begin="{c.at(2.0)}" repeatCount="indefinite"/>'
              f'<animate attributeName="opacity" values=".7;0" dur="2.2s" begin="{c.at(2.0)}" repeatCount="indefinite"/>'
              "</circle></g>")

    ly = h - 22
    legend_x = right - 5 * step - 34
    c.add(f'<g{c.anim("fade", 1.4, 0.6)}>'
          + c.text(legend_x - 8, ly, "Less", "mono", 9.5, FAINT, anchor="end")
          + "".join(f'<rect x="{legend_x + k * step:.1f}" y="{ly - cell + 1:.1f}" width="{cell}" height="{cell}" rx="2.5" fill="{col}"/>'
                    for k, col in enumerate(RAMP))
          + c.text(legend_x + 5 * step + 5, ly, "More", "mono", 9.5, FAINT)
          + (c.text(left, ly, f"Busiest day: {fmt_date(s.busiest[0])} · {plural(s.busiest[1], 'contribution')}",
                    "mono", 10, MUTED) if s.busiest[1] else "")
          + "</g>")
    if s.busiest[1]:
        c.add(f'<circle cx="{left - 10}" cy="{ly - 3.5}" r="3.5" fill="none" stroke="{SOFT}" stroke-width="1.2"'
              f'{c.anim("zoom", 1.4, 0.6)}/>')
    return c


# ── README + preview ─────────────────────────────────────────────────────────
# The README is yours to write. The script only fills blocks marked like this,
# wherever you put them, and leaves everything else alone:
#     <!-- cards:hero:start --> ... <!-- cards:hero:end -->
# Blocks: hero, contact (the tiles), projects (featured repos), activity.
def readme_blocks(prefix: str) -> dict[str, str]:
    p = PROFILE
    tiles = " ".join(
        f'<a href="{escape(t["href"])}"><img src="{prefix}tile-{t["id"]}.svg" width="32.5%" '
        f'alt="{escape(t["label"])}: {escape(t["sub"])}"></a>' for t in p.get("tiles", []))
    projects = "\n".join(
        f'<p align="center"><a href="https://github.com/{escape(item["repo"])}">'
        f'<img src="{prefix}repo-{i + 1}.svg" width="100%" alt="{escape(item["repo"])}"></a></p>'
        for i, item in enumerate(p.get("featured", [])))
    return {
        "hero": f'<p align="center"><img src="{prefix}hero.svg" width="100%" '
                f'alt="{escape(p["name"])}: {escape(p["tagline"])}"></p>',
        "contact": f'<p align="center">\n{tiles}\n</p>',
        "projects": projects,
        "activity": f'<p align="center"><img src="{prefix}contributions.svg" width="100%" alt="Contributions per month"></p>\n'
                    f'<p align="center"><img src="{prefix}graph.svg" width="100%" alt="Contribution graph"></p>',
    }


def fill_blocks(text: str, blocks: dict[str, str]) -> tuple[str, list[str]]:
    filled = []
    for name, html in blocks.items():
        start, end = f"<!-- cards:{name}:start -->", f"<!-- cards:{name}:end -->"
        if start in text and end in text:
            head, rest = text.split(start, 1)
            _, tail = rest.split(end, 1)
            text = f"{head}{start}\n{html}\n{end}{tail}" if html else f"{head}{start}\n{end}{tail}"
            filled.append(name)
    return text, filled


def update_readme() -> None:
    path = ROOT / "README.md"
    if not path.exists():
        log.warning("README.md not found; nothing to update")
        return
    text = path.read_text(encoding="utf-8")
    new, filled = fill_blocks(text, readme_blocks("cards/"))
    if not filled:
        log.warning("README.md has no <!-- cards:NAME:start --> markers; left it alone")
    if new != text:
        path.write_text(new, encoding="utf-8")
        sys.stdout.write(f"updated README.md blocks: {', '.join(filled)}\n")


def markdown_to_html(md: str) -> str:
    """Just enough Markdown for a local look at the README; GitHub renders the real thing."""
    def inline(t: str) -> str:
        t = re.sub(r"\[!\[([^\]]*)\]\(([^)]+)\)\]\(([^)]+)\)", r'<a href="\3"><img src="\2" alt="\1"></a>', t)
        t = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r'<img src="\2" alt="\1">', t)
        t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        return re.sub(r"(?<![*\w])\*(?!\s)(.+?)\*", r"<em>\1</em>", t)
    out, para, lists = [], [], 0
    def flush() -> None:
        nonlocal lists
        if para:
            out.append("<p>" + " ".join(para) + "</p>")
            para.clear()
        out.extend(["</ul>"] * lists)
        lists = 0
    for line in md.splitlines():
        bullet = re.match(r"^(\s*)- (.*)", line)
        if not line.strip():
            flush()
        elif line.lstrip().startswith("<"):
            flush()
            out.append(line)
        elif line.strip() == "---":
            flush()
            out.append("<hr>")
        elif m := re.match(r"^(#{1,6}) (.*)", line):
            flush()
            out.append(f"<h{len(m.group(1))}>{inline(m.group(2))}</h{len(m.group(1))}>")
        elif bullet:
            depth = len(bullet.group(1)) // 2 + 1
            if para:
                out.append("<p>" + " ".join(para) + "</p>")
                para.clear()
            while lists < depth:
                out.append("<ul>")
                lists += 1
            while lists > depth:
                out.append("</ul>")
                lists -= 1
            out.append(f"<li>{inline(bullet.group(2))}</li>")
        else:
            para.append(inline(line))
    flush()
    return "\n".join(out)


def write_preview() -> None:
    readme = ROOT / "README.md"
    md = readme.read_text(encoding="utf-8") if readme.exists() else ""
    md, _ = fill_blocks(md, readme_blocks(""))
    body = markdown_to_html(md.replace('src="cards/', 'src="'))
    html = f"""<!doctype html><meta charset="utf-8"><title>Profile preview</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{margin:0;background:#0d1117;color:#f0f6fc;font:16px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif}}
body.light{{background:#fff;color:#1f2328}} main{{max-width:830px;margin:32px auto;padding:0 16px}}
h1,h2{{border-bottom:1px solid #3d444d;padding-bottom:.3em}} hr{{border:0;height:4px;background:#3d444d;margin:24px 0}}
body.light h1,body.light h2{{border-color:#d1d9e0}} body.light hr{{background:#d1d9e0}}
img{{max-width:100%}} p[align=center]{{text-align:center}} a{{color:#4493f8;text-decoration:none}}
button{{position:fixed;top:12px;right:12px;font:14px system-ui;padding:6px 12px;border-radius:6px;border:1px solid #3d444d;background:#151b23;color:#f0f6fc;cursor:pointer}}
</style>
<button onclick="document.body.classList.toggle('light')">Toggle light / dark</button>
<main>
{body}
</main>
"""
    (OUT / "preview.html").write_text(html, encoding="utf-8")


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    data = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else {"days": [], "repos": {}}
    stats = Stats(data)

    cards = [hero_card()]
    cards += [tile_card(t, i) for i, t in enumerate(PROFILE["tiles"])]
    cards.append(chart_card(stats))
    featured = PROFILE.get("featured", [])
    for i, item in enumerate(featured):
        meta = data.get("repos", {}).get(item["repo"]) or next(
            (v for k, v in data.get("repos", {}).items() if k.lower() == item["repo"].lower()), {})
        cards.append(repo_card(item, meta, i, TIMELINE["repo"] + i * TIMELINE["repo_step"]))
    cards.append(graph_card(stats, TIMELINE["repo"] + len(featured) * TIMELINE["repo_step"] + TIMELINE["graph_gap"]))

    OUT.mkdir(parents=True, exist_ok=True)
    expected = {f"{c.name}.svg" for c in cards}
    for old in OUT.glob("*.svg"):  # drop cards for tiles/repos that were removed from profile.json
        if old.name not in expected:
            old.unlink()
    for c in cards:
        svg = c.svg()
        (OUT / f"{c.name}.svg").write_text(svg, encoding="utf-8")
        sys.stdout.write(f"{OUT.name}/{c.name}.svg  {len(svg.encode()) / 1024:.1f} KB\n")

    if STATIC:
        write_preview()
        sys.stdout.write(f"static build: open {OUT.relative_to(ROOT)}/preview.html to screenshot the final frame\n")
    else:
        update_readme()


if __name__ == "__main__":
    main()
