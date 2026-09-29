# FAU OLC Evidence Agent

Evidence-gathering and gap-analysis application for Florida Atlantic University Online's self-review against the
**OLC Quality Scorecard for the Administration of Online Programs** (70 indicators).

It ingests documents and URLs, extracts evidence passages with page/section provenance, maps them to one or more
indicators, grades what each passage can actually prove (INTENT, PROCESS, IMPLEMENTATION, MEASUREMENT, IMPROVEMENT),
finds gaps, recommends the artifact that would close each gap and its likely FAU owner, and produces a **preliminary**
0-3 assessment. Only human reviewers approve evidence and enter the **official** score.

> NO EVIDENCE = NO CLAIM. Relevance is not substantiation. A plan proves intent; a policy proves a requirement exists;
> a service webpage proves availability. Only records, reports, data and minutes can prove implementation or effectiveness.

## Quick start (Windows, this machine)

```bash
pip install -r requirements.txt
python seed.py        # first run: builds the database, loads indicators, imports the matrix, ingests FAU sources
python app.py         # http://127.0.0.1:5210
```

Sign in with the admin credentials written to `instance/INITIAL_ADMIN_PASSWORD.txt`, change the password on the
Account page, delete that file, then add team members in **Admin > Users**.

`seed.py` is safe to re-run: identical files are skipped, and human decisions are never overwritten.
Use `python seed.py --offline` to skip fetching public URLs.

## Sharing with the OLC team (server deployment)

The app is a single Flask service with a SQLite database in `instance/`. To host it for the team, for example on the
same Docker host that serves forms.fauelearning.com:

```bash
docker compose up -d --build            # serves on port 5210
docker compose exec olc-agent python seed.py   # first time only
```

Put it behind the existing nginx with HTTPS and set `OLC_SECURE_COOKIES=1`. Back up the `olc-data` volume
(the database and uploaded files). Uploaded INTERNAL/RESTRICTED files are stored only in that volume and are served
only to signed-in users whose clearance allows it.

## What the team can do

| Area | Where |
|---|---|
| Readiness dashboard: 70 indicators by category, filters (category, status, strength, owner, review status) | `/` |
| Indicator view: requirement, handbook interpretation, rubric, evidence with verbatim passages and sources, gap analysis, skeptical-review flags, AI preliminary, human decision and official score | `/indicator/INS-01` |
| Add documents (PDF, DOCX, XLSX, CSV, TXT/MD, HTML), URLs, or pasted text | `/sources/add` |
| Evidence repository with approve/reject, export to Excel | `/evidence`, `/export.xlsx` |
| Gap report, VERIFY pass, meeting brief, WHAT CHANGED, initial review | `/reports/...` |
| Owner packets (evidence requests per FAU office) | `/owners` |
| Command console | `/commands` |
| Users, clearance, OLC rubric import, checkpoints, audit log | `/admin` |

### Commands

`PROCESS SOURCE <url>` · `REPROCESS <id>` · `SHOW GAPS` · `INDICATOR IS-1` · `OWNER PACKET OIT` · `OWNER PACKET PROVOST` ·
`OWNERS` · `MEETING BRIEF` · `WHAT CHANGED` · `CHECKPOINT <label>` · `VERIFY` · `VERIFY INS-04` ·
`SUBMISSION DRAFT INS-01` · `SEARCH FAU TEC-01` · `INITIAL REVIEW`

## How evidence is judged

1. **Extraction.** Every passage keeps document, page (PDF) or row (spreadsheet), section heading, and the verbatim text.
2. **Mapping.** Each passage is scored against every indicator's concepts. Menus, link lists, tables of contents and
   passages missing a required concept (for example, "faculty" for Faculty Support) are discarded.
3. **Implementation level.** Language cues determine the level, then the **document type caps it**:
   PLAN caps at INTENT (present-tense descriptions at PROCESS); POLICY, STANDARD and WEBPAGE cap at PROCESS;
   REPORT, DATA and MINUTES can reach IMPLEMENTATION, MEASUREMENT or IMPROVEMENT.
4. **Status.** FOUND (reaches the indicator's required level), PARTIAL, NEEDS VERIFICATION (prior matrix claims,
   non-authoritative sources), PROPOSED/DRAFT (e.g., filename contains "proposed"), SUPERSEDED/STALE; humans set
   APPROVED or REJECTED.
5. **Strength.** STRONG / MODERATE / WEAK / NONE, from level vs. requirement, match quality and authority.
   Strength is not the OLC score.
6. **Preliminary score.** 0 with no evidence; at most 1 while support is unverified, draft or plan-only; 2 when the
   required level is substantiated; 3 is only suggested when human-approved evidence from two or more sources meets
   the requirement with no high-severity flags. It is always labelled advisory.
7. **Skeptical reviewer (VERIFY).** Challenges each indicator: whole indicator covered? current? authoritative?
   implementation shown? measurement/continuous improvement required? institution-wide? policy vs. proof, plan vs. proof?
   verifiable by an external reviewer?

Workflow: DISCOVERED > AI MAPPED > NEEDS HUMAN REVIEW > APPROVED / REJECTED > INCLUDED IN SUBMISSION.
Reprocessing only replaces AI mappings nobody has touched; anything approved, rejected or edited by a person is kept.
Submission drafts use APPROVED evidence only.

## Governing OLC materials

The QSS PDF and Handbook PDF were **not included** with the initial build. Indicator wording was loaded from the
FAU OLC Evidence Matrix (57 of 70 slots); 13 slots are marked *wording pending* and are not auto-mapped or scored until
their wording is loaded. To make the OLC documents authoritative:

**Admin > OLC Rubric**: upload *QSS - Administration of Online Programs.pdf* (parse, preview, apply), then the
*Administration of Online Programs Handbook.pdf*. Then reprocess sources so evidence is re-mapped against the official
wording. Wording can also be edited per indicator (admins).

Evidence expectations, search vocabulary, recommended artifacts and likely owners in `olc/indicator_data.py` are
FAU-team working guidance, not OLC text, and are labelled that way in the UI.

## Security

* Roles: viewer, contributor, reviewer, admin. Clearance: PUBLIC, INTERNAL, RESTRICTED. Users never see sources or
  evidence above their clearance (pages, reports, owner packets and exports are all filtered server-side).
* Uploads default to INTERNAL. Web fetching is limited to domains in `config/approved_sources.json`.
* CSRF protection on every form; passwords hashed; every human action is written to the audit log.

## Enabling Google Drive / SharePoint / OneDrive (read-only)

Connection points are in `olc/connectors.py` and `config/approved_sources.json`:

1. Register an OAuth app (Google Cloud Console, or Entra ID app registration for Microsoft) with **read-only
   delegated** scopes (`drive.readonly`; `Files.Read`, `Sites.Read.All`).
2. Put the client/tenant IDs in `config/approved_sources.json`, set `enabled: true`, and put the secret in the
   environment variable named there.
3. List the approved folders/libraries under `approved_folders` / `approved_locations` with a default classification.
4. Wire the OAuth redirect at `/connect/google` and `/connect/microsoft` (stubs exist) to store each reviewer's token.

Because calls use each reviewer's own token, the provider only returns files that person can already open. The agent
refuses any folder not on the approved list and never crawls a whole tenant.

## Tests

```bash
python tests/smoke_workflow.py <path-to-a-copy-of-instance>
```

Exercises CSRF, approve/reject/include, official scoring, submission drafting, reprocessing without overwriting human
decisions, duplicate detection, uploads (TXT/CSV/DOCX), domain allowlist and clearance filtering.
