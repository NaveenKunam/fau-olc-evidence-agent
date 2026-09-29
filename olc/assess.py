"""Indicator-level assessment: gap analysis, skeptical verification, preliminary (never official) score."""
import json
import re
from datetime import date

from .db import visible_classes
from .indicators import LEVELS, LEVEL_RANK, all_indicators, get_indicator

NOT_PROVEN = {
    "PROCESS": "that a defined policy, procedure or service actually exists for this requirement",
    "IMPLEMENTATION": "that the process is actually carried out (no records, completed reviews, logs or rosters located)",
    "MEASUREMENT": "that effectiveness is measured (no data, results or metrics located)",
    "IMPROVEMENT": "that results are used to make changes (no closed-loop improvement documentation located)",
}
PROVES = {
    "INTENT": "Documented intent/plan",
    "PROCESS": "A stated requirement, policy, process or available service",
    "IMPLEMENTATION": "The process is being carried out",
    "MEASUREMENT": "Effectiveness is measured",
    "IMPROVEMENT": "Results are used for improvement",
}
STRENGTH_RANK = {"NONE": 0, "WEAK": 1, "MODERATE": 2, "STRONG": 3}
UNVERIFIED = ("NEEDS VERIFICATION", "PROPOSED/DRAFT", "SUPERSEDED/STALE")
EFFECTIVE_RX = re.compile(r"\b(effective|effectively|reliab|sufficient|timely|measurable)\b", re.I)
REVIEW_RX = re.compile(r"\b(periodically|reviewed|regularly|updated|improv|continuous)\b", re.I)


def load_evidence(conn, clearance, indicator_id=None, include_rejected=True):
    cls = visible_classes(clearance)
    q = f"""SELECT e.*, d.doc_type, d.authority, d.is_draft, d.source_url AS doc_url, d.title AS doc_title, d.filename AS doc_filename,
                   d.doc_date AS d_date, d.superseded_by
            FROM evidence e LEFT JOIN documents d ON d.id = e.document_id
            WHERE e.classification IN ({','.join('?' * len(cls))})"""
    args = list(cls)
    if indicator_id:
        q += " AND e.indicator_id=?"
        args.append(indicator_id)
    if not include_rejected:
        q += " AND e.review_status!='REJECTED'"
    q += " ORDER BY e.review_status='APPROVED' DESC, e.match_score DESC, e.id"
    return [dict(r) for r in conn.execute(q, args)]


def display_status(e):
    if e["review_status"] in ("APPROVED", "REJECTED"):
        return e["review_status"]
    return e["ai_status"]


def _year(s):
    m = re.search(r"(19|20)\d{2}", s or "")
    return int(m.group(0)) if m else None


def assess(ind, evidence, review=None):
    review = review or {}
    req = ind["required_level"]
    r = LEVEL_RANK[req]
    valid = [e for e in evidence if e["review_status"] != "REJECTED"]
    verified = [e for e in valid if e["review_status"] == "APPROVED" or e["ai_status"] in ("FOUND", "PARTIAL")]
    unverified = [e for e in valid if e not in verified]
    approved = [e for e in valid if e["review_status"] == "APPROVED"]

    achieved = max((e["implementation_level"] for e in verified), key=lambda l: LEVEL_RANK[l], default=None)
    a = LEVEL_RANK.get(achieved, 0)
    best = max((e["strength"] for e in verified), key=lambda s: STRENGTH_RANK[s], default="NONE")
    if not verified and valid:
        best = "WEAK"

    # ---------------- indicator status
    if ind["pending"] and not valid:
        status = "WORDING PENDING"
    elif not valid:
        status = "MISSING"
    elif not verified:
        status = "NEEDS VERIFICATION"
    elif a >= r and best == "STRONG":
        status = "STRONG"
    else:
        status = "PARTIAL"

    # ---------------- skeptical verification flags
    flags = []

    def flag(sev, q, msg):
        flags.append({"severity": sev, "question": q, "message": msg})

    if ind["pending"]:
        flag("HIGH", "Is the requirement known?", "Indicator wording has not been loaded from the OLC QSS PDF. Load it before mapping or scoring.")
    if ind.get("wording_conflict"):
        flag("MEDIUM", "Is the requirement known?", "The FAU matrix contains two different wordings for this indicator. Confirm the current QSS wording.")
    if valid:
        if a < r:
            if a <= LEVEL_RANK["INTENT"] and verified:
                flag("HIGH", "Is this a plan or proof the plan was implemented?",
                     "The strongest verified evidence documents INTENT only (plan language). Nothing shows it has been done.")
            elif a == LEVEL_RANK["PROCESS"] and r >= LEVEL_RANK["IMPLEMENTATION"]:
                flag("HIGH", "Is this a policy or proof the policy is followed?",
                     "Evidence establishes a stated requirement/process or available service, but nothing shows it is followed in practice.")
            if r >= LEVEL_RANK["MEASUREMENT"] and a < LEVEL_RANK["MEASUREMENT"]:
                flag("HIGH", "Does the indicator require measurement?",
                     "This indicator needs measured results (data, metrics, survey or audit results). None located.")
            if r >= LEVEL_RANK["IMPROVEMENT"]:
                flag("HIGH", "Does the indicator require continuous improvement?",
                     "This indicator needs closed-loop evidence (results -> documented change). None located.")
        if EFFECTIVE_RX.search(ind.get("text") or "") and a < LEVEL_RANK["MEASUREMENT"]:
            flag("HIGH", "Does the evidence show actual outcomes?",
                 "Wording asks for effectiveness/reliability/sufficiency/timeliness. A description that a service exists does not prove that.")
        if REVIEW_RX.search(ind.get("text") or "") and not any(LEVEL_RANK[e["implementation_level"]] >= 3 for e in verified):
            flag("MEDIUM", "Is it current and periodically reviewed?",
                 "Wording requires periodic review/updating. No review history (dates, minutes, revision log) located.")
        types = {e["doc_type"] for e in verified if e["doc_type"]}
        if types and types <= {"PLAN"}:
            flag("HIGH", "Is this a plan or proof the plan was implemented?", "All verified evidence comes from a strategic plan.")
        if types and types <= {"POLICY", "STANDARD"} and r > LEVEL_RANK["PROCESS"]:
            flag("HIGH", "Is this a policy or proof the policy is followed?", "All verified evidence comes from policy/standard documents.")
        if types and types <= {"WEBPAGE"} and r > LEVEL_RANK["PROCESS"]:
            flag("MEDIUM", "Is this a service webpage or proof of effectiveness?", "All verified evidence is public webpage content.")
        if any(e["is_draft"] for e in valid):
            flag("HIGH", "Is it current and authoritative?",
                 "At least one source is marked proposed/draft (e.g., filename 'proposed-revisions'). Confirm the adopted, current version.")
        if any(e["authority"] == "STATE" for e in valid):
            flag("MEDIUM", "Is it authoritative for FAU?",
                 "Some evidence is a state (FLVC) standard. It defines the standard; it does not show FAU courses meet it.")
        if unverified and not verified:
            flag("HIGH", "Would an external OLC reviewer be able to verify the claim?",
                 "Only unverified claims (prior matrix statements, drafts or non-authoritative sources) support this indicator.")
        undated = [e for e in verified if not (e.get("doc_date") or e.get("d_date"))]
        if undated:
            flag("LOW", "Is it current?", f"{len(undated)} evidence item(s) have no document date (typical for webpages). Record the access/revision date.")
        old = [e for e in verified if _year(e.get("doc_date") or e.get("d_date")) and _year(e.get("doc_date") or e.get("d_date")) < date.today().year - 4]
        if old:
            flag("MEDIUM", "Is it current?", f"{len(old)} evidence item(s) are dated more than 4 years ago.")
        unit = [e for e in verified if re.search(r"\b(COCE|Center for Online)\b", (e.get("doc_title") or "") + " " + (e.get("passage") or ""))]
        if ind["category_code"] == "INS" and unit and len(unit) == len(verified):
            flag("MEDIUM", "Does it apply institution-wide?",
                 "Evidence is from the COCE unit. Confirm institution-level adoption (Provost/University plan, governance approval).")
        prior = [json.loads(e["prior_claim_json"]) for e in valid if e.get("prior_claim_json")]
        if any((p.get("matrix_strength") or "").lower() == "strong" for p in prior) and status != "STRONG":
            flag("MEDIUM", "Does the evidence address the entire indicator?",
                 "The prior FAU matrix rated this 'Strong'. The agent's skeptical pass does not support that rating yet (see gaps).")

    # element coverage
    elements = ind["config"].get("elements") or {}
    covered, uncovered = [], []
    corpus = " ".join((e.get("passage") or "") + " " + (e.get("summary") or "") for e in verified).lower()
    for name, words in elements.items():
        (covered if any(w.lower() in corpus for w in words) else uncovered).append(name)
    if elements and uncovered and valid:
        flag("MEDIUM", "Does the evidence address the entire indicator?", "Not yet evidenced: " + "; ".join(uncovered) + ".")

    # ---------------- preliminary score (advisory only)
    if not valid:
        prelim, why = 0, "No evidence located. NO EVIDENCE = NO CLAIM."
    elif not verified:
        prelim, why = 1, ("Only unverified, draft or prior-matrix claims. At most Developing (1) until a reviewer verifies the source.")
    elif a >= r:
        if best == "WEAK":
            prelim, why = 1, f"Evidence reaches {req} but match/authority is weak. Developing (1)."
        elif (len({e['document_id'] for e in approved}) >= 2 and all(LEVEL_RANK[e["implementation_level"]] >= r for e in approved)
              and not any(f["severity"] == "HIGH" for f in flags)):
            prelim, why = 3, ("Human-approved evidence from 2+ sources meets the required level with no open high-severity "
                              "flags. Candidate for Exemplary (3). Reviewer decides.")
        else:
            prelim, why = 2, (f"Evidence reaches the required level ({req}) and can be substantiated. Accomplished (2) at most "
                              "until human-approved evidence from multiple sources confirms full implementation.")
    elif a >= LEVEL_RANK["IMPLEMENTATION"]:
        prelim, why = 2, f"Some implementation is documented ({achieved}); the indicator needs {req}. Accomplished (2), with work remaining."
    else:
        prelim, why = 1, (f"Evidence establishes {achieved} only; the indicator needs {req}. Developing (1): hard to substantiate "
                          "without implementation records.")
    if ind["pending"]:
        why += " Indicator wording not yet loaded."

    # ---------------- gap narrative
    have = [f"{e['evidence_code']} [{display_status(e)} / {e['strength']} / {e['implementation_level']}]: {e['summary']}"
            for e in valid[:6]]
    grouped = {}
    for e in sorted(verified, key=lambda e: -LEVEL_RANK[e["implementation_level"]]):
        k = (e["implementation_level"], e["title"])
        grouped.setdefault(k, [])
        if e.get("page") and str(e["page"]).isdigit() and e["page"] not in grouped[k]:
            grouped[k].append(e["page"])
    proves = []
    for (lvl_, title), pages in grouped.items():
        pg = (", p. " if len(pages) == 1 else ", pp. ") + ", ".join(sorted(pages, key=int)) if pages else ""
        proves.append(f"{PROVES[lvl_]} ({title[:80]}{pg})")
    not_proves = [NOT_PROVEN[l] for l in LEVELS[max(a, 1):r] if l in NOT_PROVEN] if a < r else []
    if EFFECTIVE_RX.search(ind.get("text") or "") and a < LEVEL_RANK["MEASUREMENT"]:
        not_proves.append("that the service/system is effective, reliable or sufficient (the indicator's wording demands this)")
    arts = ind["config"].get("artifacts") or []
    needed = [(l, t) for (l, t) in arts if LEVEL_RANK[l] > a] or ([] if a >= r else arts)
    owners = ind["config"].get("owners") or []
    SHORT = {"PROCESS": "a defined process/requirement", "IMPLEMENTATION": "that it is actually carried out",
             "MEASUREMENT": "measured results", "IMPROVEMENT": "use of results to improve"}
    gap_short = [SHORT[l] for l in LEVELS[max(a, 1):r] if l in SHORT] if a < r else []
    ask = f"Obtain: {'; '.join(t for _, t in needed[:3])}" if needed else "Obtain implementation records"
    who = f" (likely owner: {', '.join(owners[:2])})." if owners else "."
    potential = max((e["implementation_level"] for e in unverified if e["origin"] != "matrix"), key=lambda l: LEVEL_RANK[l], default=None)
    drafts = sorted({e["title"] for e in unverified if e.get("is_draft")})
    if not valid:
        headline = (f"No evidence located for {ind['id']}. " + (ask + who if needed else "Load the indicator wording, then search."))
    elif not verified:
        headline = ("Support exists only in sources that need verification"
                    + (f" (draft/proposed: {'; '.join(drafts)})" if drafts else " (prior matrix claims without a located passage)")
                    + "." + (f" If verified, they would establish {potential}; the indicator needs {req}." if potential else "")
                    + (f" {ask}{who}" if LEVEL_RANK.get(potential, 0) < r else " Confirm the source's adopted status."))
    elif a >= r:
        headline = ("Evidence reaches the required level. Remaining work: human verification of currency, adoption and scope"
                    + (f"; strengthen with {needed[0][1]}" if needed else "") + ".")
    else:
        what_have = proves[0] if proves else "unverified claims only"
        what_have = what_have[:1].lower() + what_have[1:]
        headline = (f"Current evidence establishes {what_have} but does not demonstrate {', '.join(gap_short) or 'the required level'}. "
                    f"{ask}{who}")
    return {
        "indicator": ind, "status": status, "strength": best if valid else "NONE", "achieved": achieved,
        "required": req, "prelim": prelim, "prelim_rationale": why, "flags": flags, "covered": covered,
        "uncovered": uncovered, "have": have, "proves": proves, "not_proves": not_proves,
        "needed": needed, "owners": owners, "headline": headline,
        "n_valid": len(valid), "n_verified": len(verified), "n_unverified": len(unverified), "n_approved": len(approved),
        "n_review_backlog": sum(1 for e in valid if e["review_status"] == "NOT REVIEWED"),
        "review": review, "official": review.get("official_score"),
        "human_prelim": review.get("human_prelim_score"),
        "review_status": review.get("review_status") or "NOT REVIEWED",
    }


def assess_all(conn, clearance):
    ev = load_evidence(conn, clearance)
    by = {}
    for e in ev:
        by.setdefault(e["indicator_id"], []).append(e)
    reviews = {r["indicator_id"]: dict(r) for r in conn.execute("SELECT * FROM indicator_reviews")}
    return [assess(ind, by.get(ind["id"], []), reviews.get(ind["id"])) for ind in all_indicators(conn)]


def assess_one(conn, clearance, ind_id):
    ind = get_indicator(conn, ind_id)
    if not ind:
        return None, []
    ev = load_evidence(conn, clearance, ind["id"])
    r = conn.execute("SELECT * FROM indicator_reviews WHERE indicator_id=?", (ind["id"],)).fetchone()
    return assess(ind, ev, dict(r) if r else None), ev


def summary(results):
    total = len(results)
    s = {k: sum(1 for x in results if x["status"] == k) for k in
         ("STRONG", "PARTIAL", "MISSING", "NEEDS VERIFICATION", "WORDING PENDING")}
    with_ev = sum(1 for x in results if x["n_valid"])
    relying_unverified = sum(1 for x in results if x["n_unverified"])
    reviewed = sum(1 for x in results if x["review_status"] != "NOT REVIEWED")
    approved = sum(1 for x in results if x["review_status"] == "APPROVED")
    official = [x["official"] for x in results if x["official"] is not None]
    return {
        "total": total, "with_evidence": with_ev, "strong": s["STRONG"], "partial": s["PARTIAL"],
        "missing": s["MISSING"], "needs_verification": s["NEEDS VERIFICATION"], "wording_pending": s["WORDING PENDING"],
        "relying_unverified": relying_unverified, "reviewed": reviewed, "approved": approved,
        "prelim_points": sum(x["prelim"] for x in results), "max_points": total * 3,
        "official_points": sum(official) if official else None, "official_count": len(official),
        "coverage_pct": round(100 * with_ev / total) if total else 0,
        "backlog": sum(x["n_review_backlog"] for x in results),
    }


def snapshot(results):
    return {x["indicator"]["id"]: {"status": x["status"], "strength": x["strength"], "prelim": x["prelim"],
                                   "achieved": x["achieved"], "n": x["n_valid"]} for x in results}
