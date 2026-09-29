"""Initial ingestion for the FAU OLC Evidence Agent.

    python seed.py            # first-time setup + initial ingestion (safe to re-run; skips duplicates)
    python seed.py --offline  # skip fetching public URLs

Steps: create schema -> load 70 indicator slots -> create admin -> import FAU OLC Evidence Matrix as prior claims ->
ingest supplied FAU documents -> fetch supplied FAU URLs -> map evidence -> checkpoint -> write reports/.
"""
import json
import os
import secrets
import sys

from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from olc.db import connect, init_schema, ensure_dirs, now  # noqa: E402
from olc.indicators import seed_indicators  # noqa: E402
from olc.matrix import import_matrix  # noqa: E402
from olc.service import add_bytes, add_url, checkpoint  # noqa: E402
from olc import reports  # noqa: E402
from olc.llm import get_llm  # noqa: E402

LOCAL_SOURCES = [
    # (file in seed/sources, official public URL it was downloaded from, classification, title)
    ("coce-2025-2030-strategic-plan-07-17-2025.pdf",
     "https://www.fau.edu/elearning/faculty-online-quality-commitment/_documents/coce-2025-2030-strategic-plan-07-17-2025.pdf", "PUBLIC",
     "COCE / Florida Atlantic Online 2025-2030 Strategic Plan"),
    ("proposed-revisions-02-19-2024-distance-learning-scope-and-policies.pdf",
     "https://www.fau.edu/provost/documents/proposed-revisions-02-19-2024-distance-learning-scope-and-policies.pdf", "PUBLIC",
     "Distance Learning Scope and Policies (Office of the Provost memorandum, Feb 19, 2024)"),
]


def main():
    offline = "--offline" in sys.argv
    instance = os.environ.get("OLC_INSTANCE", os.path.join(BASE, "instance"))
    ensure_dirs(instance)
    conn = connect(os.path.join(instance, "olc.db"))
    init_schema(conn)
    seed_indicators(conn)
    n_ind = conn.execute("SELECT COUNT(*) FROM indicators").fetchone()[0]
    print(f"[1] Indicator slots loaded: {n_ind}")

    if not conn.execute("SELECT 1 FROM users").fetchone():
        pw = secrets.token_urlsafe(12)
        conn.execute("INSERT INTO users(username, display_name, role, clearance, pw_hash, created_at) VALUES (?,?,?,?,?,?)",
                     ("admin", "OLC Admin", "admin", "RESTRICTED", generate_password_hash(pw), now()))
        conn.commit()
        with open(os.path.join(instance, "INITIAL_ADMIN_PASSWORD.txt"), "w") as f:
            f.write(f"username: admin\npassword: {pw}\n\nChange this after first sign-in (Account page). Delete this file afterwards.\n")
        print(f"[2] Admin account created. Credentials written to instance/INITIAL_ADMIN_PASSWORD.txt")
    else:
        print("[2] Users already exist; skipping admin creation")

    m = import_matrix(conn, os.path.join(BASE, "seed", "FAU_OLC_Evidence_Matrix.xlsx"))
    print(f"[3] Matrix imported: {m['claims']} prior claims (NEEDS VERIFICATION), {m['rows_without_evidence']} rows had no evidence, {m['requests']} evidence requests")

    llm = get_llm()
    for fn, url, cls, title in LOCAL_SOURCES:
        path = os.path.join(BASE, "seed", "sources", fn)
        if not os.path.exists(path):
            print(f"    missing local file {fn}")
            continue
        with open(path, "rb") as f:
            data = f.read()
        r = add_bytes(conn, data, filename=fn, source_url=url, actor="Initial ingestion", classification=cls, title=title,
                      origin="seed", llm=llm, notes="Supplied FAU source (downloaded from the official URL for initial ingestion)")
        conn.execute("UPDATE documents SET stored_path=? WHERE id=?", (path, r["doc_id"]))
        conn.commit()
        print(f"[4] {r['status']:10s} {r['title'][:70]} -> {len(r['created'])} evidence items")

    if not offline:
        cfg = json.load(open(os.path.join(BASE, "config", "approved_sources.json"), encoding="utf-8"))
        for url in cfg.get("initial_urls", []):
            try:
                r = add_url(conn, url, cfg["allowed_web_domains"], actor="Initial ingestion", origin="seed", llm=llm)
                print(f"[5] {r['status']:10s} {r['title'][:70]} -> {len(r['created'])} evidence items")
            except Exception as ex:
                print(f"[5] FAILED {url}: {ex}")

    if not conn.execute("SELECT 1 FROM runs").fetchone():
        checkpoint(conn, "Initial ingestion", "Initial ingestion")

    from app import missing_inputs
    os.makedirs(os.path.join(BASE, "reports"), exist_ok=True)
    out = {
        "initial_review.md": reports.initial_review(conn, "RESTRICTED", docs_note=missing_inputs(conn)),
        "gap_report.md": reports.gap_report(conn, "RESTRICTED"),
        "verify.md": reports.verify_report(conn, "RESTRICTED"),
        "meeting_brief.md": reports.meeting_brief(conn, "RESTRICTED"),
    }
    from olc.assess import assess_all
    for owner in sorted(reports.all_owners(assess_all(conn, "RESTRICTED"))):
        safe = "".join(ch if ch.isalnum() else "_" for ch in owner)[:50]
        out[f"owner_packets/{safe}.md"] = reports.owner_packet(conn, "RESTRICTED", owner)
    for name, text in out.items():
        p = os.path.join(BASE, "reports", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    print(f"[6] Wrote {len(out)} reports to reports/")
    conn.close()


if __name__ == "__main__":
    main()
