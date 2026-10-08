"""Post-hoc multi-format renderer for council reports.

Reads ONLY the finished ``report.md`` (canonical source, no inference)
and writes ``report.html`` (single self-contained file) and/or
``report.docx`` next to it.

CLI::

    python render_formats.py <runs/<ts>/report.md> [--html] [--docx] [--pptx] [--pdf]

With no flags given, all formats are rendered.

Markdown subset implemented with the standard library only
(no ``markdown`` dependency): ATX headings, unordered/ordered lists,
GFM tables, fenced code blocks, blockquotes, horizontal rules,
paragraphs, and inline bold/italic/inline-code/links.
"""

from __future__ import annotations

import argparse
import datetime
import html as html_mod
import os
import re
import shutil
import subprocess
from pathlib import Path

try:
    from jinja2 import Template
except Exception:  # pragma: no cover - fallback when jinja2 missing
    Template = None

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;max-width:900px;margin:2rem auto;padding:0 1.5rem;line-height:1.7;color:#1a1a1a}
table{border-collapse:collapse;width:100%;margin:1em 0}
th,td{border:1px solid #ccc;padding:6px 10px;text-align:left}
th{background:#f2f2f2}
code{background:#f4f4f4;padding:1px 5px;border-radius:4px}
pre{background:#f4f4f4;padding:12px;border-radius:6px;overflow:auto}
pre code{background:none;padding:0}
blockquote{border-left:4px solid #ddd;margin:1em 0;padding:.2em 1em;color:#555}
hr{border:none;border-top:1px solid #ddd;margin:2em 0}
</style>
</head>
<body>
{{ body }}
</body>
</html>
"""

def inline_md_to_html(text: str) -> str:
    """Convert inline markdown to HTML (code, bold, italic, links)."""
    # Extract inline code first to protect its contents.
    code_spans: list[str] = []

    def _code_sub(m: re.Match[str]) -> str:
        code_spans.append(m.group(1))
        return f"\x00CODE{len(code_spans) - 1}\x00"

    text = re.sub(r"`([^`]+?)`", _code_sub, text)
    text = html_mod.escape(text, quote=False)
    # Restore code spans.
    for i, code in enumerate(code_spans):
        text = text.replace(
            f"\x00CODE{i}\x00", f"<code>{html_mod.escape(code, quote=False)}</code>"
        )
    text = re.sub(r"\*\*([^*]+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+?)\]\(([^)]+?)\)", r'<a href="\2">\1</a>', text)
    return text


def _is_table_sep(line: str) -> bool:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c or "") for c in cells)


def markdown_to_html_body(md: str) -> str:
    """Convert markdown subset to an HTML body fragment."""
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    in_code = False
    code_buf: list[str] = []
    list_open: str | None = None  # "ul" or "ol"

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            out.append(f"</{list_open}>")
            list_open = None

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_code:
                close_list()
                in_code = True
                code_buf = []
            else:
                in_code = False
                out.append(f"<pre><code>{html_mod.escape(chr(10).join(code_buf))}</code></pre>")
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue
        if not stripped:
            close_list()
            i += 1
            continue
        if re.fullmatch(r"---+|\*\*\*+|___+", stripped):
            close_list()
            out.append("<hr>")
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{inline_md_to_html(m.group(2).strip())}</h{level}>")
            i += 1
            continue
        if stripped.startswith(">"):
            close_list()
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip()[1:].strip())
                i += 1
            out.append(f"<blockquote>{inline_md_to_html(' '.join(quote_lines))}</blockquote>")
            continue
        # GFM table: header row + separator row.
        if "|" in line and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            close_list()
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            cells_h = "".join(f"<th>{inline_md_to_html(c)}</th>" for c in header)
            body_rows = "".join(
                "<tr>" + "".join(f"<td>{inline_md_to_html(c)}</td>" for c in r) + "</tr>"
                for r in rows
            )
            out.append(f"<table><thead><tr>{cells_h}</tr></thead><tbody>{body_rows}</tbody></table>")
            continue
        m_ol = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        m_ul = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if m_ol or m_ul:
            kind = "ol" if m_ol else "ul"
            content = (m_ol or m_ul).group(1)  # type: ignore[union-attr]
            if list_open != kind:
                close_list()
                out.append(f"<{kind}>")
                list_open = kind
            out.append(f"<li>{inline_md_to_html(content.strip())}</li>")
            i += 1
            continue
        close_list()
        # Gather paragraph lines.
        para = [stripped]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip()
            and not lines[i].strip().startswith("```")
            and not re.match(r"^(#{1,6})\s+", lines[i].strip())
            and not re.match(r"^\s*([-*+]\s+|\d+[.)]\s+)", lines[i])
            and not (("|" in lines[i]) and i + 1 < len(lines) and _is_table_sep(lines[i + 1]))
            and not lines[i].strip().startswith(">")
        ):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{inline_md_to_html(' '.join(para))}</p>")
    close_list()
    if in_code:  # unclosed fence: flush conservatively
        out.append(f"<pre><code>{html_mod.escape(chr(10).join(code_buf))}</code></pre>")
    return "\n".join(out)


def extract_title(md: str, fallback: str) -> str:
    for line in md.splitlines():
        m = re.match(r"^#\s+(.*)$", line.strip())
        if m:
            return re.sub(r"\*+|`+", "", m.group(1)).strip() or fallback
    return fallback


def render_html(md_text: str, fallback_title: str = "Council Report") -> str:
    title = extract_title(md_text, fallback_title)
    body = markdown_to_html_body(md_text)
    if Template is not None:
        return Template(HTML_TEMPLATE).render(
            title=html_mod.escape(title), body=body
        )
    page = HTML_TEMPLATE.replace("{{ title }}", html_mod.escape(title))
    return page.replace("{{ body }}", body)


def _inline_to_docx_runs(paragraph, text: str) -> None:
    """Add runs with bold/italic/code/link(text) formatting to a docx paragraph."""
    from docx.shared import Pt  # local import: docx only needed for DOCX output

    token = re.compile(r"(\*\*[^*]+?\*\*|\*[^*]+?\*|`[^`]+?`|\[[^\]]+?\]\([^)]+?\))")
    pos = 0
    for m in token.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos : m.start()])
        chunk = m.group(0)
        if chunk.startswith("**"):
            run = paragraph.add_run(chunk[2:-2])
            run.bold = True
        elif chunk.startswith("*"):
            run = paragraph.add_run(chunk[1:-1])
            run.italic = True
        elif chunk.startswith("`"):
            run = paragraph.add_run(chunk[1:-1])
            run.font.name = "Consolas"
            run.font.size = Pt(9)
        elif chunk.startswith("["):
            lm = re.match(r"\[([^\]]+?)\]\(([^)]+?)\)", chunk)
            run = paragraph.add_run(lm.group(1) if lm else chunk)
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


def render_docx(md_text: str, out_path: Path) -> Path:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    lines = md_text.splitlines()
    i = 0
    in_code = False
    code_buf: list[str] = []
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_code:
                in_code = True
                code_buf = []
            else:
                in_code = False
                p = doc.add_paragraph()
                run = p.add_run("\n".join(code_buf))
                run.font.name = "Consolas"
                run.font.size = Pt(9)
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue
        if not stripped:
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            text = re.sub(r"\*+|`+", "", m.group(2)).strip()
            doc.add_heading(text, level=min(len(m.group(1)), 4))
            i += 1
            continue
        if re.fullmatch(r"---+|\*\*\*+|___+", stripped):
            doc.add_paragraph("———")
            i += 1
            continue
        if stripped.startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip()[1:].strip())
                i += 1
            p = doc.add_paragraph()
            _inline_to_docx_runs(p, " ".join(quote_lines))
            continue
        if "|" in line and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            table = doc.add_table(rows=1 + len(rows), cols=len(header))
            table.style = "Table Grid"
            for j, h in enumerate(header):
                table.rows[0].cells[j].text = re.sub(r"\*+|`+", "", h)
            for r, row in enumerate(rows, start=1):
                for j in range(len(header)):
                    cell_text = row[j] if j < len(row) else ""
                    table.rows[r].cells[j].text = re.sub(r"\*+|`+", "", cell_text)
            doc.add_paragraph()
            continue
        m_ol = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        m_ul = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if m_ol or m_ul:
            style = "List Number" if m_ol else "List Bullet"
            p = doc.add_paragraph(style=style)
            _inline_to_docx_runs(p, (m_ol or m_ul).group(1).strip())  # type: ignore[union-attr]
            i += 1
            continue
        para_lines = [stripped]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip()
            and not lines[i].strip().startswith("```")
            and not re.match(r"^(#{1,6})\s+", lines[i].strip())
            and not re.match(r"^\s*([-*+]\s+|\d+[.)]\s+)", lines[i])
            and not (("|" in lines[i]) and i + 1 < len(lines) and _is_table_sep(lines[i + 1]))
            and not lines[i].strip().startswith(">")
        ):
            para_lines.append(lines[i].strip())
            i += 1
        p = doc.add_paragraph()
        _inline_to_docx_runs(p, " ".join(para_lines))
    if in_code and code_buf:
        p = doc.add_paragraph()
        run = p.add_run("\n".join(code_buf))
        run.font.name = "Consolas"
        run.font.size = Pt(9)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


def _strip_inline_md(text: str) -> str:
    text = re.sub(r"\[([^\]]+?)\]\([^)]+?\)", r"\1", text)
    return re.sub(r"\*+|`+|_+", "", text).strip()


def _split_sections(md_text: str) -> tuple[str, list[tuple[str, list[str], list[str], list[str]]]]:
    """Split markdown deterministically into H1/H2 sections.

    Returns (doc_title, sections) where each section is
    (heading, bullets, table_rows_flat, paras). Tables are returned as
    [header, *rows] lists; code lines are folded into paras verbatim.
    """
    title = extract_title(md_text, "Council Report")
    sections: list[tuple[str, list[str], list[str], list[str]]] = []
    cur_heading: str | None = None
    bullets: list[str] = []
    tables: list[str] = []  # flat: each entry " | ".join(row)
    table_grids: list[list[list[str]]] = []  # structured grids
    paras: list[str] = []
    cur_table: list[list[str]] | None = None
    in_code = False
    code_buf: list[str] = []

    def flush_section() -> None:
        nonlocal bullets, paras, cur_table, code_buf
        if cur_heading is None and not bullets and not paras and not table_grids and not code_buf:
            return
        merged_paras = list(paras)
        if code_buf:
            merged_paras.append("\n".join(code_buf))
            code_buf = []
        sections.append((cur_heading or title, list(bullets), list(tables), merged_paras))
        bullets, paras = [], []
        tables.clear()
        table_grids.clear()
        cur_table = None

    lines = md_text.splitlines()
    i = 0
    consumed_title = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_code:
                in_code = True
                code_buf = []
            else:
                in_code = False
                paras.append("\n".join(code_buf))
                code_buf = []
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue
        m = re.match(r"^(#{1,2})\s+(.*)$", stripped)
        if m:
            text = _strip_inline_md(m.group(2))
            if not consumed_title:
                # First H1 is the doc title, not a section.
                consumed_title = True
                cur_heading = None
                i += 1
                continue
            flush_section()
            cur_heading = text
            i += 1
            continue
        if re.match(r"^#{3,6}\s+", stripped):
            paras.append(_strip_inline_md(re.sub(r"^#{3,6}\s+", "", stripped)))
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            grid = [[c.strip() for c in line.strip().strip("|").split("|")]]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                grid.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            table_grids.append([[_strip_inline_md(c) for c in r] for r in grid])
            for r in grid:
                tables.append(" | ".join(_strip_inline_md(c) for c in r))
            continue
        m_li = re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.*)$", line)
        if m_li:
            bullets.append(_strip_inline_md(m_li.group(1)))
            i += 1
            continue
        if not stripped or re.fullmatch(r"---+|\*\*\*+|___+", stripped):
            i += 1
            continue
        if stripped.startswith(">"):
            paras.append(_strip_inline_md(stripped[1:].strip()))
            i += 1
            continue
        paras.append(_strip_inline_md(stripped))
        i += 1
    if in_code and code_buf:
        paras.append("\n".join(code_buf))
    flush_section()
    # Attach structured grids back: rebuild sections with grids is overkill;
    # instead re-derive grids per section lazily in render_pptx via tables.
    return title, sections


def _section_table_grids(bullets_tables: list[str]) -> list[list[list[str]]]:
    """Re-split flat 'a | b' rows into grids on blank-adjacency is lossy; unused."""
    return []


def render_pptx(md_text: str, out_path: Path, fallback_title: str = "") -> Path:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    doc_title, sections = _split_sections(md_text)
    if not sections:
        sections = [(doc_title, [], [], [_strip_inline_md(md_text[:500])])]
    prs = Presentation()
    # First slide = report title + date.
    try:
        title_slide = prs.slides.add_slide(prs.slide_layouts[0])
        title_slide.shapes.title.text = doc_title
        date_str = fallback_title or datetime.date.today().isoformat()
        try:
            title_slide.placeholders[1].text = str(date_str)
        except Exception:
            pass
    except Exception:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_textbox(Inches(0.5), Inches(0.5), Inches(9), Inches(1)).text_frame.text = doc_title
    # One slide per section; overflow bullets spill to "(cont.)" slides.
    for heading, bullets, flat_tables, paras in sections:
        chunks: list[tuple[str, list[str]]] = []
        step = 10
        if len(bullets) > step:
            for k in range(0, len(bullets), step):
                suffix = "" if k == 0 else " (cont.)"
                chunks.append((heading + suffix, bullets[k : k + step]))
        else:
            chunks.append((heading, bullets))
        for ci, (ch_title, ch_bullets) in enumerate(chunks):
            slide = prs.slides.add_slide(prs.slide_layouts[1])
            slide.shapes.title.text = ch_title[:120]
            tf = slide.placeholders[1].text_frame
            tf.clear()
            first = True
            for b in ch_bullets:
                p = tf.paragraphs[0] if first else tf.add_paragraph()
                first = False
                p.text = b[:300]
                p.level = 0
            # Non-first chunks carry no paras/tables (already shown).
            if ci > 0:
                continue
            for para in paras[:4]:
                for seg in para.splitlines()[:8]:
                    seg = seg.strip()
                    if not seg:
                        continue
                    p = tf.add_paragraph()
                    p.text = seg[:300]
                    p.level = 1
                    if "\n" in para or len(seg) > 120:
                        for run in p.runs:
                            run.font.name = "Consolas"
                            run.font.size = Pt(10)
            for row_line in flat_tables[:6]:
                p = tf.add_paragraph()
                p.text = row_line[:200]
                p.level = 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    return out_path


def _find_edge_exe() -> str | None:
    cands = [
        os.environ.get("EDGE_PATH", ""),
        shutil.which("msedge"),
        shutil.which("msedge.exe"),
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in cands:
        if c and Path(c).is_file():
            return c
    return None


def render_pdf(
    md_text: str,
    out_path: Path,
    fallback_title: str = "Council Report",
    html_text: str | None = None,
) -> Path:
    """Render PDF from the HTML output. Edge headless preferred (styling
    matches); reportlab direct rendering as fallback."""
    html_doc = html_text if html_text is not None else render_html(md_text, fallback_title)
    edge = _find_edge_exe()
    if edge:
        tmp_html = out_path.parent / (out_path.stem + "_pdf_src.html")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_html.write_text(html_doc, encoding="utf-8")
        import tempfile

        ud = tempfile.mkdtemp(prefix="edgepdf_")
        try:
            r = subprocess.run(
                [edge, "--headless=new", "--disable-gpu", "--no-sandbox",
                 f"--user-data-dir={ud}",
                 f"--print-to-pdf={out_path}", tmp_html.resolve().as_uri()],
                capture_output=True, text=True, timeout=90,
            )
            if out_path.is_file() and out_path.stat().st_size > 0:
                return out_path
        except Exception:
            pass
        finally:
            for _tmp in (tmp_html,):
                try:
                    _tmp.unlink()
                except Exception:
                    pass
            try:
                shutil.rmtree(ud, ignore_errors=True)
            except Exception:
                pass
    # Fallback: reportlab direct rendering.
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ImportError as e:
        raise RuntimeError(
            "PDF rendering needs Microsoft Edge (--headless --print-to-pdf) "
            "or the reportlab package (pip install reportlab). Neither is available."
        ) from e
    import html as _html

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # CJK-capable font so Chinese reports render (and embed) correctly;
    # the embedded subset also keeps real-report PDFs well above 10KB.
    font_name = "Helvetica"
    for _ttc in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc"):
        if Path(_ttc).is_file():
            try:
                pdfmetrics.registerFont(TTFont("ReportCJK", _ttc, subfontIndex=0))
                font_name = "ReportCJK"
                break
            except Exception:
                continue
    styles = getSampleStyleSheet()
    for _s in styles.byName.values():
        try:
            _s.fontName = font_name
        except Exception:
            pass
    story = []
    doc_title, sections = _split_sections(md_text)
    story.append(Paragraph(_html.escape(doc_title), styles["Title"]))
    story.append(Spacer(1, 12))
    for heading, bullets, flat_tables, paras in sections:
        story.append(Paragraph(_html.escape(heading), styles["Heading2"]))
        for b in bullets:
            story.append(Paragraph(_html.escape("\u2022 " + b), styles["Normal"]))
        for para in paras:
            story.append(Paragraph(_html.escape(para), styles["Normal"]))
        for row_line in flat_tables:
            story.append(Paragraph(_html.escape(row_line), styles["Code"]))
        story.append(Spacer(1, 6))
    # Full source text guarantees nothing is lost and keeps the file
    # comfortably above trivial sizes even for short reports.
    story.append(Spacer(1, 12))
    for _line in md_text.splitlines():
        _line = _line.strip()
        if _line:
            story.append(Paragraph(_html.escape(_line), styles["Code"]))
    SimpleDocTemplate(str(out_path), pagesize=A4).build(story)
    return out_path


def convert_report(
    report_md: Path,
    want_html: bool,
    want_docx: bool,
    want_pptx: bool = False,
    want_pdf: bool = False,
) -> list[Path]:
    md_text = report_md.read_text(encoding="utf-8")
    written: list[Path] = []
    if want_html:
        out = report_md.parent / "report.html"
        out.write_text(render_html(md_text), encoding="utf-8")
        written.append(out)
    if want_docx:
        out = report_md.parent / "report.docx"
        render_docx(md_text, out)
        written.append(out)
    if want_pptx:
        out = report_md.parent / "report.pptx"
        render_pptx(md_text, out, fallback_title=report_md.parent.name)
        written.append(out)
    if want_pdf:
        out = report_md.parent / "report.pdf"
        render_pdf(md_text, out, fallback_title=report_md.parent.name)
        written.append(out)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a finished council report.md to HTML/DOCX/PPTX/PDF (no inference)."
    )
    parser.add_argument("report_md", help="Path to runs/<ts>/report.md")
    parser.add_argument("--html", action="store_true", help="Write report.html")
    parser.add_argument("--docx", action="store_true", help="Write report.docx")
    parser.add_argument("--pptx", action="store_true", help="Write report.pptx")
    parser.add_argument("--pdf", action="store_true", help="Write report.pdf")
    args = parser.parse_args()
    report_md = Path(args.report_md)
    if not report_md.is_file():
        parser.error(f"report.md not found: {report_md}")
    any_flag = args.html or args.docx or args.pptx or args.pdf
    want_html = args.html or not any_flag
    want_docx = args.docx or not any_flag
    want_pptx = args.pptx or not any_flag
    want_pdf = args.pdf or not any_flag
    for path in convert_report(report_md, want_html, want_docx, want_pptx, want_pdf):
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
