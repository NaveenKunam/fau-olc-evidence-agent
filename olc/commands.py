"""Team command console: parse a command line into an action."""
import re

from . import reports
from .indicators import normalize_id

HELP = """# Commands

| Command | What it does |
|---|---|
| `PROCESS SOURCE <url>` | Fetch an approved URL and analyze it against all indicators (upload files on **Add Source**) |
| `REPROCESS <source id>` | Re-run mapping on a stored source (human-reviewed evidence is never touched) |
| `SHOW GAPS` | Indicators where evidence is missing or incomplete |
| `INDICATOR IS-1` | Everything associated with an indicator (IDs like INS-01, IS-1, CDID-3, LEA-14 all work) |
| `OWNER PACKET OIT` | Consolidated evidence request for an owner (OIT, PROVOST, COCE, IE, REGISTRAR, LIBRARY, SAS, CAPS, BUDGET...) |
| `OWNERS` | List every likely owner with open requests |
| `MEETING BRIEF` | Concise OLC team meeting report |
| `WHAT CHANGED` | Evidence added and gaps closed since the last checkpoint |
| `CHECKPOINT <label>` | Save the current state so WHAT CHANGED can compare against it |
| `VERIFY` / `VERIFY INS-04` | Skeptical OLC reviewer pass over all or one indicator |
| `SUBMISSION DRAFT INS-01` | Draft justification from APPROVED evidence only |
| `SEARCH FAU TEC-01` | Search approved FAU web sources for candidate evidence for an indicator |
| `INITIAL REVIEW` | Regenerate the initial review report |
"""


def run(cmd, ctx):
    """ctx provides: conn, clearance, user, llm, allowed, seeds, can_write. Returns dict(markdown=..., redirect=...)."""
    c = cmd.strip()
    u = c.upper()
    conn, cl = ctx["conn"], ctx["clearance"]
    if not c or u in ("HELP", "?"):
        return {"markdown": HELP}
    if u.startswith("PROCESS SOURCE"):
        arg = c[len("PROCESS SOURCE"):].strip()
        if not arg:
            return {"markdown": "Give a URL (`PROCESS SOURCE https://www.fau.edu/...`) or upload a file on **Add Source**.", "redirect": "/sources/add"}
        return {"action": "process_url", "url": arg}
    if u.startswith("REPROCESS"):
        m = re.search(r"(\d+)", c)
        return {"action": "reprocess", "doc_id": int(m.group(1))} if m else {"markdown": "Usage: REPROCESS <source id>"}
    if u.startswith("SHOW GAPS") or u == "GAPS":
        return {"markdown": reports.gap_report(conn, cl)}
    if u.startswith("INDICATOR"):
        arg = c[len("INDICATOR"):].strip()
        return {"markdown": reports.indicator_brief(conn, cl, normalize_id(arg)), "link": f"/indicator/{normalize_id(arg)}"}
    if u.startswith("OWNER PACKET"):
        arg = c[len("OWNER PACKET"):].strip() or "OIT"
        return {"markdown": reports.owner_packet(conn, cl, arg)}
    if u == "OWNERS":
        from .assess import assess_all
        owners = reports.all_owners(assess_all(conn, cl))
        lines = ["# Likely owners with open evidence requests", "", "| Owner | Indicators |", "|---|---|"]
        lines += [f"| {o} | {', '.join(x['indicator']['id'] for x in xs)} |" for o, xs in sorted(owners.items(), key=lambda kv: -len(kv[1]))]
        return {"markdown": "\n".join(lines)}
    if u.startswith("MEETING BRIEF"):
        return {"markdown": reports.meeting_brief(conn, cl)}
    if u.startswith("WHAT CHANGED"):
        return {"markdown": reports.what_changed(conn, cl)}
    if u.startswith("CHECKPOINT"):
        return {"action": "checkpoint", "label": c[len("CHECKPOINT"):].strip() or "Manual checkpoint"}
    if u.startswith("VERIFY"):
        arg = c[len("VERIFY"):].strip()
        return {"markdown": reports.verify_report(conn, cl, normalize_id(arg) if arg else None)}
    if u.startswith("SUBMISSION DRAFT"):
        arg = c[len("SUBMISSION DRAFT"):].strip()
        if not arg:
            return {"markdown": "Usage: SUBMISSION DRAFT <indicator>"}
        return {"markdown": reports.submission_draft(conn, cl, normalize_id(arg), llm=ctx.get("llm"))}
    if u.startswith("SEARCH FAU") or u.startswith("SEARCH"):
        arg = re.sub(r"^SEARCH( FAU)?", "", c, flags=re.I).strip()
        return {"action": "search", "indicator": normalize_id(arg) if arg else None}
    if u.startswith("INITIAL REVIEW"):
        return {"markdown": reports.initial_review(conn, cl)}
    return {"markdown": f"Unrecognized command: `{c}`\n\n" + HELP}
