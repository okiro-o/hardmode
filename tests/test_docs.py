from app.docs import inline, load_help, render


def test_render_basic_blocks():
    md = """# Title

## Section one

Text with `code`, **bold**, <https://a.b/c> and [link](https://x.y).

- item one
- item two
  continued

1. first
2. second

| A | B |
|---|---|
| 1 | `2` |

```
raw <code>
```
"""
    html, headings = render(md)
    assert '<h1 id="title">Title</h1>' in html
    assert headings == [{"id": "section-one", "level": 2, "text": "Section one"}]
    assert "<code>code</code>" in html and "<b>bold</b>" in html
    assert 'href="https://a.b/c"' in html and '<a href="https://x.y" target="_blank" rel="noopener">link</a>' in html
    assert "<ul><li>item one</li><li>item two continued</li></ul>" in html
    assert "<ol><li>first</li><li>second</li></ol>" in html
    assert "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td><code>2</code></td></tr></tbody></table>" in html
    assert "<pre><code>raw &lt;code&gt;</code></pre>" in html


def test_inline_escapes_html():
    assert inline("<script>alert(1)</script> & **x**") == "&lt;script&gt;alert(1)&lt;/script&gt; &amp; <b>x</b>"


def test_help_file_renders_with_headings():
    h = load_help()
    assert "<h2" in h["html"] and any(x["text"].startswith("Startup") for x in h["headings"])
    ids = [x["id"] for x in h["headings"]]
    assert len(ids) == len(set(ids))


def test_quickstart_found_inside_frozen_internal_dir(tmp_path, monkeypatch):
    """PyInstaller onedir: exe in dist/, data files in dist/_internal (sys._MEIPASS)."""
    import sys

    from app import docs

    exe_dir = tmp_path / "dist"
    internal = exe_dir / "_internal"
    (internal / "app").mkdir(parents=True)
    (internal / "QUICKSTART.md").write_text("# Справка\n\n## Startup\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "hardmode.exe"))
    assert docs.quickstart_path() == str(internal / "QUICKSTART.md")
    assert any(h["text"] == "Startup" for h in docs.load_help()["headings"])


def test_build_script_ships_quickstart():
    import os

    bat = open(os.path.join(os.path.dirname(__file__), "..", "build.bat"), encoding="utf-8").read()
    assert '--add-data "QUICKSTART.md;."' in bat
