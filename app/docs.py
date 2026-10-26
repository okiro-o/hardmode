"""Tiny Markdown -> HTML renderer for the in-app help (QUICKSTART.md).

Supports what the help uses: ATX headings (with ids for a table of contents), paragraphs,
bullet and numbered lists, pipe tables, fenced code blocks, inline code, bold, autolinks
<https://...> and [text](url). Everything is HTML-escaped first. No external dependency.
"""
from __future__ import annotations

import html
import os
import re

from . import config

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_AUTOLINK = re.compile(r"&lt;(https?://[^&\s]+)&gt;")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _slug(text: str, used: set[str]) -> str:
    base = re.sub(r"[^\w\-]+", "-", text.lower(), flags=re.UNICODE).strip("-") or "h"
    slug, i = base, 2
    while slug in used:
        slug, i = f"{base}-{i}", i + 1
    used.add(slug)
    return slug


def inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = _INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _BOLD.sub(r"<b>\1</b>", out)
    out = _AUTOLINK.sub(r'<a href="\1" target="_blank" rel="noopener">\1</a>', out)
    out = _LINK.sub(r'<a href="\2" target="_blank" rel="noopener">\1</a>', out)
    return out


def render(md: str) -> tuple[str, list[dict]]:
    """Returns (html, headings) where headings = [{"id", "level", "text"}]."""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    headings: list[dict] = []
    used: set[str] = set()
    i = 0
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + inline(" ".join(s.strip() for s in para)) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush_para()
            i += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            flush_para()
            level = len(m.group(1))
            text = m.group(2).strip()
            hid = _slug(text, used)
            out.append(f'<h{level} id="{hid}">{inline(text)}</h{level}>')
            if level >= 2:
                headings.append({"id": hid, "level": level, "text": text})
            i += 1
            continue
        if stripped.startswith("```"):
            flush_para()
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            continue
        if stripped.startswith("|"):
            flush_para()
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                head, body = rows[0], rows[1:]
                out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                           + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body)
                           + "</tbody></table>")
            continue
        lm = re.match(r"^(\s*)([-*]|\d+[.)])\s+(.*)$", line)
        if lm:
            flush_para()
            ordered = lm.group(2)[0].isdigit()
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while i < len(lines):
                m2 = re.match(r"^(\s*)([-*]|\d+[.)])\s+(.*)$", lines[i])
                if m2 and (m2.group(2)[0].isdigit()) == ordered:
                    items.append(m2.group(3).strip())
                    i += 1
                    # continuation lines (indented, not a new item, not blank)
                    while i < len(lines) and lines[i].strip() and lines[i].startswith((" ", "\t")) \
                            and not re.match(r"^(\s*)([-*]|\d+[.)])\s+", lines[i]) and not lines[i].strip().startswith("|"):
                        items[-1] += " " + lines[i].strip()
                        i += 1
                else:
                    break
            out.append(f"<{tag}>" + "".join(f"<li>{inline(it)}</li>" for it in items) + f"</{tag}>")
            continue
        para.append(line)
        i += 1
    flush_para()
    return "\n".join(out), headings


def quickstart_path() -> str:
    for base in (config.app_root(), os.path.dirname(config.package_dir()), config.package_dir()):
        p = os.path.join(base, "QUICKSTART.md")
        if os.path.isfile(p):
            return p
    return os.path.join(config.app_root(), "QUICKSTART.md")


def load_help() -> dict:
    try:
        with open(quickstart_path(), encoding="utf-8") as f:
            md = f.read()
    except OSError:
        md = "# Справка\n\nФайл QUICKSTART.md не найден рядом с программой."
    body, headings = render(md)
    return {"html": body, "headings": headings}
