"""Minimal, safe Markdown -> HTML (headings, tables, lists, quotes, emphasis, code, links)."""
import html
import re


def _inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])_([^_]+)_(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\((/[^)\s]*|https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"(?<![\"'>=])(https?://[^\s<)|]+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', s)
    s = re.sub(r"\b(E-\d{4})\b", r'<a class="evlink" href="/evidence?code=\1">\1</a>', s)
    return s.replace("  <br>", "<br>")


def render(md):
    lines = (md or "").split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            body = [r for r in rows if not all(re.match(r"^:?-{2,}:?$", c) for c in r)]
            t = ["<div class='tablewrap'><table class='md'>"]
            for k, r in enumerate(body):
                tag = "th" if k == 0 else "td"
                t.append("<tr>" + "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in r) + "</tr>")
            t.append("</table></div>")
            out.append("".join(t))
            continue
        if line.startswith(">"):
            q = []
            while i < len(lines) and lines[i].startswith(">"):
                q.append(lines[i][1:].strip())
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(q))}</blockquote>")
            continue
        if re.match(r"^\s*[-*]\s+", line):
            items = []
            while i < len(lines) and (re.match(r"^\s*[-*]\s+", lines[i]) or (lines[i].startswith("  ") and lines[i].strip())):
                l = lines[i]
                if re.match(r"^\s{2,}[-*]\s+", l):
                    items.append(("sub", re.sub(r"^\s*[-*]\s+", "", l)))
                elif re.match(r"^\s*[-*]\s+", l):
                    items.append(("top", re.sub(r"^\s*[-*]\s+", "", l)))
                elif items:
                    items[-1] = (items[-1][0], items[-1][1] + "<br>" + l.strip())
                i += 1
            h, open_sub = ["<ul>"], False
            for kind, txt in items:
                txt_html = _inline(txt.replace("<br>", "\u0000")).replace("\u0000", "<br>").replace("  <br>", "<br>")
                if kind == "sub" and not open_sub:
                    h.append("<ul>")
                    open_sub = True
                if kind == "top" and open_sub:
                    h.append("</ul>")
                    open_sub = False
                h.append(f"<li>{txt_html}</li>")
            if open_sub:
                h.append("</ul>")
            h.append("</ul>")
            out.append("".join(h))
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||>|\s*[-*]\s)", lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)
