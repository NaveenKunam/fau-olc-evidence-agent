"""High-level operations shared by the web UI, the command console and the seed script."""
import os
import re
import urllib.parse
from datetime import date

from . import ingest
from .analyze import process_document, store_document
from .assess import assess_all, snapshot
from .db import now, audit
import json


def _stale(doc_date):
    y = ingest.detect_year(doc_date)
    return bool(y and y < date.today().year - 6)


def add_bytes(conn, data, *, filename=None, source_url=None, ctype="", actor=None, classification="INTERNAL",
              authority=None, source_org=None, title=None, doc_type=None, doc_date=None, origin="upload",
              upload_dir=None, notes=None, llm=None, process=True):
    kind = ingest.detect_kind(filename or source_url or "", ctype)
    if not kind:
        raise ValueError("Unsupported file type. Supported: PDF, DOCX, XLSX, CSV, TXT/MD, HTML.")
    passages, full, pages, meta, links = ingest.extract(kind, data, base_url=source_url)
    if not passages:
        raise ValueError("No extractable text found (scanned image PDF? Run OCR first).")
    h = ingest.sha256(data)
    dup = conn.execute("SELECT id, title FROM documents WHERE sha256=?", (h,)).fetchone()
    if dup:
        return {"doc_id": dup["id"], "status": "duplicate", "title": dup["title"], "created": [], "links": links}
    title = title or ingest.guess_title(meta, filename, passages[0]["text"], source_url)
    if kind == "html" and source_url:
        kind_label = "url"
    else:
        kind_label = kind
    dtype = doc_type or ingest.detect_doc_type(title, filename or source_url or "", full, kind)
    ddate = doc_date or ingest.detect_date(filename or source_url or "", full, meta)
    draft = ingest.detect_draft(title, filename or urllib.parse.unquote(source_url or ""), full)
    auth = authority or (ingest.authority_for(source_url) if source_url else "FAU")
    org = source_org or {"FAU": "Florida Atlantic University", "STATE": "State of Florida / FLVC",
                         "ACCREDITOR": "Accreditor"}.get(auth)
    stored = None
    if upload_dir and filename:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(filename))
        stored = os.path.join(upload_dir, f"{h[:12]}_{safe}")
        with open(stored, "wb") as f:
            f.write(data)
    doc_id, status = store_document(
        conn, title=title, kind=kind_label, doc_type=dtype, data_hash=h, passages=passages, full_text=full, pages=pages,
        meta=meta, source_url=source_url, filename=filename, stored_path=stored, source_org=org, doc_date=ddate,
        draft_reasons=draft, authority=auth, classification=classification, origin=origin, actor=actor, notes=notes,
        stale=_stale(ddate))
    res = {"doc_id": doc_id, "status": status, "title": title, "created": [], "links": links, "passages": len(passages)}
    if process:
        r = process_document(conn, doc_id, actor, llm=llm)
        res["created"] = r["created"]
        res["duplicates"] = r["duplicates"]
    return res


def add_url(conn, url, allowed, **kw):
    data, ctype, final, last_mod = ingest.fetch_url(url, allowed)
    fname = os.path.basename(urllib.parse.urlparse(final).path) or None
    if not kw.get("title") and "html" in (ctype or ""):
        kw["title"] = _page_title(data, final)
    kind = ingest.detect_kind(fname or "", ctype)
    notes = f"Fetched {now()}" + (f"; Last-Modified: {last_mod}" if last_mod else "")
    return add_bytes(conn, data, filename=fname if kind != "html" else None, source_url=final, ctype=ctype,
                     origin=kw.pop("origin", "url"), notes=notes, classification=kw.pop("classification", "PUBLIC"), **kw)


SITE_LABELS = {"online": "FAU Online", "elearning": "Florida Atlantic Online (eLearning)", "studentresources": "FAU Student Resources",
               "provost": "Office of the Provost", "oit": "FAU OIT", "library": "FAU Libraries", "policies": "FAU Policies"}


def _page_title(data, url):
    meta = ingest.extract_html(data, url)[3]
    t = (meta.get("title") or "").split("|")[0].strip()
    u = urllib.parse.urlparse(url)
    seg = (u.path.strip("/").split("/") or [""])[0]
    label = SITE_LABELS.get(seg) if (u.hostname or "").endswith("fau.edu") else None
    if not t:
        return url
    return f"{label}: {t}" if label and label.split(" (")[0].lower() not in t.lower() else t


def checkpoint(conn, label, actor, clearance="RESTRICTED"):
    res = assess_all(conn, clearance)
    evid = [r[0] for r in conn.execute("SELECT id FROM evidence")]
    conn.execute("INSERT INTO runs(label, created_at, created_by, snapshot_json) VALUES (?,?,?,?)",
                 (label, now(), actor, json.dumps({"indicators": snapshot(res), "evidence_ids": evid})))
    audit(conn, actor, "checkpoint", None, label)
    conn.commit()


def search_fau(conn, ind, allowed, seeds, max_pages=30, fetch=None):
    """Targeted crawl of approved FAU seed pages; returns candidate URLs ranked for this indicator.
    Nothing is stored: a reviewer chooses which candidates to PROCESS."""
    from .indicators import derived_vocab
    from .analyze import score_passage
    anchors, terms = derived_vocab(ind)
    fetch = fetch or ingest.fetch_url
    queue, seen, results = list(seeds), set(), []
    known = {r[0] for r in conn.execute("SELECT source_url FROM documents WHERE source_url IS NOT NULL")}
    hint_words = [re.sub(r"[^a-z]", "", a) for a in anchors] + [t.split()[0] for t in terms]
    while queue and len(seen) < max_pages:
        url = queue.pop(0)
        if url in seen or not ingest.domain_allowed(url, allowed):
            continue
        seen.add(url)
        try:
            data, ctype, final, _ = fetch(url, allowed)
        except Exception:
            continue
        if "html" not in ctype:
            continue
        blocks, full, _, meta, links = ingest.extract_html(data, final)
        best, snippet = 0, ""
        for _, _, text in blocks:
            s, a, t = score_passage(text, anchors, terms)
            if s > best:
                best, snippet = s, text[:300]
        if best >= 6:
            results.append({"url": final, "title": meta.get("title") or final, "score": best, "snippet": snippet,
                            "already_ingested": final in known})
        for l in links:
            low = l.lower()
            if l not in seen and ingest.domain_allowed(l, allowed) and any(w and w in low for w in hint_words):
                queue.insert(0, l)
            elif l not in seen and ingest.domain_allowed(l, allowed) and len(queue) < 200:
                queue.append(l)
    results.sort(key=lambda r: -r["score"])
    return results[:12], len(seen)
