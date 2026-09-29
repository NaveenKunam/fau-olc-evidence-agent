"""Indicator registry: load seed data, read/update indicators, derive search vocabulary."""
import json
import re

from .db import now
from .indicator_data import CATEGORIES, INDICATORS, MATRIX

LEVELS = ["INTENT", "PROCESS", "IMPLEMENTATION", "MEASUREMENT", "IMPROVEMENT"]
LEVEL_RANK = {l: i + 1 for i, l in enumerate(LEVELS)}
LEVEL_HELP = {
    "INTENT": "We intend/plan to do this (goals, objectives, targets).",
    "PROCESS": "A defined policy, process, requirement or service exists.",
    "IMPLEMENTATION": "The process is actually being used (records, completed reviews, logs).",
    "MEASUREMENT": "Effectiveness is measured (data, results, metrics).",
    "IMPROVEMENT": "Measurement results are used to improve the program (closed loop).",
}

RUBRIC = [
    (0, "Deficient", "No indication that the quality standard is in place."),
    (1, "Developing", "Slight existence of the quality standard, but difficult to substantiate. Significant improvement remains."),
    (2, "Accomplished", "Moderate implementation and the quality standard can be substantiated. Some improvement remains."),
    (3, "Exemplary", "Fully implemented and fully substantiated with little or no improvement needed."),
]

CAT_BY_CODE = {c: name for c, name, _ in CATEGORIES}

STOP = set("""a an the and or of to for in on by with is are be been being that this these those as at from it its
their they them there which who whom whose into about such other etc including include includes than then so
has have had do does not no all any each both either can may must should will would institution institutional
program programs online students student course courses provided provides provide ensure ensures""".split())


def seed_indicators(conn):
    """Insert indicator slots that do not exist yet. Never overwrites edited wording."""
    cats = {c: (name, i) for i, (c, name, _) in enumerate(CATEGORIES)}
    for d in INDICATORS:
        code = d["id"].split("-")[0]
        name, ci = cats[code]
        cfg = {k: d.get(k) for k in ("anchors", "terms", "elements", "artifacts", "owners", "hint", "must")}
        exists = conn.execute("SELECT 1 FROM indicators WHERE id=?", (d["id"],)).fetchone()
        if exists:
            continue
        conn.execute(
            """INSERT INTO indicators(id, category_code, category, number, sort_key, text, alt_text, text_source,
               required_level, config_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (d["id"], code, name, d["n"], ci * 100 + d["n"], d.get("text"), d.get("alt_text"),
             d.get("text_source") or MATRIX, d["required"], json.dumps(cfg), now()))
    conn.commit()


def row_to_ind(r):
    d = dict(r)
    d["config"] = json.loads(d.pop("config_json") or "{}")
    d["pending"] = not d.get("text")
    d["wording_conflict"] = bool(d.get("alt_text"))
    return d


def all_indicators(conn):
    return [row_to_ind(r) for r in conn.execute("SELECT * FROM indicators ORDER BY sort_key")]


def get_indicator(conn, ind_id):
    r = conn.execute("SELECT * FROM indicators WHERE id=?", (ind_id.upper(),)).fetchone()
    return row_to_ind(r) if r else None


def normalize_id(raw):
    """Accept IS-1, INS-1, ins 01, CDID3, TEC_4 ... -> canonical ID."""
    s = raw.strip().upper().replace("_", "-").replace(" ", "-")
    m = re.match(r"^([A-Z]+)-?0*(\d+)$", s)
    if not m:
        return s
    code, n = m.group(1), int(m.group(2))
    alias = {"IS": "INS", "INST": "INS", "TS": "TEC", "TECH": "TEC", "CD": "CDID", "ID": "CDID", "COU": "CDID",
             "TLE": "TL", "TEA": "TL", "FS": "FAC", "SS": "LEA", "LS": "LEA", "STU": "LEA", "EA": "EVA", "EVAL": "EVA",
             "CSTR": "CS"}
    code = alias.get(code, code)
    return f"{code}-{n:02d}"


def derived_vocab(ind):
    """Anchors/terms for matching. Uses curated config, else derives from wording (for imported/edited text)."""
    cfg = ind["config"]
    anchors = cfg.get("anchors") or []
    terms = cfg.get("terms") or []
    if not anchors and ind.get("text"):
        words = [w for w in re.findall(r"[a-z][a-z\-]{3,}", ind["text"].lower()) if w not in STOP]
        seen = []
        for w in words:
            if w not in seen:
                seen.append(w)
        anchors = [re.escape(w[:-1] if w.endswith("s") else w) for w in seen[:4]]
        terms = seen[4:14]
    return anchors, terms


def update_indicator_text(conn, ind_id, text, source, user, alt_text=None, handbook=None, handbook_source=None):
    fields, vals = [], []
    if text is not None:
        fields += ["text=?", "text_source=?"]
        vals += [text.strip() or None, source]
    if alt_text is not None:
        fields.append("alt_text=?")
        vals.append(alt_text.strip() or None)
    if handbook is not None:
        fields += ["handbook_text=?", "handbook_source=?"]
        vals += [handbook.strip() or None, handbook_source]
    fields += ["updated_by=?", "updated_at=?"]
    vals += [user, now(), ind_id]
    conn.execute(f"UPDATE indicators SET {', '.join(fields)} WHERE id=?", vals)
