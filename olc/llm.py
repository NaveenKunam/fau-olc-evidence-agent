"""Optional Claude second-opinion layer.

Enabled only when the `anthropic` package is installed and ANTHROPIC_API_KEY (or another Anthropic credential)
is configured. The rule engine works fully without it. Claude is used in two narrowly-scoped ways:

1. review_candidates: re-judge rule-engine (indicator, passage) candidates. Claude may DROP a mapping or LOWER the
   implementation level; it can never raise a level above what the document type can prove, and it never invents
   passages (it only sees passages that already exist in the source text).
2. draft_narrative: draft a submission narrative from APPROVED evidence only. Every [E-xxxx] citation must refer to
   an approved item or the draft is discarded.
"""
import json
import os
import re

from .indicators import LEVELS

MODEL = os.environ.get("OLC_CLAUDE_MODEL", "claude-opus-5")


def get_llm():
    if os.environ.get("OLC_DISABLE_LLM"):
        return None
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_PROFILE")):
        return None
    return ClaudeReviewer()


class ClaudeReviewer:
    def __init__(self):
        import anthropic
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()

    def _call(self, system, user, schema, max_tokens=16000):
        kw = dict(model=MODEL, max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}],
                  thinking={"type": "adaptive"},
                  output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema}})
        try:
            # Server-side refusal fallback (beta); fall back to a plain call if unavailable.
            resp = self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
        except (TypeError, self.anthropic.BadRequestError):
            resp = self.client.messages.create(**kw)
        if getattr(resp, "stop_reason", None) == "refusal":
            return None
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), None)
        return json.loads(text) if text else None

    def review_candidates(self, doc, candidates):
        items = [{"idx": k, "indicator_id": c["ind"]["id"], "indicator": c["ind"]["text"],
                  "required_level": c["ind"]["required_level"], "passage": c["p"]["text"]}
                 for k, c in enumerate(candidates[:80])]
        system = ("You are a skeptical external reviewer for the OLC Quality Scorecard (Administration of Online Programs). "
                  "Relevance is not substantiation. A plan proves intent only; a policy proves a requirement exists, not compliance; "
                  "a service webpage proves availability, not effectiveness. Judge each (indicator, passage) pair strictly from the "
                  "passage text. Never infer facts not stated in the passage.")
        user = (f"Source document: {doc['title']} (type {doc['doc_type']}, date {doc['doc_date'] or 'unknown'}).\n"
                f"Levels: {', '.join(LEVELS)}.\nFor each item decide: does the passage genuinely bear on the indicator (maps)? "
                "What is the highest level the passage itself substantiates? Give a one-sentence rationale.\n\n"
                + json.dumps(items, ensure_ascii=False))
        schema = {"type": "object", "additionalProperties": False, "required": ["items"],
                  "properties": {"items": {"type": "array", "items": {
                      "type": "object", "additionalProperties": False, "required": ["idx", "maps", "level", "rationale"],
                      "properties": {"idx": {"type": "integer"}, "maps": {"type": "boolean"},
                                     "level": {"type": "string", "enum": LEVELS}, "rationale": {"type": "string"}}}}}}
        try:
            out = self._call(system, user, schema)
        except Exception as ex:  # network/auth problems must not block ingestion
            print(f"[llm] review skipped: {ex}")
            return candidates
        if not out:
            return candidates
        verdict = {v["idx"]: v for v in out.get("items", [])}
        kept = []
        for k, c in enumerate(candidates):
            v = verdict.get(k)
            if v is None:
                kept.append(c)
                continue
            if not v["maps"]:
                continue
            c = dict(c, llm_checked=True, llm_rationale=v["rationale"][:400])
            c["llm_level"] = v["level"]
            kept.append(c)
        return kept

    def draft_narrative(self, ind, approved):
        ev = [{"code": e["evidence_code"], "source": e["title"], "page": e.get("page"), "passage": e["passage"]} for e in approved]
        system = ("Draft an OLC Quality Scorecard justification narrative for Florida Atlantic University using ONLY the approved "
                  "evidence passages provided. Do not add facts, numbers, dates, or claims not present in the passages. Cite every "
                  "claim with its code in square brackets, e.g. [E-0012]. If the evidence shows only a plan or policy, say so plainly.")
        user = f"Indicator {ind['id']}: {ind['text']}\n\nApproved evidence:\n{json.dumps(ev, ensure_ascii=False)}"
        schema = {"type": "object", "additionalProperties": False, "required": ["narrative"],
                  "properties": {"narrative": {"type": "string"}}}
        try:
            out = self._call(system, user, schema, max_tokens=8000)
        except Exception as ex:
            print(f"[llm] draft skipped: {ex}")
            return None
        if not out:
            return None
        text = out["narrative"]
        cited = set(re.findall(r"E-\d{4}", text))
        allowed = {e["evidence_code"] for e in approved}
        if not cited or not cited <= allowed:
            return None  # uncited or cites non-approved evidence -> discard
        return text
