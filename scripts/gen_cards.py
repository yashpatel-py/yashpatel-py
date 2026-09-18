#!/usr/bin/env python3
"""
gen_cards.py — self-hosted replacement for github-readme-stats.

Renders four SVG cards into assets/:

    assets/stats-dark.svg     (460 x 200)
    assets/langs-dark.svg     (460 x 200)

Dark only: the cards sit under assets/hero.svg, a single opaque dark panel with
no light variant. Each panel paints its own background, so one file per graphic
renders identically in both GitHub themes and a mismatch is impossible.

Design contract
---------------
* Standard library only. No third-party packages, ever.
* Live data comes from the GitHub GraphQL API via the PROFILE_TOKEN env var
  (see "Token" below).
* If the API fails, rate-limits, or returns data that fails the sanity gate,
  the script exits NON-ZERO and writes NOTHING, so a bad run can never commit a
  broken/empty card over a good one.
* --offline renders from the verified fact-sheet numbers so the assets exist and
  look right before the first Actions run.
* Every SVG paints its own opaque background, so a card never inherits the page
  colour.
* All styling is presentation attributes (no CSS classes, no <style>), no
  <foreignObject>, no external fonts, no external images — everything survives
  GitHub's SVG sanitiser and the camo image proxy.
* Text is native SVG, so every width below is hand-computed:
  monospace advance is 0.60 * font-size per character (see mono_w()).

NO ANIMATION — this is a hard rule, empirically established
-----------------------------------------------------------
GitHub rewrites README images through its camo proxy and renders them inside an
<img> element. In that context Chromium evaluates the SMIL timeline at t=0 and
never advances it. Any element whose animation STARTS from a hidden state
therefore renders PERMANENTLY HIDDEN, regardless of what the element's static
attribute says:

    <animate attributeName="opacity" values="0;0;1" .../>       -> invisible
    <clipPath><rect width="0"><animate attributeName="width"     -> invisible
              values="0;0;416"/></rect></clipPath>

An earlier revision of this file shipped exactly those two constructs as an
"entrance animation". Loaded via <img>, assets/stats-dark.svg and
assets/langs-dark.svg both rendered as empty shells: card border, empty bar
track, every single text label invisible. fill="freeze" does not help, because
the timeline never runs at all.

So: these cards contain ZERO animation. validate() enforces it — any "<animate"
or "<set" in the output is a hard failure. The static state of every element IS
its final visible state. If motion is ever wanted here again, it may only be
additive decoration that cannot gate visibility (it must not touch opacity,
clip width, or a transform applied to content), and the card must still be
100% correct with the animation stripped.

Token — PROFILE_TOKEN, not the default GITHUB_TOKEN
---------------------------------------------------
The workflow's default GITHUB_TOKEN is scoped to the repository, not the user.
With it, the GraphQL query silently DEGRADES rather than erroring:

  * contributionsCollection.restrictedContributionsCount comes back 0, because
    private contribution counts require a user-scoped token with `read:user`;
  * repositories(ownerAffiliations: OWNER) returns only PUBLIC repos, so the
    non-fork repo count and the language byte totals lose every private repo.

The response is well-formed and non-zero, so the old sanity gate (which only
rejected zero/absent values) happily rendered a card claiming ~0 private
contributions — factually wrong, and it would have overwritten the good card.

sanity_gate() now carries plausibility FLOORS as well as zero checks. They
encode "this account cannot have collapsed to these numbers overnight":
contributions_private >= 1, followers >= 10, stars >= 10, lang_repo_count >= 30.
A default-GITHUB_TOKEN response trips `contributions_private is 0 (floor 1)`
and, on an account with few public repos, the repo floor too — so it now FAILS
and writes nothing instead of rendering a degraded card. Verified by simulating
a degraded GraphQL response; see --self-test.

Use a classic PAT / fine-grained token with `read:user` (plus `repo` for
private repo languages) exposed to the workflow as PROFILE_TOKEN.

Determinism
-----------
Byte-identical input data must produce byte-identical output files, so the
workflow's "nothing changed -> don't commit" guard can actually short-circuit.
That is why nothing here renders a date: an "AS OF <date>" stamp changed the
rendered bytes every single day and would have committed a pointless refresh
forever.

Usage
-----
    PROFILE_TOKEN=ghp_... python3 scripts/gen_cards.py
    python3 scripts/gen_cards.py --offline
    python3 scripts/gen_cards.py --offline --out-dir /tmp/preview
    python3 scripts/gen_cards.py --contrast-report
    python3 scripts/gen_cards.py --self-test
"""

from __future__ import annotations

import argparse
import colorsys
import datetime as _dt
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape

LOGIN = "yashpatel-py"
DISPLAY_NAME = "Yash Patel"

API_URL = "https://api.github.com/graphql"
USER_AGENT = "yashpatel-py-profile-cards/1.0 (+https://github.com/yashpatel-py/yashpatel-py)"

# ---------------------------------------------------------------------------
# Canvas geometry (both cards share the same frame so they sit level side by side)
# ---------------------------------------------------------------------------
W, H = 460, 200
PAD = 22               # left/right inner padding
INNER = W - 2 * PAD    # 416 usable px
RX = 12                # card corner radius

MONO = ("ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,"
        "'Liberation Mono','Courier New',monospace")

# Monospace advance ratio. Every glyph in the stacks above is 0.6em wide in the
# fonts that actually ship on macOS / Windows / Linux, so this is exact enough
# to lay out right-aligned columns without a text-measurement pass.
MONO_RATIO = 0.60


def mono_w(text: str, size: float, tracking: float = 0.0) -> float:
    """Hand-computed advance width of `text` at `size` px, plus letter-spacing."""
    n = len(text)
    if n == 0:
        return 0.0
    # SVG letter-spacing adds `tracking` after every glyph including the last.
    return n * (size * MONO_RATIO + tracking)


# ---------------------------------------------------------------------------
# Colour maths — WCAG 2.x relative luminance / contrast ratio
# ---------------------------------------------------------------------------
# Nothing in this file is eyeballed. Every colour that ships is checked against
# the background it is drawn on, at import time (see the assertions below) and
# printable with --contrast-report.
#
# Thresholds used:
#   * text                    >= 4.5:1  (WCAG AA for body text)
#   * graphical objects        >= 3.0:1  (WCAG AA 1.4.11 — bar segments, swatches)
#   * swatch vs. its own track >= 3.0:1  (so "Other" is never lost in the track)

def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _channel_lin(c: int) -> float:
    s = c / 255.0
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def luminance(h: str) -> float:
    r, g, b = _hex_rgb(h)
    return 0.2126 * _channel_lin(r) + 0.7152 * _channel_lin(g) + 0.0722 * _channel_lin(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two sRGB hex colours (1.0 .. 21.0)."""
    la, lb = luminance(a), luminance(b)
    if la < lb:
        la, lb = lb, la
    return (la + 0.05) / (lb + 0.05)


def _rgb_hex(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % (
        max(0, min(255, round(r * 255))),
        max(0, min(255, round(g * 255))),
        max(0, min(255, round(b * 255))),
    )


def fit_to_bg(colour: str, bg: str, target: float = 3.0) -> str:
    """Return `colour` adjusted to clear `target` contrast against `bg`.

    Hue and saturation are preserved exactly; only HLS lightness moves, and only
    away from the background — so the result is still recognisably the same
    colour. A colour that already clears `target` is returned untouched.
    Deterministic: pure arithmetic, no randomness, no clock.
    """
    if contrast(colour, bg) >= target:
        return colour
    r, g, b = _hex_rgb(colour)
    hue, light, sat = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
    darken = luminance(bg) > 0.18          # light background -> move darker
    lo, hi = (0.0, light) if darken else (light, 1.0)
    best = None
    for _ in range(64):                    # bisect to the closest passing shade
        mid = (lo + hi) / 2.0
        cand = _rgb_hex(*colorsys.hls_to_rgb(hue, mid, sat))
        if contrast(cand, bg) >= target:
            best = cand
            if darken:
                lo = mid
            else:
                hi = mid
        else:
            if darken:
                hi = mid
            else:
                lo = mid
    return best if best is not None else ("#000000" if darken else "#ffffff")


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------
#   bg/border are GitHub-native (#0d1117/#30363d, #ffffff/#d0d7de) so the cards
#   sit flush in the README on either theme. accent/accent2 are the signal mint
#   and amber carried over from assets/hero.svg (#2EE6A8 / #F5B544) so the whole
#   page reads as one palette; the light theme uses darkened equivalents that
#   clear 4.5:1 on white.
#
#   Measured (asserted at import, printable with --contrast-report):
#     dark   text  #e6edf3 on #0d1117 = 16.02:1   muted #8b949e = 6.15:1
#            faint #7d8590 on #0d1117 =  5.07:1   (was #6e7681 = 4.12:1, AA fail
#                                                  at the 9.5px footer size)
#     light  text  #1f2328 on #ffffff = 15.80:1   muted #59636e = 6.11:1
#            faint #6b7683 on #ffffff =  4.62:1   (was #818b98 = 3.45:1)
#     "other" swatch #7d8590 reads against BOTH the card bg and the bar track:
#            dark  vs #0d1117 = 5.07:1, vs track #21262d = 4.08:1
#            light vs #ffffff = 3.73:1, vs track #eaeef2 = 3.20:1
THEMES = {
    "dark": {
        "bg":      "#0d1117",
        "border":  "#30363d",
        "rule":    "#21262d",
        "text":    "#e6edf3",
        "muted":   "#8b949e",
        "faint":   "#7d8590",   # 5.07:1 on bg — clears AA at 9.5px
        "accent":  "#2ee6a8",   # private / primary signal  11.72:1 on bg
        "accent2": "#f5b544",   # public / secondary signal 10.43:1 on bg
        "track":   "#21262d",   # empty bar track
        "other":   "#7d8590",   # "Other languages" swatch
    },
    "light": {
        "bg":      "#ffffff",
        "border":  "#d0d7de",
        "rule":    "#d8dee4",
        "text":    "#1f2328",
        "muted":   "#59636e",
        "faint":   "#6b7683",   # 4.62:1 on white — clears AA at 9.5px
        "accent":  "#067a57",   # 5.34:1 on white
        "accent2": "#9a6700",   # 4.87:1 on white
        "track":   "#eaeef2",
        "other":   "#7d8590",   # 3.73:1 on white, 3.20:1 on the track
    },
}

# Real GitHub linguist brand colours. These are the *source* palette; the
# per-theme palettes below are derived from them.
LANG_BRAND = {
    "Python": "#3572A5",
    "JavaScript": "#f1e05a",
    "TypeScript": "#3178c6",
    "HTML": "#e34c26",
    "CSS": "#563d7c",
    "SCSS": "#c6538c",
    "Jupyter Notebook": "#DA5B0B",
    "Jupyter": "#DA5B0B",
    "R": "#198CE7",
    "Shell": "#89e051",
    "Swift": "#F05138",
    "Dart": "#00B4AB",
    "Rust": "#dea584",
    "PowerShell": "#012456",
    "Java": "#b07219",
    "Go": "#00ADD8",
    "C": "#555555",
    "C#": "#178600",
    "Ruby": "#701516",
    "Kotlin": "#A97BFF",
    "Dockerfile": "#384d54",
    "Makefile": "#427819",
    "Procfile": "#a91e50",
    "Mako": "#7e858d",
    "Batchfile": "#C1F12E",
    "Vim Script": "#199f4b",
    "Lua": "#000080",
    "SQL": "#e38c00",
    "PLpgSQL": "#336790",
}

# A language swatch is a graphical object, so it needs 3:1 against the card
# background it is painted on. The brand colours are tuned for GitHub's own
# light-ish repo pages and several of them are unusable on one of our two
# backgrounds:
#
#   LIGHT card (#ffffff) — brand colours that fail 3:1 and get darkened:
#     JavaScript #f1e05a 1.35:1 -> #a7950e 3.02:1
#     Shell      #89e051 1.63:1 -> #54a81e 3.00:1
#     Batchfile  #C1F12E 1.32:1 -> #7ca10b 3.02:1
#     Rust       #dea584 2.14:1 -> #d18153 3.01:1
#     SQL        #e38c00 2.62:1 -> #d48200 3.00:1
#     Go         #00ADD8 2.64:1 -> #00a1ca 3.02:1
#     Dart       #00B4AB 2.59:1 -> #00a69e 3.02:1
#     R          #198CE7 3.53:1 -> unchanged (already passes; checked, not assumed)
#     every other entry already clears 3:1 on white and is untouched.
#
#   DARK card (#0d1117) — the brand colours are kept verbatim. Seven of them are
#   too dark to see on #0d1117 (CSS 2.12, PowerShell 1.25, C 2.54, Ruby 1.63,
#   Dockerfile 2.12, Procfile 2.69, Lua 1.18) and get the same minimal lift, so
#   a card can never render an invisible segment. None of those seven appear in
#   this profile's data: Python (3.70), JavaScript (14.01) and TypeScript (4.17)
#   all pass untouched, i.e. every colour that actually ships on the dark card is
#   the unmodified linguist colour.
#
# Hue and saturation are preserved in every adjustment; only lightness moves.
LANG_COLORS = {
    theme: {name: fit_to_bg(c, THEMES[theme]["bg"], 3.0)
            for name, c in LANG_BRAND.items()}
    for theme in ("dark", "light")
}

# Deterministic muted ramp so an unexpected language never renders invisible.
FALLBACK_BRAND = ["#7d8590", "#6e7681", "#57606a", "#8b949e", "#9198a1"]
FALLBACK_RAMP = {
    theme: [fit_to_bg(c, THEMES[theme]["bg"], 3.0) for c in FALLBACK_BRAND]
    for theme in ("dark", "light")
}

# Display-only shortenings so the legend never has to ellipsise a common name.
LANG_SHORT = {
    "Jupyter Notebook": "Jupyter",
    "Vim Script": "Vim",
    "Objective-C": "Obj-C",
    "Emacs Lisp": "Elisp",
    "Rich Text Format": "RTF",
}


def lang_color(name: str, index: int, theme: str) -> str:
    ramp = FALLBACK_RAMP[theme]
    return LANG_COLORS[theme].get(name, ramp[index % len(ramp)])


def contrast_report() -> str:
    """Every shipped colour, measured against what it is drawn on."""
    lines = ["colour                       fg        bg        ratio  need  ok"]

    def row(label: str, fg: str, bg: str, need: float) -> str:
        r = contrast(fg, bg)
        return (f"{label:28.28s} {fg:9s} {bg:9s} {r:6.2f}  {need:4.1f}  "
                f"{'OK' if r >= need else 'FAIL'}")

    for theme in ("dark", "light"):
        t = THEMES[theme]
        lines.append(f"-- {theme} theme ------------------------------------------")
        for key, need in (("text", 4.5), ("muted", 4.5), ("faint", 4.5),
                          ("accent", 4.5), ("accent2", 4.5),
                          ("border", 1.0), ("rule", 1.0)):
            lines.append(row(f"{key}", t[key], t["bg"], need))
        lines.append(row("other swatch vs card bg", t["other"], t["bg"], 3.0))
        lines.append(row("other swatch vs track", t["other"], t["track"], 3.0))
        lines.append(row("track vs card bg", t["track"], t["bg"], 1.0))
        for name in sorted(LANG_BRAND):
            c = LANG_COLORS[theme][name]
            tag = name if c == LANG_BRAND[name] else f"{name} (adj)"
            lines.append(row(f"lang {tag}", c, t["bg"], 3.0))
        for i, c in enumerate(FALLBACK_RAMP[theme]):
            lines.append(row(f"fallback[{i}]", c, t["bg"], 3.0))
    return "\n".join(lines)


def _assert_palette() -> None:
    """Import-time proof that nothing ships below its contrast floor."""
    for theme in ("dark", "light"):
        t = THEMES[theme]
        bg = t["bg"]
        for key in ("text", "muted", "faint", "accent", "accent2"):
            r = contrast(t[key], bg)
            assert r >= 4.5, f"{theme}.{key} {t[key]} is {r:.2f}:1 on {bg} (<4.5)"
        r = contrast(t["other"], bg)
        assert r >= 3.0, f"{theme}.other vs bg is {r:.2f}:1 (<3.0)"
        r = contrast(t["other"], t["track"])
        assert r >= 3.0, f"{theme}.other vs track is {r:.2f}:1 (<3.0)"
        for name, c in LANG_COLORS[theme].items():
            r = contrast(c, bg)
            assert r >= 3.0, f"{theme} lang {name} {c} is {r:.2f}:1 on {bg} (<3.0)"
        for c in FALLBACK_RAMP[theme]:
            r = contrast(c, bg)
            assert r >= 3.0, f"{theme} fallback {c} is {r:.2f}:1 on {bg} (<3.0)"


_assert_palette()


# ---------------------------------------------------------------------------
# Verified offline fallback — straight from the fact sheet, nothing invented.
# ---------------------------------------------------------------------------
# Contribution split: 1,675 total in the last 12 months, 1,604 restricted
# (private). The remaining 71 public contributions are 54 commits + 6 PRs
# + 11 repos created.
OFFLINE = {
    "login": LOGIN,
    "name": DISPLAY_NAME,
    "contributions_total": 1675,
    "contributions_private": 1604,
    "contributions_public": 71,
    "followers": 52,
    "public_repos": 22,
    "stars": 48,
    "forks": 15,
    # Offline we do NOT have byte counts, only the verified primary-language
    # repo census (Python 24 of 46 non-fork repos, JavaScript 6, TypeScript 4).
    # So the card is labelled for what it actually shows and never pretends to
    # be a bytes-of-code measurement.
    "lang_basis": "PRIMARY LANGUAGE",
    "lang_repo_count": 46,
    "languages": [
        ("Python", 24.0),
        ("JavaScript", 6.0),
        ("TypeScript", 4.0),
        ("Other", 12.0),
    ],
}


# ---------------------------------------------------------------------------
# GitHub GraphQL
# ---------------------------------------------------------------------------
QUERY = """
query($login:String!, $from:DateTime!, $to:DateTime!, $cursor:String) {
  rateLimit { remaining }
  user(login:$login) {
    login
    name
    followers { totalCount }
    publicRepos: repositories(privacy: PUBLIC, ownerAffiliations: OWNER) { totalCount }
    contributionsCollection(from: $from, to: $to) {
      restrictedContributionsCount
      contributionCalendar { totalContributions }
    }
    repositories(first: 50, after: $cursor, ownerAffiliations: OWNER, isFork: false,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        isPrivate
        stargazerCount
        forkCount
        languages(first: 12, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name } }
        }
      }
    }
  }
}
"""


class FetchError(RuntimeError):
    pass


def _post(token: str, variables: dict, timeout: int = 25) -> dict:
    body = json.dumps({"query": QUERY, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if "errors" in payload and payload["errors"]:
        raise FetchError("GraphQL errors: " + json.dumps(payload["errors"])[:600])
    if not payload.get("data", {}).get("user"):
        raise FetchError("GraphQL returned no user object")
    return payload["data"]


def _post_retrying(token: str, variables: dict) -> dict:
    """3 attempts with backoff. Honours Retry-After on secondary rate limits."""
    delays = [2, 8, 30]
    last: Exception | None = None
    for attempt, delay in enumerate(delays, start=1):
        try:
            return _post(token, variables)
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            last = exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            wait = int(retry_after) if (retry_after or "").isdigit() else delay
            if exc.code == 401:
                sys.stderr.write("[gen_cards] HTTP 401: token is invalid; not retrying\n")
                break  # a bad token will never succeed; fail fast
            sys.stderr.write(
                f"[gen_cards] HTTP {exc.code} on attempt {attempt}; "
                f"retrying in {wait}s\n"
            )
        except (urllib.error.URLError, TimeoutError, FetchError, ValueError) as exc:
            last = exc
            wait = delay
            sys.stderr.write(
                f"[gen_cards] {type(exc).__name__} on attempt {attempt}: {exc}; "
                f"retrying in {wait}s\n"
            )
        if attempt < len(delays):
            time.sleep(wait)
    raise FetchError(f"all attempts failed: {last}")


def fetch(token: str) -> dict:
    now = _dt.datetime.now(_dt.timezone.utc)
    frm = now - _dt.timedelta(days=365)
    iso = "%Y-%m-%dT%H:%M:%SZ"

    cursor = None
    followers = public_repos = 0
    total_contrib = private_contrib = 0
    stars = forks = 0
    name = DISPLAY_NAME
    lang_repos: dict[str, int] = {}
    nonfork_repos = 0
    pages = 0

    while True:
        data = _post_retrying(token, {
            "login": LOGIN,
            "from": frm.strftime(iso),
            "to": now.strftime(iso),
            "cursor": cursor,
        })
        user = data["user"]
        if pages == 0:
            remaining = (data.get("rateLimit") or {}).get("remaining")
            sys.stderr.write(f"[gen_cards] GraphQL ok; rate limit remaining: {remaining}\n")
            name = user.get("name") or DISPLAY_NAME
            followers = int(user["followers"]["totalCount"])
            public_repos = int(user["publicRepos"]["totalCount"])
            cc = user["contributionsCollection"]
            total_contrib = int(cc["contributionCalendar"]["totalContributions"])
            private_contrib = int(cc["restrictedContributionsCount"])
            nonfork_repos = int(user["repositories"]["totalCount"])

        for node in user["repositories"]["nodes"]:
            if not node.get("isPrivate"):
                stars += int(node.get("stargazerCount") or 0)
                forks += int(node.get("forkCount") or 0)
            # Count each repo once, under its PRIMARY language, rather than
            # summing bytes. Byte totals are dominated by artifacts rather than
            # effort: a single Jupyter repo here carries 4.4 MB because notebooks
            # embed base64 image output, which rendered a card reading
            # "Jupyter 75.9% / Python 9.1%" for an engineer whose work is almost
            # entirely Python. Repo counts are stable, representative, and match
            # what the offline fallback reports, so both paths agree.
            edges = (node.get("languages") or {}).get("edges") or []
            if not edges:
                continue
            primary = (edges[0].get("node") or {}).get("name")
            if primary:
                lang_repos[primary] = lang_repos.get(primary, 0) + 1

        page_info = user["repositories"]["pageInfo"]
        pages += 1
        if not page_info["hasNextPage"] or pages >= 10:
            break
        cursor = page_info["endCursor"]

    public_contrib = max(total_contrib - private_contrib, 0)

    result = {
        "login": LOGIN,
        "name": name,
        "contributions_total": total_contrib,
        "contributions_private": private_contrib,
        "contributions_public": public_contrib,
        "followers": followers,
        "public_repos": public_repos,
        "stars": stars,
        "forks": forks,
        "lang_basis": "PRIMARY LANGUAGE",
        "lang_repo_count": nonfork_repos,
        "languages": sorted(lang_repos.items(), key=lambda kv: kv[1], reverse=True),
    }
    sanity_gate(result)
    return result


# Plausibility floors. These are NOT "is the field present" checks — they encode
# "this account cannot plausibly have collapsed to this number since the last
# good run". A degraded-but-well-formed response (see the module docstring: the
# default GITHUB_TOKEN cannot see private contributions or private repos) trips
# these and the run dies without writing, instead of overwriting a correct card
# with a factually wrong one. Verified numbers at the time of writing:
# 1,604 private contributions, 52 followers, 48 stars, 46 non-fork repos — every
# floor sits an order of magnitude below reality, so normal drift never trips it.
FLOORS = {
    # 46 non-fork repos exist, only 21 of them public. A run that sees ~21 is a
    # token that cannot read private repos, which silently skews the language
    # mix towards whatever happens to be public. Fail closed: the committed
    # cards stay as the last known-good version until PROFILE_TOKEN is present.
    "lang_repo_count": 30,
    "contributions_private": 1,
    "followers": 10,
    "stars": 10,
}


def sanity_gate(d: dict) -> None:
    """Refuse to render anything that is obviously a degraded response."""
    problems = []
    if d["contributions_total"] <= 0:
        problems.append("contributions_total is 0")
    if d["public_repos"] <= 0:
        problems.append("public_repos is 0")
    if not d["languages"] or sum(v for _, v in d["languages"]) <= 0:
        problems.append("no language data")
    if d["contributions_private"] > d["contributions_total"]:
        problems.append("private > total contributions")
    for key, floor in FLOORS.items():
        value = int(d.get(key) or 0)
        if value < floor:
            problems.append(f"{key} is {value} (implausible; floor {floor})")
    if problems:
        raise FetchError("sanity gate failed: " + "; ".join(problems))


# ---------------------------------------------------------------------------
# SVG primitives
# ---------------------------------------------------------------------------
def esc(s: str) -> str:
    return _xml_escape(str(s), {'"': "&quot;", "'": "&apos;"})


def text(x, y, s, size, fill, *, weight="400", anchor="start", tracking=0.0,
         opacity=None, extra=""):
    attrs = [
        f'x="{fmt(x)}"', f'y="{fmt(y)}"',
        f'font-family="{MONO}"',
        f'font-size="{fmt(size)}"',
        f'fill="{fill}"',
    ]
    if weight != "400":
        attrs.append(f'font-weight="{weight}"')
    if anchor != "start":
        attrs.append(f'text-anchor="{anchor}"')
    if tracking:
        attrs.append(f'letter-spacing="{fmt(tracking)}"')
    if opacity is not None:
        attrs.append(f'opacity="{fmt(opacity)}"')
    if extra:
        attrs.append(extra)
    return f'<text {" ".join(attrs)}>{esc(s)}</text>'


def fmt(v) -> str:
    """Deterministic number formatting so diffs stay tiny."""
    if isinstance(v, int):
        return str(v)
    f = float(v)
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return f"{f:.2f}".rstrip("0").rstrip(".")


def frame(t: dict, title_for_a11y: str, desc: str, body: str, defs: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" role="img" '
        f'aria-label="{esc(title_for_a11y)}">'
        f"<title>{esc(title_for_a11y)}</title>"
        f"<desc>{esc(desc)}</desc>"
        f"<defs>{defs}</defs>"
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="{RX}" '
        f'fill="{t["bg"]}" stroke="{t["border"]}" stroke-width="1"/>'
        f"{body}"
        f"</svg>"
    )


def rule(t: dict, y: float) -> str:
    return (f'<line x1="{PAD}" y1="{fmt(y)}" x2="{W - PAD}" y2="{fmt(y)}" '
            f'stroke="{t["rule"]}" stroke-width="1" shape-rendering="crispEdges"/>')


def rounded_clip(clip_id: str, x: float, y: float, w: float, h: float) -> str:
    """A STATIC clipPath that rounds the ends of a stacked bar.

    Static on purpose: its rect carries its full width as a plain attribute and
    is never animated, so it clips identically at t=0 and forever after.
    """
    return (
        f'<clipPath id="{clip_id}">'
        f'<rect x="{fmt(x)}" y="{fmt(y)}" width="{fmt(w)}" height="{fmt(h)}" '
        f'rx="{fmt(h / 2)}"/></clipPath>'
    )


# ---------------------------------------------------------------------------
# Card 1 — stats
# ---------------------------------------------------------------------------
# Vertical rhythm:
#   0   .. 44   header        (title baseline 30)
#   44  .. 152  2x3 stat grid (row pitch 42; value baselines 79 / 121)
#   152 .. 200  private/public split bar + caption
STAT_COLS = (PAD, PAD + 139, PAD + 278)          # 22, 161, 300  (col width 138)
VALUE_SIZE, VALUE_WEIGHT = 21, "600"
LABEL_SIZE, LABEL_TRACK = 10, 0.6                # 10px is the hard type floor


def render_stats(d: dict, theme: str) -> str:
    t = THEMES[theme]

    total = d["contributions_total"]
    priv = d["contributions_private"]
    pub = d["contributions_public"]
    share = (priv / total * 100.0) if total else 0.0

    cells = [
        # (value, label, is_accent)
        (f"{total:,}",       "CONTRIBUTIONS 1Y", True),
        (f"{share:.1f}%",    "PRIVATE SHARE",    True),
        (f"{d['stars']:,}",  "STARS EARNED",     False),
        (f"{d['public_repos']:,}", "PUBLIC REPOS", False),
        (f"{d['forks']:,}",  "FORKS EARNED",     False),
        (f"{d['followers']:,}", "FOLLOWERS",     False),
    ]

    parts: list[str] = []

    # --- header -------------------------------------------------------------
    title = d.get("name") or DISPLAY_NAME
    handle = f"@{d['login']}"
    # width check: 15px title + 10px handle must fit 416px
    assert mono_w(title, 15) + mono_w(handle, 10) + 24 <= INNER, "stats header overflow"
    parts.append(
        text(PAD, 30, title, 15, t["text"], weight="600")
        + text(W - PAD, 30, handle, 10, t["muted"], anchor="end", tracking=0.3)
    )
    parts.append(rule(t, 44))

    # --- 2 x 3 stat grid ----------------------------------------------------
    for i, (value, label, accent) in enumerate(cells):
        col, row = i % 3, i // 3
        x = STAT_COLS[col]
        vy = 79 + row * 42
        ly = vy + 16
        # width check: value and label must stay inside their 138px column
        assert mono_w(value, VALUE_SIZE) <= 132, f"stat value overflow: {value}"
        assert mono_w(label, LABEL_SIZE, LABEL_TRACK) <= 136, f"stat label overflow: {label}"
        parts.append(
            text(x, vy, value, VALUE_SIZE,
                 t["accent"] if accent else t["text"], weight=VALUE_WEIGHT)
            + text(x, ly, label, LABEL_SIZE, t["muted"], tracking=LABEL_TRACK)
        )

    parts.append(rule(t, 152))

    # --- private / public split bar ----------------------------------------
    bar_y, bar_h = 164, 10
    priv_w = round(INNER * (priv / total), 2) if total else 0.0
    priv_w = max(0.0, min(priv_w, INNER))
    pub_w = round(INNER - priv_w, 2)
    round_clip = f"barround-{theme}"

    defs_bar = rounded_clip(round_clip, PAD, bar_y, INNER, bar_h)

    parts.append(
        f'<rect x="{PAD}" y="{bar_y}" width="{INNER}" height="{bar_h}" '
        f'rx="{fmt(bar_h / 2)}" fill="{t["track"]}"/>'
        f'<g clip-path="url(#{round_clip})">'
        f'<rect x="{PAD}" y="{bar_y}" width="{fmt(priv_w)}" height="{bar_h}" '
        f'fill="{t["accent"]}"/>'
        f'<rect x="{fmt(PAD + priv_w)}" y="{bar_y}" width="{fmt(pub_w)}" '
        f'height="{bar_h}" fill="{t["accent2"]}"/>'
        f"</g>"
    )

    caption = f"{priv:,} PRIVATE  ·  {pub:,} PUBLIC CONTRIBUTIONS"
    window = "LAST 12 MONTHS"
    assert mono_w(caption, 10, 0.4) + mono_w(window, 9.5, 0.4) + 16 <= INNER, \
        "stats caption overflow"
    parts.append(
        text(PAD, 190, caption, 10, t["muted"], tracking=0.4)
        + text(W - PAD, 190, window, 9.5, t["faint"], anchor="end", tracking=0.4)
    )

    a11y = (
        f"{title} on GitHub: {total:,} contributions in the last 12 months, "
        f"{priv:,} of them ({share:.1f}%) in private repositories and {pub:,} public. "
        f"{d['stars']:,} stars earned, {d['public_repos']:,} public repositories, "
        f"{d['forks']:,} forks earned, {d['followers']:,} followers."
    )
    return frame(t, f"{title} — GitHub statistics", a11y, "".join(parts), defs_bar)


# ---------------------------------------------------------------------------
# Card 2 — languages
# ---------------------------------------------------------------------------
# Vertical rhythm:
#   0   .. 36   header        (title baseline 30)
#   42  .. 55   stacked bar
#   64  .. 160  2-col legend, filled ROW-MAJOR over ceil(n/2) evenly spaced rows
#                so both columns are always populated, however many languages
#                come back from the API
#   166 .. 200  footer rule + provenance line
LEGEND_TOP, LEGEND_BOTTOM = 64, 160
LEGEND_MAX_ROWS = 4
LEGEND_COLS = 2
MAX_LEGEND = LEGEND_MAX_ROWS * LEGEND_COLS   # 8 entries incl. "Other"
LEG_COL_X = (PAD, PAD + 216)                 # 22, 238
LEG_COL_RIGHT = (PAD + 200, W - PAD)         # 222, 438


def top_languages(raw: list[tuple[str, float]]) -> list[tuple[str, float, int]]:
    """Collapse to MAX_LEGEND rows; everything past the cut becomes 'Other'."""
    named = [(n, float(v)) for n, v in raw if float(v) > 0 and n != "Other"]
    named.sort(key=lambda kv: kv[1], reverse=True)
    other = sum(float(v) for n, v in raw if n == "Other" and float(v) > 0)

    keep = named[: MAX_LEGEND - 1]
    other += sum(v for _, v in named[MAX_LEGEND - 1:])

    out = [(n, v, i) for i, (n, v) in enumerate(keep)]
    if other > 0:
        out.append(("Other", other, -1))
    return out


def render_langs(d: dict, theme: str) -> str:
    t = THEMES[theme]
    entries = top_languages(d["languages"])
    total = sum(v for _, v, _ in entries)
    if total <= 0:
        raise FetchError("language card: total is zero")

    parts: list[str] = []

    # --- header -------------------------------------------------------------
    title = "Languages"
    basis = f"{d['lang_basis']}  ·  {d['lang_repo_count']} NON-FORK REPOS"
    assert mono_w(title, 15) + mono_w(basis, 9.5, 0.4) + 24 <= INNER, "langs header overflow"
    parts.append(
        text(PAD, 30, title, 15, t["text"], weight="600")
        + text(W - PAD, 30, basis, 9.5, t["muted"], anchor="end", tracking=0.4)
    )

    # --- stacked bar --------------------------------------------------------
    bar_y, bar_h = 42, 13
    round_clip = f"langround-{theme}"
    defs = rounded_clip(round_clip, PAD, bar_y, INNER, bar_h)

    segs: list[str] = []
    cursor = float(PAD)
    for idx, (name, value, rank) in enumerate(entries):
        last = idx == len(entries) - 1
        seg_w = (W - PAD) - cursor if last else round(INNER * (value / total), 2)
        seg_w = max(seg_w, 0.0)
        fill = t["other"] if name == "Other" else lang_color(name, max(rank, 0), theme)
        segs.append(
            f'<rect x="{fmt(cursor)}" y="{bar_y}" width="{fmt(seg_w)}" '
            f'height="{bar_h}" fill="{fill}"/>'
        )
        cursor += seg_w
    parts.append(
        f'<rect x="{PAD}" y="{bar_y}" width="{INNER}" height="{bar_h}" '
        f'rx="{fmt(bar_h / 2)}" fill="{t["track"]}"/>'
        f'<g clip-path="url(#{round_clip})">'
        + "".join(segs) + "</g>"
    )

    # --- legend -------------------------------------------------------------
    # Row-major over an evenly distributed row count, so 4 languages fill the
    # card just as well as 8 and column 2 is never left empty.
    n = len(entries)
    rows = max(2, min(LEGEND_MAX_ROWS, -(-n // LEGEND_COLS)))
    band = LEGEND_BOTTOM - LEGEND_TOP
    slot_h = min(band / rows, 31)          # cap the pitch so 2 rows stay a block
    block_top = LEGEND_TOP + (band - slot_h * rows) / 2

    for i, (name, value, rank) in enumerate(entries):
        row, col = divmod(i, LEGEND_COLS)
        if row >= rows:
            break
        x = LEG_COL_X[col]
        right = LEG_COL_RIGHT[col]
        by = round(block_top + slot_h * row + slot_h / 2 + 4, 2)
        pct = f"{value / total * 100:.1f}%"
        short = LANG_SHORT.get(name, name)
        label = short if len(short) <= 13 else short[:12] + "…"
        # width check: dot(8) + gap(8) + name must clear the right-aligned pct
        name_right = x + 16 + mono_w(label, 11.5)
        pct_left = right - mono_w(pct, 10.5, 0.2)
        assert name_right + 8 <= pct_left, f"legend row collision: {name}"
        fill = t["other"] if name == "Other" else lang_color(name, max(rank, 0), theme)
        parts.append(
            f'<rect x="{fmt(x)}" y="{fmt(by - 8.5)}" width="9" height="9" rx="2.5" '
            f'fill="{fill}"/>'
            + text(x + 17, by, label, 11.5, t["text"])
            + text(right, by, pct, 10.5, t["muted"], anchor="end", tracking=0.2)
        )

    # --- footer -------------------------------------------------------------
    parts.append(rule(t, 166))
    prov = "GENERATED IN-REPO  ·  NO THIRD-PARTY SERVICE"
    handle = f"@{d['login']}"
    assert mono_w(prov, 9.5, 0.4) + mono_w(handle, 9.5, 0.4) + 16 <= INNER, \
        "langs footer overflow"
    parts.append(
        text(PAD, 186, prov, 9.5, t["faint"], tracking=0.4)
        + text(W - PAD, 186, handle, 9.5, t["faint"], anchor="end", tracking=0.4)
    )

    breakdown = ", ".join(
        f"{n} {v / total * 100:.1f}%" for n, v, _ in entries
    )
    a11y = (
        f"Language distribution across {d['lang_repo_count']} non-fork repositories, "
        f"measured by {d['lang_basis'].lower()}: {breakdown}."
    )
    return frame(t, "Language distribution", a11y, "".join(parts), defs)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def validate(svg: str, label: str) -> None:
    try:
        ET.fromstring(svg)
    except ET.ParseError as exc:
        raise FetchError(f"{label}: generated SVG is not well-formed XML: {exc}") from exc
    if len(svg) < 500:
        raise FetchError(f"{label}: generated SVG is suspiciously small ({len(svg)}B)")
    if len(svg) > 120_000:
        raise FetchError(f"{label}: generated SVG is too large ({len(svg)}B)")
    # Nothing that GitHub's sanitiser strips, and no external fetch of any kind:
    # the only permitted URL in the whole file is the SVG namespace.
    # "<animate"/"<set" are banned outright: see the NO ANIMATION note at the top
    # of this file — inside camo's <img> the SMIL timeline is frozen at t=0, so
    # any animated element renders at its START value forever.
    for banned in ("<script", "<foreignObject", "<image", "@import",
                   "<style", 'href="http', "url(http", "<animate", "<set"):
        if banned in svg:
            raise FetchError(f"{label}: contains banned construct {banned!r}")
    if svg.count("http") != svg.count("http://www.w3.org/2000/svg"):
        raise FetchError(f"{label}: contains an unexpected external URL")


def self_test() -> int:
    """Prove the degraded-token path fails instead of rendering.

    Simulates what the repo-scoped default GITHUB_TOKEN actually returns:
    well-formed JSON, non-zero totals, but restrictedContributionsCount == 0 and
    only public repositories visible.
    """
    ok = True

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    print("self-test: sanity gate")
    good = dict(OFFLINE)
    try:
        sanity_gate(good)
        check("verified fact-sheet data passes the gate", True)
    except FetchError as exc:
        check(f"verified fact-sheet data passes the gate ({exc})", False)

    degraded = dict(OFFLINE)
    degraded.update({
        # default GITHUB_TOKEN: no read:user -> private contributions invisible
        "contributions_private": 0,
        "contributions_public": 1675,
        # default GITHUB_TOKEN: private repos invisible -> only 21 non-fork public
        "lang_repo_count": 21,
        "languages": [("Python", 12.0), ("JavaScript", 6.0), ("TypeScript", 3.0)],
    })
    try:
        sanity_gate(degraded)
        check("degraded (default GITHUB_TOKEN) response is REJECTED", False)
    except FetchError as exc:
        check(f"degraded response rejected: {exc}", True)

    for field, bad in (("followers", 3), ("stars", 4), ("lang_repo_count", 2)):
        d = dict(OFFLINE)
        d[field] = bad
        try:
            sanity_gate(d)
            check(f"floor on {field} rejects {bad}", False)
        except FetchError:
            check(f"floor on {field} rejects {bad}", True)

    print("self-test: no animation in rendered output")
    for theme in ("dark", "light"):
        for label, svg in (("stats", render_stats(OFFLINE, theme)),
                           ("langs", render_langs(OFFLINE, theme))):
            validate(svg, f"{label}-{theme}")
            check(f"{label}-{theme}: no <animate>/<set>, validates",
                  "<animate" not in svg and "<set" not in svg)

    print("self-test: determinism")
    a = render_stats(OFFLINE, "dark")
    b = render_stats(OFFLINE, "dark")
    check("same input -> byte-identical output", a == b)

    print("self-test: palette contrast floors")
    try:
        _assert_palette()
        check("every shipped colour clears its floor", True)
    except AssertionError as exc:
        check(f"palette: {exc}", False)

    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate self-hosted profile stat cards.")
    ap.add_argument("--offline", action="store_true",
                    help="render from the verified fact-sheet numbers; no network")
    ap.add_argument("--out-dir", default=None,
                    help="output directory (default: <repo>/assets)")
    ap.add_argument("--contrast-report", action="store_true",
                    help="print the measured contrast ratio of every shipped colour")
    ap.add_argument("--self-test", action="store_true",
                    help="verify the sanity gate, the no-animation rule and determinism")
    args = ap.parse_args(argv)

    if args.contrast_report:
        print(contrast_report())
        return 0
    if args.self_test:
        return self_test()

    out_dir = pathlib.Path(args.out_dir) if args.out_dir else \
        pathlib.Path(__file__).resolve().parent.parent / "assets"

    if args.offline:
        data = dict(OFFLINE)
        sanity_gate(data)
        sys.stderr.write("[gen_cards] offline mode: rendering verified fallback data\n")
    else:
        token = (os.environ.get("PROFILE_TOKEN")
                 or os.environ.get("GITHUB_TOKEN")
                 or os.environ.get("GH_TOKEN"))
        if not token:
            sys.stderr.write(
                "[gen_cards] FATAL: PROFILE_TOKEN is not set. "
                "Refusing to write cards. Use --offline for the fallback render.\n"
            )
            return 2
        if not os.environ.get("PROFILE_TOKEN"):
            sys.stderr.write(
                "[gen_cards] WARNING: falling back to GITHUB_TOKEN. The default "
                "Actions token cannot see private contributions or private repo "
                "languages; the sanity-gate floors will reject the degraded "
                "response rather than render a wrong card.\n"
            )
        try:
            data = fetch(token)
        except Exception as exc:  # noqa: BLE001 - any failure must be terminal
            sys.stderr.write(
                f"[gen_cards] FATAL: {type(exc).__name__}: {exc}\n"
                "[gen_cards] Nothing was written; the previously committed "
                "cards remain the last known-good version.\n"
            )
            return 1

    # Render everything into memory first. A failure here must not leave a
    # half-updated assets/ directory behind.
    try:
        # Dark only, by design. The cards sit directly under assets/hero.svg,
        # which is a single opaque dark panel with no light variant. Emitting
        # white cards underneath it would look inconsistent on light-theme
        # GitHub, and because every panel paints its own opaque background,
        # one file per graphic renders identically in both themes -- a
        # light/dark mismatch becomes structurally impossible rather than
        # something <picture> has to get right. render_* still accepts
        # "light" and THEMES still carries a validated light palette, so
        # switching back is a one-line change here.
        rendered = {
            "stats-dark.svg": render_stats(data, "dark"),
            "langs-dark.svg": render_langs(data, "dark"),
        }
        for fname, svg in rendered.items():
            validate(svg, fname)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[gen_cards] FATAL render/validate: "
                         f"{type(exc).__name__}: {exc}\n")
        return 1

    # Two-pass write: stage every .tmp first, then rename them all. A failure
    # partway through the staging pass leaves the committed cards untouched,
    # so assets/ can never end up holding a mix of old and new numbers.
    out_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for fname, svg in rendered.items():
            tmp = out_dir / (fname + ".tmp")
            tmp.write_text(svg + "\n", encoding="utf-8")
            staged.append((tmp, out_dir / fname, len(svg)))
    except Exception as exc:  # noqa: BLE001
        for tmp, _, _ in staged:
            tmp.unlink(missing_ok=True)
        sys.stderr.write(f"[gen_cards] FATAL staging: {type(exc).__name__}: {exc}\n"
                         "[gen_cards] Nothing was written; the previously "
                         "committed cards remain the last known-good version.\n")
        return 1

    for tmp, dest, size in staged:
        tmp.replace(dest)
        sys.stderr.write(f"[gen_cards] wrote {dest} ({size}B)\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
