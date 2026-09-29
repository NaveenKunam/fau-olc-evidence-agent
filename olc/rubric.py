"""Import official OLC wording from the QSS PDF and interpretation text from the Handbook PDF.

Both imports are two-step: parse -> preview (diff against current wording) -> admin applies.
Parsing is heuristic because PDF layouts vary; nothing is written until a human confirms the preview.
"""
import io
import re

from .indicator_data import CATEGORIES
from .indicators import all_indicators, update_indicator_text
from .db import audit


def _pages(data):
    import pypdf
    r = pypdf.PdfReader(io.BytesIO(data))
    return [(i + 1, re.sub(r"[ \t]+", " ", p.extract_text() or "")) for i, p in enumerate(r.pages)]


def _cat_regex(name):
    words = re.split(r"\s+|&", name.replace("&", " & "))
    return r"\s*(?:&|and)?\s*".join(re.escape(w) for w in words if w and w != "&")


def parse_qss(data):
    pages = _pages(data)
    text, page_at = "", []
    for pno, t in pages:
        page_at.append((len(text), pno))
        text += t + "\n"

    def page_of(pos):
        p = 1
        for off, pno in page_at:
            if off <= pos:
                p = pno
        return p

    alias = {"Learner Support": ["Learner Support", "Student Support"],
             "Course Development & Instructional Design": ["Course Development & Instructional Design",
                                                            "Course Development and Instructional Design",
                                                            "Course Development/Instructional Design"]}
    found = []
    for code, name, count in CATEGORIES:
        names = alias.get(name, [name])
        best = None
        for nm in names:
            for m in re.finditer(_cat_regex(nm), text, re.I):
                # prefer occurrences followed by an indicator numbered 1
                if re.match(r"[\s\S]{0,400}?\b1\s*[.)]?\s+[A-Z]", text[m.end():m.end() + 420]):
                    best = m
                    break
            if best:
                break
        if best:
            found.append((best.start(), best.end(), code, count))
    found.sort()
    results = {}
    for k, (s, e, code, count) in enumerate(found):
        end = found[k + 1][0] if k + 1 < len(found) else len(text)
        seg = text[e:end]
        for m in re.finditer(r"(?:^|\n)\s*(\d{1,2})\s*[.)]?\s+([A-Z][\s\S]+?)(?=\n\s*\d{1,2}\s*[.)]?\s+[A-Z]|\Z)", seg):
            n = int(m.group(1))
            if not (1 <= n <= count) or f"{code}-{n:02d}" in results:
                continue
            body = re.sub(r"\s+", " ", m.group(2)).strip()
            body = re.split(r"\s(?:Score|Points|0 1 2 3|Deficient|Developing|Accomplished|Exemplary)\b", body)[0].strip()
            if len(body) < 25:
                continue
            results[f"{code}-{n:02d}"] = {"text": body[:900], "page": page_of(e + m.start())}
    return results


def parse_handbook(data, indicators):
    pages = _pages(data)
    text, page_at = "", []
    for pno, t in pages:
        page_at.append((len(text), pno))
        text += t + "\n"
    flat = re.sub(r"\s+", " ", text)
    # map positions in flat text back to pages approximately by proportion
    ratio = len(flat) / max(len(text), 1)

    def page_of(pos):
        raw = pos / ratio
        p = 1
        for off, pno in page_at:
            if off <= raw:
                p = pno
        return p

    hits = []
    for ind in indicators:
        if not ind["text"]:
            continue
        probe = re.escape(re.sub(r"\s+", " ", ind["text"])[:60]).replace(r"\ ", r"\s*")
        m = re.search(probe, flat, re.I)
        if m:
            hits.append((m.start(), ind["id"]))
    hits.sort()
    out = {}
    for k, (pos, iid) in enumerate(hits):
        end = hits[k + 1][0] if k + 1 < len(hits) else min(len(flat), pos + 4000)
        out[iid] = {"text": flat[pos:min(end, pos + 4000)].strip(), "page": page_of(pos)}
    return out


def preview(conn, parsed):
    cur = {i["id"]: i for i in all_indicators(conn)}
    rows = []
    for iid, v in sorted(parsed.items(), key=lambda kv: cur.get(kv[0], {}).get("sort_key", 0)):
        c = cur.get(iid)
        if not c:
            continue
        rows.append({"id": iid, "new": v["text"], "page": v["page"], "old": c["text"],
                     "changed": (c["text"] or "").strip() != v["text"].strip()})
    return rows


def apply_qss(conn, parsed, filename, user, ids=None):
    n = 0
    for iid, v in parsed.items():
        if ids is not None and iid not in ids:
            continue
        cur = conn.execute("SELECT text FROM indicators WHERE id=?", (iid,)).fetchone()
        old = cur["text"] if cur else None
        update_indicator_text(conn, iid, v["text"], f"OLC QSS PDF: {filename}, p. {v['page']}", user,
                              alt_text=(old if old and old.strip() != v["text"].strip() else ""))
        n += 1
    audit(conn, user, "import_qss", filename, {"updated": n})
    conn.commit()
    return n


def apply_handbook(conn, parsed, filename, user):
    for iid, v in parsed.items():
        update_indicator_text(conn, iid, None, None, user, handbook=v["text"],
                              handbook_source=f"OLC Handbook: {filename}, p. {v['page']}")
    audit(conn, user, "import_handbook", filename, {"updated": len(parsed)})
    conn.commit()
    return len(parsed)
