"""Markdown reports: initial review, gaps, owner packets, meeting brief, what changed, verify, submission draft."""
import json
import re
from collections import OrderedDict

from .assess import assess_all, assess_one, load_evidence, summary, display_status
from .db import now

DISCLAIMER = ("_Preliminary AI assessment for FAU OLC team discussion. It is **not** an official FAU score. "
              "Official 0-3 scores are entered only by human reviewers._")

OWNER_ALIASES = {
    "OIT": ["information technology", "oit"],
    "PROVOST": ["provost"],
    "FACULTY SENATE": ["faculty senate"],
    "COCE": ["coce", "florida atlantic online"],
    "FAO": ["florida atlantic online", "coce"],
    "IE": ["institutional effectiveness"],
    "IEA": ["institutional effectiveness"],
    "REGISTRAR": ["registrar"],
    "FINANCIAL AID": ["financial aid"],
    "LIBRARY": ["librar"], "LIBRARIES": ["librar"],
    "SAS": ["accessibility"], "ACCESSIBILITY": ["accessibility"],
    "STUDENT AFFAIRS": ["student affairs"],
    "CAPS": ["caps", "counseling"],
    "BUDGET": ["budget"],
    "COMMUNICATIONS": ["communications"],
    "COLLEGES": ["colleges"],
    "OESS": ["oess", "enrollment & student success"],
    "CAREER": ["career"],
    "ADVISING": ["advising"],
    "ADMISSIONS": ["admissions"],
    "INSTRUCTIONAL DESIGN": ["instructional design"],
    "FACULTY DEVELOPMENT": ["faculty development"],
}


def owner_matches(owner_key, owner_name):
    key = owner_key.strip().upper()
    pats = OWNER_ALIASES.get(key, [key.lower()])
    return any(p in owner_name.lower() for p in pats)


def _ind_line(x):
    i = x["indicator"]
    txt = i["text"] or "(wording pending: load QSS PDF)"
    return f"**{i['id']}** {txt[:140]}{'...' if len(txt) > 140 else ''}"


def all_owners(results):
    owners = OrderedDict()
    for x in results:
        if (x["status"] in ("STRONG",) and not x["needed"]) or (x["indicator"]["pending"] and not x["needed"]):
            continue
        for o in x["owners"]:
            owners.setdefault(o, []).append(x)
    return owners


def initial_review(conn, clearance, docs_note=None):
    res = assess_all(conn, clearance)
    s = summary(res)
    docs = [dict(r) for r in conn.execute("SELECT * FROM documents ORDER BY id")]
    out = [f"# FAU OLC EVIDENCE AGENT - INITIAL REVIEW", f"_Generated {now()}_", "", DISCLAIMER, ""]
    out += ["## Summary", "",
            "| Measure | Count |", "|---|---|",
            f"| OLC indicator slots loaded | {s['total']} |",
            f"| ...with wording available (from FAU matrix) | {s['total'] - sum(1 for x in res if x['indicator']['pending'])} |",
            f"| ...wording pending (QSS PDF not yet loaded) | {sum(1 for x in res if x['indicator']['pending'])} |",
            f"| Indicators with any evidence | {s['with_evidence']} |",
            f"| Strong evidence (reaches required level, strong source) | {s['strong']} |",
            f"| Partial evidence | {s['partial']} |",
            f"| Requiring verification (only unverified/draft/prior-matrix support) | {s['needs_verification']} |",
            f"| No evidence yet | {s['missing'] + s['wording_pending']} |",
            f"| Indicators relying on at least one unverified/draft source | {s['relying_unverified']} |",
            f"| Evidence items awaiting human review | {s['backlog']} |",
            f"| Preliminary points (AI, advisory) | {s['prelim_points']} / {s['max_points']} |",
            f"| Official points | {'none entered' if s['official_points'] is None else s['official_points']} |",
            "", f"Evidence coverage: {s['coverage_pct']}% of indicators have at least one mapped item. "
            "_Coverage is not an OLC score._", ""]
    # top gaps
    gaps = sorted([x for x in res if x["status"] != "STRONG" and not x["indicator"]["pending"]],
                  key=lambda x: (x["prelim"], -len([f for f in x["flags"] if f["severity"] == "HIGH"])))
    out += ["## Top evidence gaps", ""]
    for x in gaps[:15]:
        out.append(f"- {_ind_line(x)}  \n  _{x['status']} · prelim {x['prelim']} · has {x['achieved'] or 'nothing'}, needs {x['required']}_  \n  {x['headline']}")
    out.append("")
    # new evidence
    ai = conn.execute("SELECT COUNT(*) FROM evidence WHERE origin LIKE 'ai_%'").fetchone()[0]
    mx = conn.execute("SELECT COUNT(*) FROM evidence WHERE origin='matrix'").fetchone()[0]
    out += ["## New evidence discovered", "",
            f"- {ai} passage-level evidence items mapped from source documents (each with document, page/section and verbatim passage).",
            f"- {mx} prior claims imported from the FAU OLC Evidence Matrix, all set to NEEDS VERIFICATION until a reviewer confirms them against a source passage.", ""]
    out += ["| Source | Type | Date | Status | Items mapped |", "|---|---|---|---|---|"]
    for d in docs:
        n = conn.execute("SELECT COUNT(*) FROM evidence WHERE document_id=?", (d["id"],)).fetchone()[0]
        st = "PROPOSED/DRAFT" if d["is_draft"] else ("SUPERSEDED" if d["superseded_by"] else "current")
        out.append(f"| {d['title'][:70]} | {d['doc_type']} | {d['doc_date'] or 'undated'} | {st} | {n} |")
    out.append("")
    # documents requiring verification
    out += ["## Documents requiring additional verification", ""]
    for d in docs:
        if d["is_draft"]:
            hinge = [x["indicator"]["id"] for x in res if x["status"] == "NEEDS VERIFICATION"
                     and any(h.startswith("E-") for h in x["have"]) and d["title"][:40] in x["headline"]]
            out.append(f"- **{d['title']}**: {d['version_note']}. Confirm this is the adopted, current version (Office of the Provost)."
                       + (f" **{len(hinge)} indicators depend on it for verification:** {', '.join(hinge)}." if hinge else ""))
    for d in docs:
        if d["doc_type"] == "PLAN":
            out.append(f"- **{d['title']}**: a unit strategic plan. It can substantiate intent only. Confirm institutional endorsement.")
        if d["authority"] == "STATE":
            out.append(f"- **{d['title']}**: state standard. Shows what the standard requires, not that FAU courses meet it.")
    if docs_note:
        out += [f"- {n}" for n in docs_note]
    out.append("")
    # owner requests
    out += ["## Evidence requests grouped by likely FAU owner", ""]
    for owner, xs in sorted(all_owners(res).items(), key=lambda kv: -len(kv[1])):
        out.append(f"### {owner} ({len(xs)} indicators)")
        for x in xs[:12]:
            ask = "; ".join(t for _, t in x["needed"][:2]) or "Confirm and verify existing evidence."
            out.append(f"- **{x['indicator']['id']}**: {ask}")
        out.append("")
    # immediate human review
    out += ["## Indicators requiring immediate human review", ""]
    urgent = [x for x in res if x["n_valid"] and (any(f["severity"] == "HIGH" for f in x["flags"]) or x["n_unverified"])]
    urgent.sort(key=lambda x: -sum(1 for f in x["flags"] if f["severity"] == "HIGH"))
    for x in urgent[:15]:
        hi = [f["message"] for f in x["flags"] if f["severity"] == "HIGH"][:2]
        out.append(f"- **{x['indicator']['id']}** ({x['n_review_backlog']} items to review): " + " ".join(hi))
    pend = [x["indicator"]["id"] for x in res if x["indicator"]["pending"]]
    if pend:
        out += ["", f"**Decision needed:** load the QSS PDF (Admin > OLC Rubric) to fill wording for {', '.join(pend)}."]
    return "\n".join(out)


def gap_report(conn, clearance, only_open=True):
    res = assess_all(conn, clearance)
    out = ["# SHOW GAPS: indicators with missing or incomplete evidence", f"_Generated {now()}_", "", DISCLAIMER, ""]
    cat = None
    for x in res:
        if only_open and x["status"] == "STRONG" and not x["flags"]:
            continue
        i = x["indicator"]
        if i["category"] != cat:
            cat = i["category"]
            out += [f"## {cat}", ""]
        out.append(f"### {i['id']}: {x['status']} (prelim {x['prelim']}, strength {x['strength']})")
        out.append(f"> {i['text'] or 'Wording pending: load QSS PDF.'}")
        out.append("")
        out.append(f"- **What do we have?** " + (f"{x['n_valid']} item(s); strongest level {x['achieved'] or 'none verified'}." if x['n_valid'] else "Nothing."))
        out.append(f"- **What does it prove?** " + ("; ".join(x["proves"]) if x["proves"] else "Nothing verified yet."))
        out.append(f"- **What does it NOT prove?** " + ("; ".join(x["not_proves"]) if x["not_proves"] else "No level gap; reviewer must confirm currency/adoption."))
        out.append(f"- **What is still missing?** {x['headline']}")
        if x["needed"]:
            out.append("- **Artifacts that would close the gap:**")
            out += [f"  - [{l}] {t}" for l, t in x["needed"]]
        out.append(f"- **Likely owner:** {', '.join(x['owners']) or 'TBD'}")
        out.append("")
    return "\n".join(out)


def owner_packet(conn, clearance, owner_key):
    res = assess_all(conn, clearance)
    rows = []
    names = set()
    for x in res:
        hit = [o for o in x["owners"] if owner_matches(owner_key, o)]
        if hit and (x["status"] != "STRONG" or x["needed"]):
            rows.append(x)
            names.update(hit)
    title = ", ".join(sorted(names)) or owner_key
    out = [f"# Evidence request packet: {title}", f"_Prepared by the FAU OLC review team, {now()}_", "",
           "The FAU OLC team is conducting the OLC Quality Scorecard self-review for the Administration of Online Programs. "
           "We are asking for the specific artifacts below. For each one, please send the document (or a link to where it "
           "lives), its date/version, and tell us whether it can be shared publicly or is internal/restricted.", ""]
    if not rows:
        out.append(f"No open evidence requests matched owner '{owner_key}'. Try: OIT, PROVOST, COCE, IE, REGISTRAR, LIBRARY, SAS, CAPS, BUDGET.")
        return "\n".join(out)
    out += ["| # | OLC indicator | Why we are asking | Please provide |", "|---|---|---|---|"]
    k = 0
    for x in rows:
        i = x["indicator"]
        why = x["headline"].replace("|", "/")
        for l, t in (x["needed"] or [("", "Confirm the current version of the evidence we hold")]):
            k += 1
            out.append(f"| {k} | {i['id']}: {(i['text'] or 'wording pending')[:90]} | {why[:220]} | {t} |")
    out += ["", "_Please do not send documents containing student-level personal data; aggregate reports are sufficient._"]
    return "\n".join(out)


def verify_report(conn, clearance, ind_id=None):
    res = assess_all(conn, clearance) if not ind_id else [assess_one(conn, clearance, ind_id)[0]]
    out = ["# VERIFY: skeptical OLC reviewer pass", f"_Generated {now()}_", "",
           "Questions asked of every indicator: Does the evidence address the whole indicator? Is it current and authoritative? "
           "Does it show implementation, measurement, improvement where required? Is it institution-wide? Is it a policy/plan or "
           "proof it was followed? Could an external reviewer verify it?", ""]
    for x in res:
        if x is None:
            out.append("Indicator not found.")
            continue
        if not x["flags"] and not ind_id:
            continue
        i = x["indicator"]
        out.append(f"## {i['id']}: {x['status']}, prelim {x['prelim']}")
        for f in x["flags"]:
            out.append(f"- **[{f['severity']}] {f['question']}** {f['message']}")
        if not x["flags"]:
            out.append("- No automatic challenges raised. A human reviewer should still confirm currency and adoption.")
        out.append("")
    return "\n".join(out)


def meeting_brief(conn, clearance):
    res = assess_all(conn, clearance)
    s = summary(res)
    runs = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    since = runs["created_at"] if runs else "1970-01-01"
    new_ev = conn.execute("SELECT evidence_code, indicator_id, title, strength FROM evidence WHERE added_at > ? ORDER BY id DESC LIMIT 15", (since,)).fetchall()
    ev = load_evidence(conn, clearance, include_rejected=False)
    strongest = sorted([e for e in ev if e["strength"] == "STRONG"], key=lambda e: -(e["match_score"] or 0))[:6]
    out = ["# OLC team meeting brief", f"_{now()}_", "", DISCLAIMER, "",
           "## Progress",
           f"- {s['with_evidence']}/{s['total']} indicators have evidence ({s['coverage_pct']}% coverage; not a score).",
           f"- Strong {s['strong']} · Partial {s['partial']} · Needs verification {s['needs_verification']} · Missing {s['missing']} · Wording pending {s['wording_pending']}.",
           f"- Human-reviewed indicators: {s['reviewed']} ({s['approved']} approved). Official scores entered: {s['official_count']}.",
           f"- Preliminary points (advisory): {s['prelim_points']}/{s['max_points']}.", "",
           f"## Newly discovered evidence (since last checkpoint {since})"]
    out += [f"- {r['evidence_code']} -> {r['indicator_id']} ({r['strength']}): {r['title']}" for r in new_ev] or ["- None since the last checkpoint."]
    out += ["", "## Strongest evidence"]
    out += [f"- {e['evidence_code']} -> {e['indicator_id']}: {e['summary'][:200]}" for e in strongest] or ["- No evidence currently rated STRONG."]
    gaps = sorted([x for x in res if x["status"] in ("MISSING", "NEEDS VERIFICATION", "PARTIAL") and not x["indicator"]["pending"]],
                  key=lambda x: x["prelim"])[:8]
    out += ["", "## Major gaps"] + [f"- {x['indicator']['id']}: {x['headline']}" for x in gaps]
    owners = all_owners(res)
    out += ["", "## Evidence requests to send"] + [f"- {o}: {len(xs)} indicators (run OWNER PACKET)" for o, xs in
                                                   sorted(owners.items(), key=lambda kv: -len(kv[1]))[:8]]
    decide = [x for x in res if x["review_status"] == "NEEDS DECISION" or x["indicator"]["pending"] or x["indicator"]["wording_conflict"]]
    out += ["", "## Indicators needing team decisions"]
    out += [f"- {x['indicator']['id']}: " + ("wording pending" if x["indicator"]["pending"] else
                                              "matrix has conflicting wording" if x["indicator"]["wording_conflict"] else "flagged NEEDS DECISION")
            for x in decide] or ["- None."]
    out += ["", "## Human review backlog",
            f"- {s['backlog']} evidence items are awaiting approve/reject.",
            f"- Indicators with the most unreviewed items: " + ", ".join(
                f"{x['indicator']['id']} ({x['n_review_backlog']})" for x in sorted(res, key=lambda x: -x["n_review_backlog"])[:8])]
    return "\n".join(out)


def what_changed(conn, clearance):
    runs = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 2").fetchall()
    res = assess_all(conn, clearance)
    cur = {x["indicator"]["id"]: x for x in res}
    if not runs:
        return "# WHAT CHANGED\n\nNo previous checkpoint exists. Use 'CHECKPOINT' to save one."
    base = runs[0]
    snap = json.loads(base["snapshot_json"])
    prev_ids = set(snap["evidence_ids"])
    new = conn.execute(f"SELECT evidence_code, indicator_id, title, strength, added_by, added_at FROM evidence ORDER BY id").fetchall()
    added = [r for r in new if r["evidence_code"] and int(r["evidence_code"][2:]) not in prev_ids]
    out = ["# WHAT CHANGED", f"Compared with checkpoint **{base['label']}** ({base['created_at']}).", ""]
    out += ["## Evidence added since then"] + ([f"- {r['evidence_code']} -> {r['indicator_id']} ({r['strength']}) from {r['title']}, added by {r['added_by']} {r['added_at']}" for r in added] or ["- None."])
    closed, moved = [], []
    for iid, old in snap["indicators"].items():
        x = cur.get(iid)
        if not x:
            continue
        if old["status"] != x["status"] or old["prelim"] != x["prelim"]:
            line = f"- {iid}: {old['status']} (prelim {old['prelim']}) -> {x['status']} (prelim {x['prelim']})"
            (closed if old["status"] in ("MISSING", "NEEDS VERIFICATION", "WORDING PENDING") and x["status"] in ("PARTIAL", "STRONG") or
             (old["status"] == "PARTIAL" and x["status"] == "STRONG") else moved).append(line)
    out += ["", "## Gaps closed or narrowed"] + (closed or ["- None."])
    out += ["", "## Other status changes"] + (moved or ["- None."])
    return "\n".join(out)


def submission_draft(conn, clearance, ind_id, llm=None):
    x, ev = assess_one(conn, clearance, ind_id)
    if not x:
        return f"Indicator {ind_id} not found."
    i = x["indicator"]
    appr = [e for e in ev if e["review_status"] == "APPROVED"]
    out = [f"# Submission draft: {i['id']}", f"> {i['text'] or 'Wording pending'}", "",
           "_Drafted only from evidence marked APPROVED by a human reviewer. Rejected, missing, draft or unverified evidence is never used._", ""]
    if not appr:
        out.append("**Cannot draft.** No evidence for this indicator is marked APPROVED. Approve evidence on the indicator page first.")
        return "\n".join(out)
    text = llm.draft_narrative(i, appr) if llm is not None else None
    if text:
        out += ["## Narrative (Claude-drafted from approved evidence; citations checked)", "", text, ""]
    else:
        out += ["## Narrative", ""]
        lines = []
        for e in appr:
            loc = f", p. {e['page']}" if e.get("page") else ""
            lines.append(f"{_sentence(e)} [{e['evidence_code']}: {e['title']}{loc}]")
        out.append("Florida Atlantic University addresses this indicator as follows. " + " ".join(lines))
        out.append("")
        remaining = [n for n in x["not_proves"]]
        if remaining:
            out.append("**Reviewer note (remove before submission):** approved evidence does not yet demonstrate " + "; ".join(remaining) + ".")
    out += ["", "## Supporting sources"]
    for e in appr:
        out.append(f"- {e['evidence_code']}: {e['title']} ({e.get('doc_url') or e.get('source_ref') or 'uploaded file'})"
                   f"{', p. ' + e['page'] if e.get('page') else ''}{', ' + e['section'] if e.get('section') else ''}. "
                   f"Passage: \"{(e['passage'] or '')[:300]}\"")
    if x["official"] is not None:
        out.append(f"\nOfficial score entered by reviewer: **{x['official']}**")
    return "\n".join(out)


def _sentence(e):
    p = (e["passage"] or "").strip()
    p = re.sub(r"^[•●\-\s]+", "", p)
    if len(p) > 320:
        p = p[:320].rsplit(" ", 1)[0] + "..."
    lead = {"PLAN": "The COCE 2025-2030 Strategic Plan commits to the following:", "POLICY": "University policy states:",
            "WEBPAGE": "FAU's published guidance states:"}.get(e.get("doc_type"), "Documentation shows:")
    return f"{lead} \"{p}\""


def indicator_brief(conn, clearance, ind_id):
    x, ev = assess_one(conn, clearance, ind_id)
    if not x:
        return f"Indicator {ind_id} not found."
    i = x["indicator"]
    out = [f"# INDICATOR {i['id']} ({i['category']})", f"> {i['text'] or 'Wording pending: load QSS PDF'}", "",
           f"**Status:** {x['status']} · **Strength:** {x['strength']} · **Has:** {x['achieved'] or 'nothing verified'} · **Needs:** {x['required']}",
           f"**AI preliminary score (advisory):** {x['prelim']} ({x['prelim_rationale']})",
           f"**Official score:** {x['official'] if x['official'] is not None else 'not entered'} · **Review:** {x['review_status']}", "",
           "## Evidence"]
    for e in ev:
        out.append(f"- {e['evidence_code']} [{display_status(e)} / {e['strength']} / {e['implementation_level']}] "
                   f"{e['title']}{' p.' + e['page'] if e.get('page') else ''}: \"{(e['passage'] or e['summary'] or '')[:260]}\"")
    if not ev:
        out.append("- None located.")
    out += ["", "## Gap", x["headline"], "", "## Skeptical review"] + [f"- [{f['severity']}] {f['message']}" for f in x["flags"]]
    return "\n".join(out)
