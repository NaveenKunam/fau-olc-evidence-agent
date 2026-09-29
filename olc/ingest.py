"""Document ingestion: text extraction with page/section provenance, metadata detection, URL fetching."""
import csv
import hashlib
import io
import json
import os
import re
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree

MAX_FETCH_BYTES = 25 * 1024 * 1024
UA = "FAU-OLC-Evidence-Agent/1.0 (+read-only evidence review; contact: FAU Online)"

# ------------------------------------------------------------------ approved sources

DEFAULT_ALLOWED = ["fau.edu", "flvc.org", "floridashines.org", "flbog.edu", "sacscoc.org",
                   "onlinelearningconsortium.org", "qualitymatters.org", "sreb.org"]


def load_allowed_domains(config_path):
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("allowed_web_domains") or DEFAULT_ALLOWED
    except (OSError, ValueError):
        return DEFAULT_ALLOWED


def domain_allowed(url, allowed):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in allowed)


def authority_for(url_or_name):
    s = (url_or_name or "").lower()
    host = urllib.parse.urlparse(s).hostname or ""
    if host.endswith("fau.edu"):
        return "FAU"
    if any(host.endswith(d) for d in ("flvc.org", "flbog.edu", "floridashines.org", "myflorida.com", "fldoe.org")):
        return "STATE"
    if any(host.endswith(d) for d in ("sacscoc.org", "ed.gov")):
        return "ACCREDITOR"
    if host:
        return "OTHER"
    return "UNKNOWN"


def fetch_url(url, allowed):
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("URL must start with http:// or https://")
    if not domain_allowed(url, allowed):
        raise PermissionError(f"Domain not on the approved list: {urllib.parse.urlparse(url).hostname}. "
                              "An administrator can add it in config/approved_sources.json.")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        final = resp.geturl()
        if not domain_allowed(final, allowed):
            raise PermissionError(f"Redirected off the approved list: {final}")
        ctype = resp.headers.get("Content-Type", "")
        data = resp.read(MAX_FETCH_BYTES + 1)
        if len(data) > MAX_FETCH_BYTES:
            raise ValueError("Document exceeds 25 MB limit")
        last_mod = resp.headers.get("Last-Modified")
    return data, ctype, final, last_mod


# ------------------------------------------------------------------ text helpers

WS = re.compile(r"\s+")


def clean(s):
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("", "•").replace("●", "•")
    return WS.sub(" ", s).strip()


def looks_like_heading(line):
    t = line.strip()
    if not (3 <= len(t) <= 110):
        return False
    if re.search(r"\.{4,}", t):          # table-of-contents leader dots
        return False
    if re.match(r"^\d+ of \d+$", t):
        return False
    if t.endswith((".", ",", ";")) and not re.match(r"^(Goal|Objective) ", t):
        return False
    if re.match(r"^(Goal|Objective|Section|Article|Part|Chapter)\s*#?\s*[\dA-Z]", t):
        return True
    words = t.split()
    if len(words) > 12:
        return False
    caps = sum(1 for w in words if w[:1].isupper() or not w[:1].isalpha())
    return caps / max(len(words), 1) >= 0.7


SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[\"'(•●A-Z0-9])|\s+(?=•|●)")


def chunk_blocks(blocks, max_chars=650):
    """blocks: iterable of (page, section, text). Returns list of passage dicts (1-2 sentence windows)."""
    out = []
    for page, section, text in blocks:
        text = clean(text)
        if len(text) < 25:
            continue
        sents = [s.strip() for s in SENT_SPLIT.split(text) if len(s.strip()) > 2]
        buf = ""
        for s in sents:
            if len(buf) + len(s) + 1 <= max_chars and (len(buf) < 160 or not buf):
                buf = (buf + " " + s).strip()
                continue
            if buf:
                out.append({"page": page, "section": section, "text": buf})
            buf = s[:max_chars * 2]
        if buf:
            out.append({"page": page, "section": section, "text": buf})
    return out


# ------------------------------------------------------------------ extractors

def extract_pdf(data):
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(data))
    meta = {}
    try:
        m = reader.metadata or {}
        meta = {"title": (m.get("/Title") or "").strip(), "created": m.get("/CreationDate"),
                "modified": m.get("/ModDate")}
    except Exception:
        pass
    blocks, section, full = [], None, []
    for i, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception:
            raw = ""
        full.append(raw)
        para = []
        for line in raw.splitlines():
            line = line.rstrip()
            if re.match(r"^\s*\d+ of \d+\s*$", line) or not line.strip():
                if para and not line.strip():
                    blocks.append((str(i), section, " ".join(para)))
                    para = []
                continue
            if looks_like_heading(line):
                if para:
                    blocks.append((str(i), section, " ".join(para)))
                    para = []
                section = clean(line)
                continue
            # join hyphenated line breaks
            if para and para[-1].endswith("-") and not para[-1].endswith(" -"):
                para[-1] = para[-1][:-1] + line.strip()
            else:
                para.append(line.strip())
        if para:
            blocks.append((str(i), section, " ".join(para)))
    return blocks, "\n".join(full), len(reader.pages), meta


def extract_docx(data):
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml")
        core = z.read("docProps/core.xml") if "docProps/core.xml" in z.namelist() else None
    root = ElementTree.fromstring(xml)
    blocks, section, full, first_heading = [], None, [], None
    for p in root.iter(f"{{{ns['w']}}}p"):
        style = p.find("w:pPr/w:pStyle", ns)
        sval = style.get(f"{{{ns['w']}}}val") if style is not None else ""
        text = "".join(t.text or "" for t in p.iter(f"{{{ns['w']}}}t")).strip()
        if not text:
            continue
        full.append(text)
        if sval.lower().startswith(("heading", "title")) or (len(text) < 90 and looks_like_heading(text) and len(text.split()) <= 8):
            section = clean(text)
            first_heading = first_heading or section
        else:
            blocks.append((None, section, text))
    meta = {}
    if core:
        try:
            c = ElementTree.fromstring(core)
            for el in c:
                tag = el.tag.split("}")[-1]
                if tag in ("title", "created", "modified") and el.text:
                    meta[tag] = el.text
        except ElementTree.ParseError:
            pass
    if first_heading and not meta.get("title"):
        meta["title"] = first_heading
    return blocks, "\n".join(full), None, meta


def extract_xlsx(data):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    blocks, full = [], []
    for ws in wb.worksheets:
        header = None
        for ri, row in enumerate(ws.iter_rows(values_only=True), start=1):
            vals = ["" if v is None else str(v).strip() for v in row]
            if not any(vals):
                continue
            if header is None:
                header = vals
                continue
            parts = [f"{header[i] if i < len(header) and header[i] else 'Col' + str(i + 1)}: {v}"
                     for i, v in enumerate(vals) if v]
            line = "; ".join(parts)
            full.append(line)
            blocks.append((f"row {ri}", f"{ws.title}: " + ", ".join(h for h in header if h)[:120], line))
    return blocks, "\n".join(full), None, {}


def extract_csv(data):
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    blocks = []
    if rows:
        header = rows[0]
        for ri, row in enumerate(rows[1:], start=2):
            parts = [f"{header[i] if i < len(header) else 'Col' + str(i + 1)}: {v}" for i, v in enumerate(row) if v.strip()]
            if parts:
                blocks.append((f"row {ri}", ", ".join(h for h in header if h)[:120], "; ".join(parts)))
    return blocks, text, None, {}


def extract_txt(data):
    text = data.decode("utf-8-sig", errors="replace")
    blocks, section, para = [], None, []
    for line in text.splitlines():
        if not line.strip():
            if para:
                blocks.append((None, section, " ".join(para)))
                para = []
            continue
        if line.lstrip().startswith("#") or (looks_like_heading(line) and len(line.split()) <= 8):
            if para:
                blocks.append((None, section, " ".join(para)))
                para = []
            section = clean(line.lstrip("# "))
        else:
            para.append(line.strip())
    if para:
        blocks.append((None, section, " ".join(para)))
    return blocks, text, None, {}


class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "button", "select", "iframe"}
    BLOCK = {"p", "li", "td", "th", "dd", "dt", "div", "section", "article", "blockquote", "h1", "h2", "h3",
             "h4", "h5", "h6", "br", "tr", "summary"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.blocks = []
        self.section = None
        self.buf = []
        self.in_head = None
        self.title = ""
        self.in_title = False
        self.links = []

    def flush(self):
        t = clean(" ".join(self.buf))
        self.buf = []
        if not t:
            return
        if self.in_head:
            self.section = t[:140]
        elif len(t) >= 25:
            self.blocks.append((None, self.section, t))

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag == "title":
            self.in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in self.BLOCK:
            self.flush()
            if tag in ("h1", "h2", "h3", "h4"):
                self.in_head = tag

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "title":
            self.in_title = False
        if tag in self.BLOCK:
            self.flush()
            if tag == self.in_head:
                self.in_head = None

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        if self.skip:
            return
        self.buf.append(data)


def extract_html(data, base_url=None):
    text = data.decode("utf-8", errors="replace")
    p = _HTMLText()
    p.feed(text)
    p.flush()
    # de-duplicate repeated boilerplate blocks
    seen, blocks = set(), []
    for b in p.blocks:
        k = b[2][:200]
        if k in seen:
            continue
        seen.add(k)
        blocks.append(b)
    links = []
    for h in p.links:
        if base_url:
            h = urllib.parse.urljoin(base_url, h)
        h = h.split("#")[0]
        if h.startswith("http") and h not in links:
            links.append(h)
    meta = {"title": clean(p.title)}
    m = re.search(r"(?:Last (?:Updated|Modified|Revised)|Updated)[:\s]+([A-Z][a-z]+ \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{2,4})", text)
    if m:
        meta["page_date"] = m.group(1)
    return blocks, "\n".join(b[2] for b in blocks), None, meta, links


def detect_kind(filename, ctype=""):
    fn = (filename or "").lower()
    ctype = (ctype or "").lower()
    if fn.endswith(".pdf") or "application/pdf" in ctype:
        return "pdf"
    if fn.endswith(".docx") or "wordprocessingml" in ctype:
        return "docx"
    if fn.endswith((".xlsx", ".xlsm")) or "spreadsheetml" in ctype:
        return "xlsx"
    if fn.endswith(".csv") or "text/csv" in ctype:
        return "csv"
    if fn.endswith((".htm", ".html")) or "text/html" in ctype:
        return "html"
    if fn.endswith((".txt", ".md", ".json", ".xml")) or ctype.startswith("text/"):
        return "txt"
    return None


def extract(kind, data, base_url=None):
    links = []
    if kind == "pdf":
        blocks, full, pages, meta = extract_pdf(data)
    elif kind == "docx":
        blocks, full, pages, meta = extract_docx(data)
    elif kind == "xlsx":
        blocks, full, pages, meta = extract_xlsx(data)
    elif kind == "csv":
        blocks, full, pages, meta = extract_csv(data)
    elif kind == "html":
        blocks, full, pages, meta, links = extract_html(data, base_url)
    elif kind == "txt":
        blocks, full, pages, meta = extract_txt(data)
    else:
        raise ValueError("Unsupported file type. Supported: PDF, DOCX, XLSX, CSV, TXT/MD, HTML, URL.")
    return chunk_blocks(blocks), full, pages, meta, links


# ------------------------------------------------------------------ metadata detection

DATE_PATTERNS = [
    r"(?:DATE|Date|Effective Date|Revised|Approved|Last Updated)\s*:?\s*([A-Z][a-z]+ \d{1,2},? \d{4})",
    r"\b((?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2},? \d{4})\b",
    r"\b(\d{2}-\d{2}-\d{4})\b",
]


def detect_date(filename, full_text, meta):
    for key in ("page_date",):
        if meta.get(key):
            return meta[key]
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", filename or "")
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    head = (full_text or "")[:4000]
    for pat in DATE_PATTERNS:
        m = re.search(pat, head)
        if m:
            return m.group(1)
    for key in ("created", "modified"):
        v = meta.get(key)
        if v:
            m = re.search(r"(\d{4})(\d{2})(\d{2})", str(v))
            if m:
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)} (file metadata)"
    return None


def detect_year(date_str):
    m = re.search(r"(19|20)\d{2}", date_str or "")
    return int(m.group(0)) if m else None


def detect_doc_type(title, filename, full_text, kind):
    s = f"{title} {filename}".lower()
    body = (full_text or "")[:6000].lower()
    if kind == "html":
        return "WEBPAGE"
    if "strategic plan" in s or ("strategic plan" in body and "measurable outcome" in body):
        return "PLAN"
    if re.search(r"minutes|agenda", s):
        return "MINUTES"
    if re.search(r"report|results|dashboard|survey|analysis|audit|assessment", s):
        return "REPORT"
    if re.search(r"polic|regulation|memorandum|procedure|guideline|scope and policies", s) or "memorandum" in body[:600]:
        return "POLICY"
    if re.search(r"rubric|standard|designation|checklist", s):
        return "STANDARD"
    if kind in ("xlsx", "csv"):
        return "DATA"
    return "OTHER"


def detect_draft(title, filename, full_text):
    s = f"{title} {filename}".lower()
    head = (full_text or "")[:3000].lower()
    reasons = []
    for w in ("proposed", "draft", "for discussion", "not approved", "pending approval"):
        if w in s:
            reasons.append(f"'{w}' appears in the title/filename")
        elif re.search(rf"\b{w}\b", head):
            reasons.append(f"'{w}' appears near the start of the document")
    return reasons


def guess_title(meta, filename, blocks_text, url=None):
    t = (meta.get("title") or "").strip()
    if t and len(t) > 4 and not t.lower().startswith(("microsoft word", "untitled")):
        parts = [x.strip() for x in t.split("|") if x.strip()]
        return (f"{parts[0]} ({parts[1]})" if len(parts) > 1 else parts[0])[:200]
    if filename:
        base = os.path.splitext(os.path.basename(filename))[0]
        return re.sub(r"[-_]+", " ", base).strip()[:200]
    if url:
        return url
    return (blocks_text or "Untitled")[:80]


def sha256(data):
    return hashlib.sha256(data).hexdigest()
