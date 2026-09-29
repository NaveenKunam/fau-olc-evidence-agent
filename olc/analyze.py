"""Evidence mapping engine.

Maps document passages to OLC indicators, classifies the implementation level each passage can
actually substantiate (INTENT / PROCESS / IMPLEMENTATION / MEASUREMENT / IMPROVEMENT), and rates
evidence strength. The engine is deliberately skeptical:

* A strategic plan can substantiate INTENT (occasionally a current-state PROCESS statement) - never
  implementation.
* A policy / memorandum / standard / service webpage can substantiate PROCESS - never
  implementation, measurement or effectiveness.
* Only records, reports, data and minutes can substantiate IMPLEMENTATION and above, and only when
  the passage itself contains implementation/measurement/improvement language.
* Every record keeps its document, page, section and the verbatim extracted passage.
"""
import hashlib
import json
import re

from .db import now, audit
from .indicators import LEVELS, LEVEL_RANK, all_indicators, derived_vocab

INTENT_RX = re.compile(r"\b(will|plans? to|aims? to|aspires?|goals?|objectives?|strateg(y|ies)|targets?|measurable outcomes?|"
                       r"expand|establish|develop|create|launch|enhance|strengthen|promote|encourage|seek|strive|increase|"
                       r"at least|annually by|by year \d|over (three|five|\d) years)\b", re.I)
PROCESS_RX = re.compile(r"\b(must|shall|required|requires?|is responsible|are responsible|policy|policies|procedures?|process(es)?|"
                        r"guidelines?|is expected|are expected|standards?|provides?|offers?|available|is conducted|are conducted|"
                        r"administered|follows?|is assigned|are assigned|in place|designat(ed|ion))\b", re.I)
IMPL_RX = re.compile(r"(\bcompleted\b|\bwere (trained|reviewed|conducted|held|approved|developed|designated|launched|offered|served)\b|"
                     r"\bwas (approved|conducted|completed|implemented|launched|adopted|held)\b|\bhas been (implemented|adopted|approved)\b|"
                     r"\bin (fall|spring|summer) 20\d\d\b|\bduring (the )?20\d\d\b|\b20\d\d-(20)?\d\d (academic |fiscal )?year\b|"
                     r"\bminutes\b|\bapproved on\b|\battendance\b|\bparticipants\b|"
                     r"\b\d[\d,]* (faculty|courses|students|sessions|workshops|reviews|tickets|programs) (completed|were|attended|participated|received|reviewed))",
                     re.I)
MEAS_RX = re.compile(r"(\d+(\.\d+)?\s?%\s*(of|uptime|availability|response|retention|satisfaction|increase|decrease|were|was)|"
                     r"\bresponse rate\b|\bsurvey results\b|\bresults (show|showed|indicate|indicated)\b|\bdata (show|showed|indicate|indicated)\b|"
                     r"\bmean (score|rating)\b|\baverage (rating|score)\b|\buptime (was|of)\b|\bretention rate (was|of|is|increased|decreased)\b|"
                     r"\bbenchmarked\b|\bn\s?=\s?\d+)", re.I)
IMPROVE_RX = re.compile(r"(\bas a result of\b|\bbased on (the )?(findings|results|feedback|data|survey)\b|"
                        r"\bin response to (the )?(findings|feedback|results|survey)\b|\brevised (the|our)\b|\bchanges were made\b|"
                        r"\bimprovements? (were|was) (made|implemented)\b|\bclosing the loop\b|\baction plan (was|were) implemented\b)", re.I)
PLAN_SECTION_RX = re.compile(r"(goal|objective|strateg|measurable outcome|alignment|responsible staff)", re.I)
UNIT_SCOPE_RX = re.compile(r"\b(non-credit|continuing education|osher|olli|lifelong learning|ceus?|workforce training)\b", re.I)
ONLINE_RX = re.compile(r"\b(online|distance|e-?learning|remote|virtual|canvas)\b", re.I)
TOC_RX = re.compile(r"\.{8,}")

DOC_CAP = {"PLAN": "PROCESS", "POLICY": "PROCESS", "WEBPAGE": "PROCESS", "STANDARD": "PROCESS", "MATRIX": "PROCESS"}


def _cues(rx, text, limit=3):
    out = []
    for m in rx.finditer(text):
        s = m.group(0).strip()
        if s.lower() not in [o.lower() for o in out]:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def classify_level(text, doc_type, section):
    """Return (level, rationale). Never exceeds what the document type can prove."""
    imp, meas, impl = _cues(IMPROVE_RX, text), _cues(MEAS_RX, text), _cues(IMPL_RX, text)
    proc, intent = _cues(PROCESS_RX, text), _cues(INTENT_RX, text)
    if imp:
        raw, cues = "IMPROVEMENT", imp
    elif meas:
        raw, cues = "MEASUREMENT", meas
    elif impl:
        raw, cues = "IMPLEMENTATION", impl
    elif proc and not (intent and len(intent) > len(proc)):
        raw, cues = "PROCESS", proc
    elif intent:
        raw, cues = "INTENT", intent
    else:
        raw, cues = ("INTENT" if doc_type == "PLAN" else "PROCESS"), []

    if doc_type == "DATA" and re.search(r"\d+(\.\d+)?\s?%|\b\d{2,}\b", text) and LEVEL_RANK[raw] < LEVEL_RANK["MEASUREMENT"]:
        return "MEASUREMENT", "Data row with recorded values in a data file (measurement). Confirm the data source, period and definitions."
    if doc_type == "PLAN":
        in_plan_section = bool(section and PLAN_SECTION_RX.search(section))
        if intent or in_plan_section or raw in ("IMPLEMENTATION", "MEASUREMENT", "IMPROVEMENT"):
            return "INTENT", (f"Strategic-plan language ({', '.join(repr(c) for c in (intent or cues)[:3]) or 'goal/strategy section'})"
                              f"{' in section ' + repr(section) if section else ''}. A plan documents intent; it does not show the "
                              "activity has happened or been measured.")
        return "PROCESS", ("Present-tense description of a current service/practice inside a strategic plan "
                           f"({', '.join(repr(c) for c in proc[:3])}). Treated as a description of an existing process only; "
                           "a plan is not proof of implementation.")
    cap = DOC_CAP.get(doc_type)
    if cap and LEVEL_RANK[raw] > LEVEL_RANK[cap]:
        what = {"POLICY": "a policy/memorandum", "WEBPAGE": "a service webpage", "STANDARD": "a standard/rubric",
                "MATRIX": "a prior matrix claim"}[doc_type]
        return cap, (f"Passage contains implementation-style wording ({', '.join(repr(c) for c in cues[:3])}), but it comes from "
                     f"{what}. That establishes a stated requirement or available service (PROCESS), not proof it is followed "
                     "or effective.")
    if doc_type in ("POLICY", "STANDARD") and raw == "INTENT":
        return "PROCESS", "Requirement/expectation language in a policy or standard document (treated as a stated PROCESS)."
    if doc_type == "WEBPAGE":
        return "PROCESS", ("Service/process description on a public webpage"
                           f"{' (' + ', '.join(repr(c) for c in proc[:3]) + ')' if proc else ''}. Shows availability, "
                           "not usage or effectiveness.")
    label = {"IMPROVEMENT": "closed-loop improvement", "MEASUREMENT": "measurement/results",
             "IMPLEMENTATION": "implementation/completion", "PROCESS": "process/requirement", "INTENT": "intent/plan"}[raw]
    return raw, (f"Contains {label} language ({', '.join(repr(c) for c in cues[:3])})." if cues
                 else "No implementation, measurement or improvement language; treated as a process description.")


def score_passage(text, anchors, terms):
    low = text.lower()
    a_hits = [a for a in anchors if re.search(a, low)]
    t_hits = [t for t in terms if t.lower() in low]
    score = 3 * len(a_hits) + len(t_hits)
    if ONLINE_RX.search(low):
        score += 1
    if UNIT_SCOPE_RX.search(low) and not ONLINE_RX.search(low):
        score -= 4
    return score, a_hits, t_hits


def qualifies(score, a_hits, t_hits, text="", doc_type=None):
    if not a_hits:
        return False
    if doc_type in ("DATA", "REPORT", "MINUTES") and re.search(r"\d", text) and score >= 5:
        return True   # records and data rows are short but high-value
    return (score >= 7 or (score >= 6 and len(t_hits) >= 3)) and (len(a_hits) >= 2 or len(t_hits) >= 2)


CATEGORY_MUST = {"FAC": [r"faculty", r"instructor"], "LEA": [r"student", r"learner"]}


def must_ok(text, ind):
    low = text.lower()
    must = ind["config"].get("must") or CATEGORY_MUST.get(ind["category_code"])
    return not must or any(re.search(m, low) for m in must)


def is_navigation(text):
    """Menus, link lists and bare headings are not evidence."""
    t = text.strip()
    if len(t) < 60 and not re.search(r"\d", t):
        return True
    words = t.split()
    caps = sum(1 for w in words if w[:1].isupper())
    return not re.search(r"[.:;!?]", t) and caps / max(len(words), 1) > 0.55


def strength_and_status(level, required, score, doc):
    lvl, req = LEVEL_RANK[level], LEVEL_RANK[required]
    auth = doc["authority"]
    if lvl >= req and score >= 9 and auth == "FAU" and not doc["is_draft"]:
        strength = "STRONG"
    elif (lvl >= req and score >= 6) or (lvl == req - 1 and lvl >= 2 and score >= 9):
        strength = "MODERATE"
    else:
        strength = "WEAK"
    if doc["is_draft"]:
        status = "PROPOSED/DRAFT"
        if strength == "STRONG":
            strength = "MODERATE"
    elif doc["is_stale"] or doc["superseded_by"]:
        status = "SUPERSEDED/STALE"
        strength = "WEAK"
    elif auth in ("OTHER", "UNKNOWN"):
        status = "NEEDS VERIFICATION"
    elif lvl >= req:
        status = "FOUND"
    else:
        status = "PARTIAL"
    return strength, status


def gap_note(level, ind):
    req = ind["required_level"]
    lvl, r = LEVEL_RANK[level], LEVEL_RANK[req]
    arts = ind["config"].get("artifacts") or []
    next_art = next((t for (l, t) in arts if LEVEL_RANK[l] > lvl), arts[0][1] if arts else None)
    if lvl >= r:
        missing = (f"Passage reaches the level this indicator needs ({req}). A reviewer still needs to confirm it is "
                   "current, adopted and applies institution-wide.")
    else:
        gaps = LEVELS[lvl:r]
        missing = (f"Passage establishes {level} only; the indicator needs {req}. Not shown: {', '.join(gaps)}.")
    return missing, next_art


def fingerprint(ind_id, text):
    norm = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return hashlib.sha1(f"{ind_id}|{norm}".encode()).hexdigest()


def next_code(conn):
    r = conn.execute("SELECT MAX(id) FROM evidence").fetchone()[0] or 0
    return f"E-{r + 1:04d}"


def summarize(doc, p):
    loc = []
    if p["page"]:
        loc.append(f"p. {p['page']}" if str(p["page"]).isdigit() else str(p["page"]))
    if p["section"]:
        loc.append(f"section '{p['section'][:70]}'")
    txt = p["text"]
    if len(txt) > 230:
        txt = txt[:230].rsplit(" ", 1)[0] + "..."
    return f"{doc['title']}{' (' + ', '.join(loc) + ')' if loc else ''} states: \"{txt}\""


EVIDENCE_TYPE = {"PLAN": "Strategic plan", "POLICY": "Policy / memorandum", "WEBPAGE": "Public webpage",
                 "STANDARD": "Standard / rubric", "REPORT": "Report / results", "DATA": "Data file",
                 "MINUTES": "Minutes / governance record", "OTHER": "Document", "MATRIX": "Prior matrix claim"}


def process_document(conn, doc_id, actor, llm=None, per_indicator=3):
    """(Re)map a document against all indicators. Preserves every human-touched record."""
    doc = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
    passages = [dict(r) for r in conn.execute("SELECT * FROM passages WHERE document_id=? ORDER BY seq", (doc_id,))]
    removed = conn.execute(
        "DELETE FROM evidence WHERE document_id=? AND review_status='NOT REVIEWED' AND human_edited=0 AND origin LIKE 'ai_%'",
        (doc_id,)).rowcount
    kept_fp = {r[0] for r in conn.execute("SELECT fingerprint FROM evidence WHERE fingerprint IS NOT NULL")}

    candidates = []
    for ind in all_indicators(conn):
        if ind["pending"]:
            continue
        anchors, terms = derived_vocab(ind)
        scored = []
        for p in passages:
            if TOC_RX.search(p["text"]) or is_navigation(p["text"]) or not must_ok(p["text"], ind):
                continue
            ctx = (p["section"] + " :: " if p["section"] else "") + p["text"]
            s, a, t = score_passage(ctx, anchors, terms)
            if qualifies(s, a, t, p["text"], doc["doc_type"]):
                scored.append((s, p, a, t))
        scored.sort(key=lambda x: -x[0])
        picked, used = [], set()
        floor = scored[0][0] * 0.6 if scored else 0
        for s, p, a, t in scored:
            if s < floor:
                break
            if p["seq"] in used or (p["seq"] - 1) in used or (p["seq"] + 1) in used:
                continue
            picked.append((s, p, a, t))
            used.add(p["seq"])
            if len(picked) >= per_indicator:
                break
        for s, p, a, t in picked:
            candidates.append({"ind": ind, "score": s, "p": p, "a": a, "t": t})

    if llm is not None and candidates:
        candidates = llm.review_candidates(doc, candidates)

    created, dupes = [], 0
    for c in candidates:
        ind, p, s = c["ind"], c["p"], c["score"]
        fp = fingerprint(ind["id"], p["text"])
        if fp in kept_fp:
            dupes += 1
            continue
        level, lvl_why = classify_level(p["text"], doc["doc_type"], p["section"])
        lowered = False
        if c.get("llm_level") and LEVEL_RANK[c["llm_level"]] < LEVEL_RANK[level]:
            level, lvl_why = c["llm_level"], f"Claude review lowered the level: {c.get('llm_rationale', '')} (rule engine: {lvl_why})"
            lowered = True
        strength, status = strength_and_status(level, ind["required_level"], s, doc)
        missing, next_art = gap_note(level, ind)
        why = (f"Matched indicator concepts {', '.join(repr(x.replace(chr(92), '')) for x in c['a'])}"
               f"{' plus ' + ', '.join(repr(x) for x in c['t'][:6]) if c['t'] else ''} (match score {s}). "
               f"Indicator requires {ind['required_level']}; this passage supports {level}.")
        if c.get("llm_rationale") and not lowered:
            why += f" Claude review: {c['llm_rationale']}"
        code = next_code(conn)
        conn.execute(
            """INSERT INTO evidence(evidence_code, indicator_id, document_id, passage_id, title, evidence_type, source_org,
               source_ref, doc_date, page, section, passage, summary, mapping_rationale, implementation_level, level_rationale,
               match_score, ai_status, strength, missing_note, next_artifact, likely_owner, origin, workflow_stage,
               classification, fingerprint, added_by, added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, ind["id"], doc_id, p["id"], doc["title"], EVIDENCE_TYPE.get(doc["doc_type"], "Document"), doc["source_org"],
             doc["source_url"] or doc["filename"], doc["doc_date"], p["page"], p["section"], p["text"], summarize(doc, p), why,
             level, lvl_why, s, status, strength, missing, next_art, "; ".join(ind["config"].get("owners") or []),
             "ai_llm" if c.get("llm_checked") else "ai_rules", "NEEDS HUMAN REVIEW", doc["classification"], fp, actor, now()))
        kept_fp.add(fp)
        created.append((code, ind["id"]))

    corroborate_matrix_claims(conn, doc)
    conn.execute("UPDATE documents SET processed_at=? WHERE id=?", (now(), doc_id))
    audit(conn, actor, "process_source", f"doc:{doc_id}",
          {"created": len(created), "replaced_unreviewed": removed, "duplicates_skipped": dupes})
    conn.commit()
    return {"created": created, "removed": removed, "duplicates": dupes, "doc": doc}


def corroborate_matrix_claims(conn, doc):
    keys = [k for k in (doc.get("source_url"), doc.get("filename")) if k]
    if not keys:
        return
    for m in conn.execute("SELECT id, indicator_id, source_ref FROM evidence WHERE origin='matrix'").fetchall():
        if not m["source_ref"] or not any(k in m["source_ref"] for k in keys):
            continue
        codes = [r[0] for r in conn.execute(
            "SELECT evidence_code FROM evidence WHERE document_id=? AND indicator_id=? AND origin LIKE 'ai_%'",
            (doc["id"], m["indicator_id"]))]
        conn.execute("UPDATE evidence SET corroborated_by=? WHERE id=?",
                     (", ".join(codes) if codes else "No matching passage located in cited source", m["id"]))


def store_document(conn, *, title, kind, doc_type, data_hash, passages, full_text, pages, meta, source_url=None,
                   filename=None, stored_path=None, source_org=None, doc_date=None, draft_reasons=None,
                   authority="UNKNOWN", classification="INTERNAL", origin="upload", actor=None, notes=None, stale=False):
    """Insert a document (or detect a duplicate). Returns (doc_id, status) where status in new|duplicate|new_version."""
    dup = conn.execute("SELECT id FROM documents WHERE sha256=?", (data_hash,)).fetchone()
    if dup:
        return dup["id"], "duplicate"
    prior = None
    if source_url:
        prior = conn.execute("SELECT id FROM documents WHERE source_url=? AND superseded_by IS NULL ORDER BY id DESC",
                             (source_url,)).fetchone()
    cur = conn.execute(
        """INSERT INTO documents(title, kind, doc_type, source_org, source_url, filename, stored_path, sha256, doc_date,
           version_note, is_draft, is_stale, authority, classification, origin, page_count, char_count, notes, added_by, added_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (title, kind, doc_type, source_org, source_url, filename, stored_path, data_hash, doc_date,
         "; ".join(draft_reasons) if draft_reasons else None, 1 if draft_reasons else 0, 1 if stale else 0,
         authority, classification, origin, pages, len(full_text or ""), notes, actor, now()))
    doc_id = cur.lastrowid
    conn.executemany("INSERT INTO passages(document_id, seq, page, section, text) VALUES (?,?,?,?,?)",
                     [(doc_id, i, p["page"], p["section"], p["text"]) for i, p in enumerate(passages)])
    status = "new"
    if prior:
        conn.execute("UPDATE documents SET superseded_by=? WHERE id=?", (doc_id, prior["id"]))
        conn.execute("""UPDATE evidence SET ai_status='SUPERSEDED/STALE', strength='WEAK'
                        WHERE document_id=? AND review_status='NOT REVIEWED' AND human_edited=0""", (prior["id"],))
        status = "new_version"
    audit(conn, actor, "add_source", f"doc:{doc_id}", {"title": title, "status": status, "passages": len(passages)})
    conn.commit()
    return doc_id, status
