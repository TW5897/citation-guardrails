#!/usr/bin/env python3
# citecheck.py - multi-source reference verification for clinicians and researchers.
# Part of citation-guardrails (https://github.com/TW5897/citation-guardrails). MIT licence.
#
# Standard library only. Python 3.10+. No API keys required.
#
# Subcommands
#   verify    <file|--ids ...>   check that references exist and that cited metadata matches
#   resolve   <doi|pmid|title>   one-off lookup; prints the consensus record
#   evidence  <file|audit.json>  fetch abstracts / open-access full text for claim checking
#   claims    <manuscript>       list every sentence that carries a citation, for claim checking
#   find      "<query>"          search PubMed + OpenAlex and return verified candidates
#   selftest                     run the built-in live fixtures (real, fake, frankenstein, retracted ...)
#
# Statuses
#   VERIFIED                identifiers resolve and every cited field agrees with the sources
#   VERIFIED_WITH_WARNINGS  exists, but a minor field differs (year off by one, journal spelling, pages)
#   MISMATCH                exists, but a major field differs (title, first author, year >1, DOI/PMID conflict)
#   NOT_FOUND               identifiers do not exist, or no source has a work with this title
#   RETRACTED               exists but has been retracted (or withdrawn)
#   UNVERIFIABLE            could not be checked (network failure, offline, nothing parseable)
#
# Exit codes: 0 clean, 1 problems found, 2 usage/input error, 3 no references detected.

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import difflib
import html
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET

__version__ = "0.1.0"
TOOL_NAME = "citecheck"

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

DEFAULT_EMAIL = os.environ.get("CITECHECK_EMAIL", "").strip()
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "").strip()

TITLE_MATCH = 0.92      # >= this: titles agree
TITLE_WARN = 0.80       # between WARN and MATCH: warning; below WARN: major mismatch
YEAR_WARN = 1           # |cited - source| <= 1 -> warning (online-first vs print); > 1 -> major

ALL_SOURCES = ("crossref", "pubmed", "openalex", "europepmc", "semanticscholar")
DEFAULT_SOURCES = ("crossref", "pubmed", "openalex", "europepmc")

# Minimum seconds between calls to one host (polite defaults; NCBI allows 3/s without a key).
HOST_INTERVAL = {
    "eutils.ncbi.nlm.nih.gov": 0.11 if NCBI_API_KEY else 0.35,
    "pmc.ncbi.nlm.nih.gov": 0.35,
    "api.crossref.org": 0.12,
    "api.openalex.org": 0.12,
    "www.ebi.ac.uk": 0.25,
    "api.semanticscholar.org": 1.2,
    "doi.org": 0.25,
}

PREPRINT_PREFIXES = {
    "10.1101": "bioRxiv/medRxiv",
    "10.21203": "Research Square",
    "10.2139": "SSRN",
    "10.48550": "arXiv",
    "10.31219": "OSF Preprints",
    "10.20944": "Preprints.org",
    "10.26434": "ChemRxiv",
    "10.22541": "Authorea",
    "10.32388": "Qeios",
}

NAME_PARTICLES = {"von", "van", "de", "del", "della", "dos", "da", "le", "la", "du", "den", "der", "ten", "di", "al", "el", "bin", "ibn", "mac", "mc"}
JOURNAL_STOP = {"the", "of", "and", "for", "in", "on", "a", "an", "&", "de", "la", "der", "le", "et"}

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>\]\[{}]+", re.I)
PMID_RE = re.compile(r"\bPMID\s*:?\s*(\d{5,9})\b", re.I)
PMCID_RE = re.compile(r"\b(PMC\d{5,9})\b", re.I)
YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
URL_RE = re.compile(r"https?://\S+", re.I)


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return _dt.date.today().isoformat()


def eprint(*a: Any) -> None:
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------

@dataclass
class Ref:
    ref_id: str
    raw: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)   # surnames as parsed, in order
    first_author: str = ""
    year: str = ""
    journal: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    url: str = ""
    style_guess: str = ""
    parse_confidence: str = "low"   # high (identifiers), medium (title+author+year), low
    key: str = ""                   # bibtex key / RIS id / numeric label if any


@dataclass
class Hit:
    """What one source said about one lookup."""
    source: str
    query: str                 # 'doi:...', 'pmid:...', 'title:...'
    url: str = ""
    ok: bool = True            # the request itself succeeded (404 counts as ok, found=False)
    found: bool = False
    error: str = ""
    title: str = ""
    subtitle: str = ""
    authors: list[str] = field(default_factory=list)   # surnames
    authors_full: list[str] = field(default_factory=list)   # 'Family, Given' as the source gives them
    first_author: str = ""
    year: str = ""
    journal: str = ""
    journal_abbrev: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    work_type: str = ""
    retracted: bool = False
    retraction_notes: list[str] = field(default_factory=list)
    concern: bool = False
    correction: bool = False
    preprint: bool = False
    preprint_server: str = ""
    vor_doi: str = ""
    abstract: str = ""
    retrieved: str = ""
    candidates: list[dict] = field(default_factory=list)   # for title searches


@dataclass
class Check:
    field: str
    cited: str
    source_value: str
    source: str
    outcome: str      # match | warning | mismatch | skipped
    detail: str = ""


@dataclass
class Result:
    ref_id: str
    status: str = "UNVERIFIABLE"
    flags: list[str] = field(default_factory=list)
    confidence: str = ""           # high | medium | low
    sources_confirming: list[str] = field(default_factory=list)
    found_by: str = ""             # doi | pmid | title
    resolved: dict = field(default_factory=dict)   # consensus record
    checks: list[Check] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    hits: list[dict] = field(default_factory=list)
    cited: dict = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Text utilities
# --------------------------------------------------------------------------------------

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def norm_title(s: str) -> str:
    s = html.unescape(strip_tags(s or ""))
    s = strip_accents(s).lower()
    s = re.sub(r"^\s*(retracted|withdrawn|retraction)\s*(article|:)?\s*[:\-]?\s*", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return squash(s)


def title_similarity(a: str, b: str) -> float:
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    # Cited titles often drop a subtitle; treat clean containment as a near-match.
    if len(short) >= 25 and short in long_ and len(short) / len(long_) >= 0.55:
        ratio = max(ratio, 0.93)
    # ... or cite exactly the main title of a 'Main title: subtitle' record
    for x, y in ((a, b), (b, a)):
        if ":" in (y or "") and ":" not in (x or ""):
            main = norm_title(y.split(":", 1)[0])
            if len(main) >= 15 and main == norm_title(x):
                ratio = max(ratio, 0.93)
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb:
        jacc = len(ta & tb) / len(ta | tb)
        if jacc >= 0.9:
            ratio = max(ratio, 0.92)
    return round(ratio, 3)


def norm_surname(name: str) -> str:
    n = strip_accents(name or "").lower().strip()
    n = re.sub(r"[^a-z\s\-']", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def surname_from_display(name: str) -> str:
    """'Stefan Mark Nidorf' -> 'Nidorf'; 'Nidorf SM' -> 'Nidorf'; 'van der Meer J' -> 'van der Meer'."""
    name = squash(name or "")
    if not name:
        return ""
    if "," in name:                       # 'Nidorf, Stefan M' / 'Nidorf, S. M.'
        return name.split(",", 1)[0].strip()
    toks = name.split()
    # Trailing initials block (PubMed style 'Nidorf SM', 'Fiolet ATL')
    if len(toks) >= 2 and re.fullmatch(r"[A-Z]{1,4}\.?", toks[-1]) and not re.fullmatch(r"[A-Z]{1,4}\.?", toks[0]):
        core = toks[:-1]
    else:
        # Display order 'Stefan Mark Nidorf' -> last token, keeping particles
        core = [toks[-1]]
        i = len(toks) - 2
        while i >= 0 and toks[i].lower() in NAME_PARTICLES:
            core.insert(0, toks[i])
            i -= 1
    return " ".join(core)


def surnames_match(cited: str, actual: str) -> bool:
    a, b = norm_surname(cited), norm_surname(actual)
    if not a or not b:
        return True   # cannot judge -> do not flag
    if a == b:
        return True
    strip = lambda s: re.sub(r"^(?:(?:" + "|".join(NAME_PARTICLES) + r")\s+)+", "", s)
    a2, b2 = strip(a), strip(b)
    if a2 and b2 and (a2 == b2):
        return True
    if a.replace("-", " ") == b.replace("-", " ") or a.replace("-", "") == b.replace("-", ""):
        return True
    # Hyphenated/compound surnames cited by one half ('Abd-Alrazaq' vs 'Alrazaq')
    parts_a, parts_b = set(re.split(r"[\s\-]+", a2)), set(re.split(r"[\s\-]+", b2))
    if parts_a and parts_b and (parts_a <= parts_b or parts_b <= parts_a) and max(len(p) for p in (parts_a | parts_b)) >= 4:
        return True
    return False


def norm_journal_tokens(s: str) -> list[str]:
    s = strip_accents(html.unescape(s or "")).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return [t for t in s.split() if t not in JOURNAL_STOP]


def journals_match(cited: str, candidates: list[str]) -> bool:
    ct = norm_journal_tokens(cited)
    if not ct:
        return True
    for cand in candidates:
        kt = norm_journal_tokens(cand)
        if not kt:
            continue
        if ct == kt:
            return True
        # abbreviation vs full name: each abbreviated token is a prefix of the matching full token
        def abbrev_of(short: list[str], full: list[str]) -> bool:
            if len(short) > len(full):
                return False
            j = 0
            for tok in short:
                while j < len(full) and not full[j].startswith(tok):
                    j += 1
                if j >= len(full):
                    return False
                j += 1
            return True
        if abbrev_of(ct, kt) or abbrev_of(kt, ct):
            return True
        a, b = set(ct), set(kt)
        if len(a & b) / len(a | b) >= 0.6:
            return True
    return False


def clean_doi(doi: str) -> str:
    d = (doi or "").strip()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", d, flags=re.I)
    d = d.rstrip(".,;:)]}>\"'")
    while d.endswith(")") and d.count("(") < d.count(")"):
        d = d[:-1]
    return d.lower()


def doi_prefix(doi: str) -> str:
    return doi.split("/", 1)[0] if doi else ""


def year_int(y: str) -> Optional[int]:
    m = re.search(r"(19|20)\d{2}", str(y or ""))
    return int(m.group(0)) if m else None


# --------------------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------------------

def read_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    out = []
    for p in root.findall(".//w:p", ns):
        parts = [t.text or "" for t in p.findall(".//w:t", ns)]
        if parts:
            out.append("".join(parts))
    return "\n".join(out)


def read_text(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        return read_docx_text(path)
    return path.read_text(encoding="utf-8", errors="replace")


def detect_format(path: Path, override: str = "auto") -> str:
    if override and override != "auto":
        return override
    ext = path.suffix.lower()
    return {
        ".bib": "bibtex", ".ris": "ris", ".json": "json", ".csv": "csv", ".tsv": "tsv",
        ".docx": "text", ".md": "text", ".qmd": "text", ".rmd": "text", ".txt": "text", ".tex": "tex",
    }.get(ext, "text")


def _bib_fields(body: str) -> dict[str, str]:
    """Parse 'field = {value}' / 'field = "value"' / 'field = 123' with balanced braces."""
    fields: dict[str, str] = {}
    i, n = 0, len(body)
    while i < n:
        m = re.compile(r"\s*([A-Za-z_][\w\-]*)\s*=\s*", re.S).match(body, i)
        if not m:
            i += 1
            continue
        name = m.group(1).lower()
        i = m.end()
        if i >= n:
            break
        if body[i] == "{":
            depth, j = 0, i
            while j < n:
                if body[j] == "{":
                    depth += 1
                elif body[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            val = body[i + 1:j]
            i = j + 1
        elif body[i] == '"':
            j = body.find('"', i + 1)
            while j != -1 and body[j - 1] == "\\":
                j = body.find('"', j + 1)
            if j == -1:
                j = n
            val = body[i + 1:j]
            i = j + 1
        else:
            m2 = re.compile(r"[^,}\n]+").match(body, i)
            val = m2.group(0) if m2 else ""
            i = m2.end() if m2 else i + 1
        val = re.sub(r"[{}]", "", val)
        fields[name] = squash(val.replace("--", "-").replace("\\&", "&"))
        m3 = re.compile(r"\s*,").match(body, i)
        if m3:
            i = m3.end()
    return fields


def bib_author_surnames(author_field: str) -> list[str]:
    out = []
    for a in re.split(r"\s+and\s+", author_field or "", flags=re.I):
        a = squash(a)
        if not a or a.lower() in {"others", "et al", "et al."}:
            continue
        out.append(surname_from_display(a))
    return out


def parse_bibtex(text: str) -> list[Ref]:
    refs: list[Ref] = []
    for m in re.finditer(r"@(\w+)\s*\{\s*([^,\s]+)\s*,(.*?)\n\}", text, flags=re.S):
        etype, key, body = m.group(1).lower(), m.group(2), m.group(3)
        if etype in {"comment", "preamble", "string"}:
            continue
        f = _bib_fields(body)
        authors = bib_author_surnames(f.get("author", ""))
        doi = clean_doi(f.get("doi", "")) if f.get("doi") else ""
        pmid = re.sub(r"\D", "", f.get("pmid", "")) if f.get("pmid") else ""
        if not doi:
            dm = DOI_RE.search(f.get("url", "") + " " + f.get("note", ""))
            doi = clean_doi(dm.group(0)) if dm else ""
        if not pmid:
            pm = PMID_RE.search(f.get("note", "") + " " + f.get("annote", ""))
            pmid = pm.group(1) if pm else ""
        ref = Ref(
            ref_id=key, key=key, raw=squash(m.group(0))[:600],
            title=squash(f.get("title", "")), authors=authors, first_author=authors[0] if authors else "",
            year=(year_int(f.get("year", "")) and str(year_int(f.get("year", "")))) or "",
            journal=squash(f.get("journal") or f.get("journaltitle") or f.get("booktitle") or ""),
            volume=f.get("volume", ""), issue=f.get("number", ""), pages=f.get("pages", ""),
            doi=doi, pmid=pmid, pmcid=f.get("pmcid", ""), url=f.get("url", ""), style_guess="bibtex",
        )
        ref.parse_confidence = "high" if (doi or pmid) else ("medium" if (ref.title and ref.year) else "low")
        refs.append(ref)
    return refs


def parse_ris(text: str) -> list[Ref]:
    refs: list[Ref] = []
    cur: dict[str, list[str]] = {}
    n = 0
    for line in text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])\s{2}-\s?(.*)$", line.rstrip())
        if not m:
            continue
        tag, val = m.group(1), m.group(2).strip()
        if tag == "TY":
            cur = {"TY": [val]}
        elif tag == "ER":
            n += 1
            authors = [surname_from_display(a) for a in cur.get("AU", []) + cur.get("A1", [])]
            doi = ""
            for cand in cur.get("DO", []) + cur.get("UR", []) + cur.get("L3", []):
                dm = DOI_RE.search(cand)
                if dm:
                    doi = clean_doi(dm.group(0))
                    break
            pmid = ""
            for cand in cur.get("AN", []) + cur.get("ID", []) + cur.get("N1", []):
                pm = PMID_RE.search(cand) or re.fullmatch(r"(\d{5,9})", cand.strip())
                if pm:
                    pmid = pm.group(1)
                    break
            year = ""
            for cand in cur.get("PY", []) + cur.get("Y1", []) + cur.get("DA", []):
                yi = year_int(cand)
                if yi:
                    year = str(yi)
                    break
            sp, ep = (cur.get("SP") or [""])[0], (cur.get("EP") or [""])[0]
            pages = sp + ("-" + ep if ep else "")
            ref = Ref(
                ref_id=(cur.get("ID") or [f"ris_{n}"])[0] or f"ris_{n}", key=(cur.get("ID") or [""])[0],
                raw=squash(" ".join(v for vals in cur.values() for v in vals))[:600],
                title=squash((cur.get("TI") or cur.get("T1") or [""])[0]),
                authors=authors, first_author=authors[0] if authors else "", year=year,
                journal=squash((cur.get("JO") or cur.get("JF") or cur.get("T2") or cur.get("JA") or [""])[0]),
                volume=(cur.get("VL") or [""])[0], issue=(cur.get("IS") or [""])[0], pages=pages,
                doi=doi, pmid=pmid, url=(cur.get("UR") or [""])[0], style_guess="ris",
            )
            ref.parse_confidence = "high" if (doi or pmid) else ("medium" if (ref.title and ref.year) else "low")
            refs.append(ref)
            cur = {}
        else:
            cur.setdefault(tag, []).append(val)
    return refs


def parse_json_refs(text: str) -> list[Ref]:
    data = json.loads(text)
    items = data.get("references") if isinstance(data, dict) else data
    if isinstance(data, dict) and "records" in data and not items:
        items = [r.get("cited", r) for r in data["records"]]
    refs: list[Ref] = []
    for i, it in enumerate(items or [], 1):
        if isinstance(it, str):
            refs.append(parse_free_citation(it, f"ref_{i}"))
            continue
        authors = it.get("authors") or []
        if isinstance(authors, str):
            authors = [a.strip() for a in re.split(r";|,\s*(?=[A-Z][a-z])| and ", authors) if a.strip()]
        authors = [surname_from_display(a) if isinstance(a, str) else surname_from_display(a.get("family") or a.get("name") or "") for a in authors]
        ref = Ref(
            ref_id=str(it.get("id") or it.get("ref_id") or it.get("key") or f"ref_{i}"),
            raw=json.dumps(it, ensure_ascii=False)[:600],
            title=squash(it.get("title", "")), authors=authors,
            first_author=squash(it.get("first_author", "")) or (authors[0] if authors else ""),
            year=str(year_int(str(it.get("year", ""))) or ""), journal=squash(it.get("journal", "")),
            volume=str(it.get("volume", "")), issue=str(it.get("issue", "")), pages=str(it.get("pages", "")),
            doi=clean_doi(it.get("doi", "")) if it.get("doi") else "", pmid=re.sub(r"\D", "", str(it.get("pmid", ""))),
            pmcid=str(it.get("pmcid", "")), url=it.get("url", ""), style_guess="json",
        )
        ref.parse_confidence = "high" if (ref.doi or ref.pmid) else ("medium" if (ref.title and ref.year) else "low")
        refs.append(ref)
    return refs


def parse_delimited(text: str, delimiter: str) -> list[Ref]:
    rows = list(csv.DictReader(io.StringIO(text), delimiter=delimiter))
    refs: list[Ref] = []
    for i, row in enumerate(rows, 1):
        low = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        authors_raw = low.get("authors") or low.get("author") or ""
        authors = [surname_from_display(a) for a in re.split(r";|\s+and\s+|,\s*(?=[A-Z][a-z]+\s+[A-Z]{1,3}\b)", authors_raw) if a.strip()] if authors_raw else []
        ref = Ref(
            ref_id=low.get("id") or low.get("ref_id") or low.get("key") or f"ref_{i}",
            raw=squash(" | ".join(v for v in low.values() if v))[:600],
            title=low.get("title", ""), authors=authors, first_author=low.get("first_author") or (authors[0] if authors else ""),
            year=str(year_int(low.get("year", "")) or ""), journal=low.get("journal", ""),
            volume=low.get("volume", ""), issue=low.get("issue", ""), pages=low.get("pages", ""),
            doi=clean_doi(low.get("doi", "")) if low.get("doi") else "", pmid=re.sub(r"\D", "", low.get("pmid", "")),
            url=low.get("url", ""), style_guess="table",
        )
        ref.parse_confidence = "high" if (ref.doi or ref.pmid) else ("medium" if (ref.title and ref.year) else "low")
        refs.append(ref)
    return refs


def parse_tex_bibitems(text: str) -> list[Ref]:
    refs = []
    for i, m in enumerate(re.finditer(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}(.*?)(?=\\bibitem|\\end\{thebibliography\}|\Z)", text, flags=re.S), 1):
        key, body = m.group(1), squash(re.sub(r"\\(?:emph|textit|textbf|newblock)\b\{?", " ", m.group(2)).replace("}", " "))
        ref = parse_free_citation(body, key)
        ref.key = key
        refs.append(ref)
    return refs


REF_HEADING_RE = re.compile(r"(?im)^\s*(?:#+\s*)?(?:\d+\.?\s*)?(references|reference list|bibliography|works cited|literature cited)\s*:?\s*$")
ITEM_START_RE = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})[.)]|(\d{1,3})\s{2,}|[-*•]\s+)\s*")


def reference_section(text: str) -> str:
    m = REF_HEADING_RE.search(text)
    if not m:
        return text
    tail = text[m.end():]
    # stop at the next top-level heading that looks like a non-reference section
    stop = re.search(r"(?im)^\s*#+\s*(appendix|supplement|tables?|figures?|acknowledg)", tail)
    return tail[:stop.start()] if stop else tail


def looks_like_citation(txt: str) -> bool:
    return bool(DOI_RE.search(txt) or PMID_RE.search(txt) or AUTHOR_LIST_VANCOUVER.match(txt) or APA_YEAR_RE.search(txt))


def split_reference_items(section: str, strict: bool = False) -> list[tuple[str, str]]:
    """Return (label, text) items. Label is the numeric label when present.
    strict=True (no References heading found): keep only items shaped like citations, so body
    sentences are not mistaken for references."""
    lines = [ln.rstrip() for ln in section.splitlines()]
    items: list[tuple[str, str]] = []
    cur_label, cur = "", ""
    numbered = sum(1 for ln in lines if ITEM_START_RE.match(ln) and ln.strip()) >= 2
    if numbered:
        for ln in lines:
            if not ln.strip():
                continue
            m = ITEM_START_RE.match(ln)
            if m:
                if cur:
                    items.append((cur_label, squash(cur)))
                cur_label = next((g for g in m.groups() if g), "") or ""
                cur = ln[m.end():]
            else:
                cur = (cur + " " + ln.strip()) if cur else ln.strip()
        if cur:
            items.append((cur_label, squash(cur)))
    else:
        # paragraph-separated
        para = []
        for ln in lines + [""]:
            if ln.strip():
                para.append(ln.strip())
            elif para:
                items.append(("", squash(" ".join(para))))
                para = []
    # keep only things that look like references
    good = []
    for label, txt in items:
        if len(txt) < 25:
            continue
        if strict:
            if looks_like_citation(txt):
                good.append((label, txt))
        elif DOI_RE.search(txt) or PMID_RE.search(txt) or YEAR_RE.search(txt) or len(txt.split()) >= 8:
            good.append((label, txt))
    return good


_PARTICLES_RE = r"(?:(?:[Vv]an|[Vv]on|[Dd]e|[Dd]er|[Dd]en|[Dd]el|[Dd]ella|[Dd]a|[Dd]i|[Ll]a|[Ll]e|[Dd]u|[Tt]en|[Tt]er|[Dd]os|[Dd]as|[Ee]l|[Aa]l|[Bb]in|[Ii]bn|[Mm]ac)\s)*"
_SURNAME_RE = r"[A-Z][\w'’\-]+(?:\s[A-Z][\w'’\-]+)?"
_INITIALS_RE = r"(?:[A-Z]{1,3}(?:-[A-Z])?|[A-Z]\.(?:\s?-?[A-Z]\.)*)"
_AUTHOR_ONE = _PARTICLES_RE + _SURNAME_RE + r"\s" + _INITIALS_RE
AUTHOR_LIST_VANCOUVER = re.compile(
    r"^(?P<authors>(?:" + _AUTHOR_ONE + r"(?:,\s*|;\s*|\s*&\s*|\s+and\s+)?)+"
    r"(?:,?\s*(?:et\s+al\.?|and\s+others|\.{3}))?"
    r"(?:,?\s*(?:for\s+the\s+|on\s+behalf\s+of\s+the\s+)?[A-Z][\w'’\-\s]+(?:Group|Investigators|Committee|Collaborat\w+|Consortium|Study|Network|Team)\b\.?)?)"
    r"(?:\s*[.;]|(?<=\.))\s+(?P<rest>.*)$"
)
# A publication-year tail: '. 2020;383(19):1838-1847', ' 2021;372:n71', '. 2019 May 15;73(21):2756', '. 2020.'
YEAR_TAIL_VANCOUVER = re.compile(
    r"(?<![\w\-])(?P<year>(?:19|20)\d{2})\s*(?:[A-Z][a-z]{2}\.?(?:\s\d{1,2})?(?:-[A-Z][a-z]{2})?)?"
    r"(?:\s*[;,]\s*(?P<vol>\d+[A-Za-z]?|Suppl\w*\s*\d*)?(?:\s*\((?P<issue>[^)]{1,20})\))?\s*(?::\s*(?P<pages>[\w\-–.]+))?)?"
    r"(?=\s*(?:[;:,.]|$))"
)
APA_YEAR_RE = re.compile(r"\((?P<year>(?:19|20)\d{2})[a-z]?(?:,\s*[A-Za-z]+(?:\s\d{1,2})?)?\)\.?\s*")


def parse_free_citation(raw: str, ref_id: str) -> Ref:
    """Heuristic parser for Vancouver / AMA / APA style strings."""
    text = squash(html.unescape(raw))
    ref = Ref(ref_id=ref_id, raw=text[:600])
    dm = DOI_RE.search(text)
    if dm:
        ref.doi = clean_doi(dm.group(0))
    pm = PMID_RE.search(text)
    if pm:
        ref.pmid = pm.group(1)
    pc = PMCID_RE.search(text)
    if pc:
        ref.pmcid = pc.group(1).upper()
    um = URL_RE.search(text)
    if um:
        ref.url = um.group(0).rstrip(".,;)")
    # strip identifier noise before parsing fields
    body = DOI_RE.sub(" ", text)
    body = re.sub(r"\b(?:doi|https?://(?:dx\.)?doi\.org/?)\s*:?\s*", " ", body, flags=re.I)
    body = PMID_RE.sub(" ", body)
    body = PMCID_RE.sub(" ", body)
    body = re.sub(r"\bPMCID\s*:?\s*", " ", body, flags=re.I)
    body = URL_RE.sub(" ", body)
    body = re.sub(r"\b(?:Available|Accessed|Retrieved|Epub)\b.*$", " ", body, flags=re.I)
    body = re.sub(r"\[\s*(?:Internet|published online[^\]]*|Epub[^\]]*)\s*\]", " ", body, flags=re.I)
    body = squash(body.replace("*", "").replace("_", " ")).strip(" .;,")

    apa = APA_YEAR_RE.search(body)
    if apa and apa.start() < len(body) * 0.6:
        ref.style_guess = "apa"
        ref.year = apa.group("year")
        authors_part = body[:apa.start()].strip(" .,&")
        rest = body[apa.end():].strip()
        # authors: 'Nidorf, S. M., Fiolet, A. T. L., & Mosterd, A.'
        for chunk in re.split(r",\s*(?=[A-Z][\w'’\-]+,\s*[A-Z]\.)|\s*&\s*|,\s*\.{3}\s*|\s+and\s+", authors_part):
            chunk = chunk.strip(" ,&.")
            if not chunk or chunk.lower().startswith("et al"):
                continue
            ref.authors.append(surname_from_display(chunk))
        # title: up to the first '. ' that is followed by a capitalised journal name
        m = re.match(r"(?P<title>.+?[.?!])\s+(?P<journal>[A-Z][^,]{2,90}?),\s*(?P<vol>\d+)?(?:\((?P<issue>[^)]+)\))?,?\s*(?P<pages>[\w\-–]+)?", rest)
        if m:
            ref.title = m.group("title").rstrip(".")
            ref.journal = m.group("journal").strip()
            ref.volume = m.group("vol") or ""
            ref.issue = m.group("issue") or ""
            ref.pages = m.group("pages") or ""
        else:
            parts = [p for p in re.split(r"(?<=[.?!])\s+", rest) if p]
            if parts:
                ref.title = parts[0].rstrip(".")
            if len(parts) > 1:
                ref.journal = re.split(r",", parts[1])[0].strip()
    else:
        ref.style_guess = "vancouver"
        m = AUTHOR_LIST_VANCOUVER.match(body)
        if m:
            authors_part, rest = m.group("authors"), m.group("rest")
            for chunk in re.split(r",\s*|;\s*|\s+and\s+|\s*&\s*", authors_part):
                chunk = chunk.strip(" .")
                if not chunk or chunk.lower().startswith("et al") or re.search(r"\b(Group|Investigators|Committee|Consortium|Collaborat)", chunk):
                    continue
                ref.authors.append(surname_from_display(chunk))
        else:
            rest = body
        # The publication year sits after the journal: take the LAST year-tail whose preceding text is a
        # sentence boundary, so a year inside the title ("The PRISMA 2020 statement") is not mistaken for it.
        tail = None
        for cand in YEAR_TAIL_VANCOUVER.finditer(rest):
            before = rest[:cand.start()].rstrip()
            if before.endswith((".", ";", ",")) or re.search(r"\b[A-Z][A-Za-z&]*\s*$", before):
                tail = cand
        if tail:
            ref.year = tail.group("year")
            ref.volume = (tail.group("vol") or "").strip()
            ref.issue = (tail.group("issue") or "").strip()
            ref.pages = (tail.group("pages") or "").strip(" .")
            before = rest[:tail.start()].rstrip(" .;,")
            chunks = [c.strip(" .") for c in re.split(r"(?<=[.?!])\s+(?=[A-Z0-9\"'“(\[])", before) if c.strip(" .")]
            if len(chunks) >= 2:
                journal = chunks[-1]
                # a journal name is short and has no sentence punctuation; otherwise it is part of the title
                if len(journal.split()) <= 12 and not re.search(r"[?!]", journal):
                    ref.journal = journal
                    title_chunks = chunks[:-1]
                else:
                    title_chunks = chunks
            else:
                title_chunks = chunks
            ref.title = " ".join(title_chunks).strip()
        else:
            sentences = [s.strip(" .") for s in re.split(r"(?<=[.?!])\s+(?=[A-Z0-9\"'“(\[])", rest) if s.strip(" .")]
            ref.title = sentences[0] if sentences else ""
            # only trust a year that sits outside the title ("The PRISMA 2020 statement" is not a 2020 paper)
            after_title = rest[len(ref.title):] if ref.title and rest.startswith(ref.title) else (rest if not ref.title else "")
            ym = YEAR_RE.search(after_title)
            ref.year = ym.group(0) if ym else ""
        # if the "title" still looks like an author list, we failed to split: blank it
        if ref.title and AUTHOR_LIST_VANCOUVER.match(ref.title + ". x"):
            ref.title = ""
    ref.authors = [a for a in ref.authors if a]
    ref.first_author = ref.authors[0] if ref.authors else ""
    ref.title = squash(strip_tags(ref.title)).strip("\"'“”")
    if ref.doi or ref.pmid:
        ref.parse_confidence = "high"
    elif ref.title and ref.year and ref.first_author:
        ref.parse_confidence = "medium"
    else:
        ref.parse_confidence = "low"
    return ref


def parse_reference_list(text: str) -> list[Ref]:
    has_heading = REF_HEADING_RE.search(text) is not None
    section = reference_section(text)
    items = split_reference_items(section, strict=not has_heading)
    if not items and has_heading:
        items = split_reference_items(text, strict=True)
    refs = []
    for i, (label, txt) in enumerate(items, 1):
        rid = f"ref_{label}" if label else f"ref_{i}"
        refs.append(parse_free_citation(txt, rid))
    return refs


def parse_input(path: Path, fmt: str = "auto") -> list[Ref]:
    kind = detect_format(path, fmt)
    text = read_text(path)
    if kind == "bibtex":
        return parse_bibtex(text)
    if kind == "ris":
        return parse_ris(text)
    if kind == "json":
        return parse_json_refs(text)
    if kind == "csv":
        return parse_delimited(text, ",")
    if kind == "tsv":
        return parse_delimited(text, "\t")
    if kind == "tex":
        refs = parse_tex_bibitems(text)
        return refs or parse_reference_list(text)
    return parse_reference_list(text)


def refs_from_ids(ids: list[str]) -> list[Ref]:
    refs = []
    for i, tok in enumerate(ids, 1):
        tok = tok.strip()
        dm = DOI_RE.search(tok)
        if dm:
            refs.append(Ref(ref_id=f"id_{i}", raw=tok, doi=clean_doi(dm.group(0)), parse_confidence="high"))
        elif re.fullmatch(r"(?:PMID:?\s*)?\d{5,9}", tok, flags=re.I):
            refs.append(Ref(ref_id=f"id_{i}", raw=tok, pmid=re.sub(r"\D", "", tok), parse_confidence="high"))
        else:
            r = parse_free_citation(tok, f"id_{i}")
            looks_like_citation = bool(AUTHOR_LIST_VANCOUVER.match(squash(tok))) or bool(APA_YEAR_RE.search(tok))
            if not looks_like_citation and len(tok.split()) >= 3:
                # a bare title: do not read a year or an author out of it
                r.title, r.year, r.journal, r.authors, r.first_author = squash(tok).strip(" ."), "", "", [], ""
                r.parse_confidence = "medium"
            refs.append(r)
    return refs


# --------------------------------------------------------------------------------------
# HTTP layer: throttle, retry, cache
# --------------------------------------------------------------------------------------

class Http:
    def __init__(self, email: str = "", timeout: int = 20, cache_path: Optional[Path] = None, ttl_days: int = 7, quiet: bool = False):
        self.email = email or DEFAULT_EMAIL
        self.timeout = timeout
        self.cache_path = cache_path
        self.ttl = ttl_days * 86400
        self.quiet = quiet
        self.last_call: dict[str, float] = {}
        self.cache: dict[str, dict] = {}
        self.calls = 0
        self.cache_hits = 0
        if cache_path and cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                self.cache = {}

    @property
    def user_agent(self) -> str:
        contact = f"mailto:{self.email}" if self.email else "no-contact-email-set"
        return f"{TOOL_NAME}/{__version__} (https://github.com/TW5897/citation-guardrails; {contact})"

    def save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache), encoding="utf-8")
        except Exception as exc:  # cache is best-effort
            if not self.quiet:
                eprint(f"[citecheck] cache not saved: {exc}")

    def _throttle(self, host: str) -> None:
        gap = HOST_INTERVAL.get(host, 0.3)
        last = self.last_call.get(host, 0.0)
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        self.last_call[host] = time.monotonic()

    def get(self, url: str, method: str = "GET", accept: str = "") -> tuple[int, str, str]:
        """Returns (status, body, final_url). status 0 = transport failure. Caches 200/404."""
        key = f"{method} {redact_url(url)}"   # the cache file may be shared: keep emails and keys out of it
        now = time.time()
        ent = self.cache.get(key)
        if ent and now - ent.get("ts", 0) < self.ttl:
            self.cache_hits += 1
            return ent["status"], ent.get("body", ""), ent.get("final_url", url)
        host = urllib.parse.urlparse(url).netloc
        headers = {"User-Agent": self.user_agent}
        if accept:
            headers["Accept"] = accept
        delay = 1.0
        status, body, final_url = 0, "", url
        for attempt in range(4):
            self._throttle(host)
            self.calls += 1
            req = urllib.request.Request(url, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status = resp.status
                    final_url = resp.geturl()
                    body = "" if method == "HEAD" else resp.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as e:
                status = e.code
                final_url = e.geturl() or url
                if status in (429, 500, 502, 503, 504) and attempt < 3:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    try:
                        sleep_for = float(ra) if ra else delay
                    except ValueError:
                        sleep_for = delay
                    time.sleep(min(sleep_for, 30))
                    delay *= 2
                    continue
                try:
                    body = e.read().decode("utf-8", "replace") if method != "HEAD" else ""
                except Exception:
                    body = ""
                break
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                status, body = 0, str(e)
                if attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                break
        if status in (200, 404, 410):
            self.cache[key] = {"status": status, "body": body, "final_url": final_url, "ts": now}
        return status, body, final_url

    def get_json(self, url: str) -> tuple[int, Any]:
        status, body, _ = self.get(url, accept="application/json")
        if status != 200:
            return status, None
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, None


def ncbi_params(http: Http, **kw: str) -> str:
    kw = {k: v for k, v in kw.items() if v}
    kw["tool"] = TOOL_NAME
    if http.email:
        kw["email"] = http.email
    if NCBI_API_KEY:
        kw["api_key"] = NCBI_API_KEY
    return urllib.parse.urlencode(kw)


# --------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------

def redact_url(url: str) -> str:
    """Audit files may be shared or committed: never record contact emails or API keys in them."""
    return re.sub(r"([?&])(email|api_key|mailto)=[^&]*", r"\1\2=REDACTED", url or "")


def _hit(source: str, query: str, url: str) -> Hit:
    return Hit(source=source, query=query, url=redact_url(url), retrieved=utcnow())


def _mark_preprint(h: Hit) -> None:
    pref = doi_prefix(h.doi)
    if pref in PREPRINT_PREFIXES and not h.preprint:
        h.preprint = True
        h.preprint_server = h.preprint_server or PREPRINT_PREFIXES[pref]


def crossref_by_doi(http: Http, doi: str) -> Hit:
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    h = _hit("crossref", f"doi:{doi}", url)
    status, data = http.get_json(url)
    if status == 404:
        return h
    if status != 200 or not data or data.get("status") != "ok":
        h.ok, h.error = False, f"HTTP {status}"
        return h
    m = data.get("message", {})
    h.found = True
    h.title = squash(strip_tags(html.unescape(" ".join(m.get("title") or []))))
    h.subtitle = squash(strip_tags(html.unescape(" ".join(m.get("subtitle") or []))))
    for a in m.get("author") or []:
        fam = a.get("family") or a.get("name") or ""
        if fam:
            h.authors.append(squash(fam))
            h.authors_full.append(squash(fam) + (f", {squash(a['given'])}" if a.get("given") else ""))
    h.first_author = h.authors[0] if h.authors else ""
    for k in ("published-print", "published-online", "issued", "created"):
        parts = ((m.get(k) or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            h.year = str(parts[0])
            if k == "published-print":
                break
            if k == "issued":
                break
    h.journal = squash(" ".join(m.get("container-title") or []))
    h.journal_abbrev = squash(" ".join(m.get("short-container-title") or []))
    h.volume, h.issue, h.pages = str(m.get("volume") or ""), str(m.get("issue") or ""), str(m.get("page") or "")
    h.doi = clean_doi(m.get("DOI") or doi)
    h.work_type = m.get("type") or ""
    if m.get("abstract"):
        h.abstract = squash(strip_tags(html.unescape(m["abstract"])))
    for u in m.get("update-to") or []:
        t = (u.get("type") or "").lower()
        label = f"{u.get('label') or t} (CrossRef update-to, DOI {u.get('DOI')})"
        if t in ("retraction", "withdrawal", "removal"):
            h.retracted = True
            h.retraction_notes.append(label)
        elif "concern" in t:
            h.concern = True
            h.retraction_notes.append(label)
        elif t in ("correction", "erratum", "corrigendum"):
            h.correction = True
    for u in m.get("updated-by") or []:
        t = (u.get("type") or "").lower()
        label = f"{u.get('label') or t} (CrossRef updated-by, DOI {u.get('DOI')})"
        if t in ("retraction", "withdrawal", "removal"):
            h.retracted = True
            h.retraction_notes.append(label)
        elif "concern" in t:
            h.concern = True
            h.retraction_notes.append(label)
        elif t in ("correction", "erratum", "corrigendum"):
            h.correction = True
    if re.match(r"^\s*(retracted|withdrawn)\b", h.title, flags=re.I):
        h.retracted = True
        h.retraction_notes.append("title carries RETRACTED/WITHDRAWN label (CrossRef)")
    if h.work_type == "posted-content":
        h.preprint = True
        h.preprint_server = squash(m.get("group-title") or (m.get("institution") or [{}])[0].get("name", "") if isinstance(m.get("institution"), list) else m.get("group-title") or "")
    rel = m.get("relation") or {}
    for r in rel.get("is-preprint-of") or []:
        if r.get("id"):
            h.vor_doi = clean_doi(r["id"])
            h.preprint = True
    _mark_preprint(h)
    return h


def doiorg_resolves(http: Http, doi: str) -> Hit:
    url = "https://doi.org/" + urllib.parse.quote(doi, safe="/()")
    h = _hit("doi.org", f"doi:{doi}", url)
    status, _, final = http.get(url, method="HEAD")
    if status == 0:
        h.ok, h.error = False, "transport failure"
    elif status in (404, 410):
        h.found = False
    else:
        h.found = True
        h.doi = doi
        h.url = final
    return h


def pubmed_by_pmid(http: Http, pmid: str, want_abstract: bool = False) -> Hit:
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + ncbi_params(http, db="pubmed", id=pmid, retmode="xml")
    h = _hit("pubmed", f"pmid:{pmid}", url)
    status, body, _ = http.get(url)
    if status != 200:
        h.ok, h.error = False, f"HTTP {status}"
        return h
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        h.ok, h.error = False, "XML parse error"
        return h
    art = root.find(".//PubmedArticle")
    if art is None:
        h.found = False
        return h
    _fill_from_pubmed_xml(h, art, want_abstract)
    return h


def _fill_from_pubmed_xml(h: Hit, art: ET.Element, want_abstract: bool) -> None:
    h.found = True
    med = art.find("MedlineCitation")
    a = art.find(".//Article")
    h.pmid = (med.findtext("PMID") if med is not None else "") or ""
    if a is None:
        return
    h.title = squash(html.unescape("".join(a.find("ArticleTitle").itertext()) if a.find("ArticleTitle") is not None else ""))
    j = a.find("Journal")
    if j is not None:
        h.journal = squash(j.findtext("Title") or "")
        h.journal_abbrev = squash(j.findtext("ISOAbbreviation") or "")
        ji = j.find("JournalIssue")
        if ji is not None:
            h.volume, h.issue = ji.findtext("Volume") or "", ji.findtext("Issue") or ""
            pd = ji.find("PubDate")
            if pd is not None:
                h.year = pd.findtext("Year") or (pd.findtext("MedlineDate") or "")[:4]
    h.pages = a.findtext("Pagination/MedlinePgn") or ""
    for au in a.findall("AuthorList/Author"):
        last = au.findtext("LastName")
        coll = au.findtext("CollectiveName")
        if last:
            h.authors.append(squash(last))
            given = au.findtext("ForeName") or au.findtext("Initials") or ""
            h.authors_full.append(squash(last) + (f", {squash(given)}" if given else ""))
        elif coll and not h.authors:
            h.authors.append(squash(coll))
            h.authors_full.append(squash(coll))
    h.first_author = h.authors[0] if h.authors else ""
    for e in a.findall("ELocationID"):
        if e.get("EIdType") == "doi" and e.text:
            h.doi = clean_doi(e.text)
    for i in art.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if i.get("IdType") == "doi" and i.text and not h.doi:
            h.doi = clean_doi(i.text)
        if i.get("IdType") == "pmc" and i.text:
            h.pmcid = i.text.strip()
    ptypes = [p.text or "" for p in a.findall("PublicationTypeList/PublicationType")]
    h.work_type = "; ".join(ptypes)
    if any("Retracted Publication" in p for p in ptypes):
        h.retracted = True
        h.retraction_notes.append("PubMed PublicationType: Retracted Publication")
    if any(p == "Preprint" for p in ptypes):
        h.preprint = True
    for c in (med.findall("CommentsCorrectionsList/CommentsCorrections") if med is not None else []):
        rt = c.get("RefType") or ""
        src = squash(c.findtext("RefSource") or "")
        if rt == "RetractionIn":
            h.retracted = True
            h.retraction_notes.append(f"PubMed RetractionIn: {src}")
        elif rt == "ExpressionOfConcernIn":
            h.concern = True
            h.retraction_notes.append(f"PubMed ExpressionOfConcernIn: {src}")
        elif rt == "ErratumIn":
            h.correction = True
        elif rt == "UpdateIn" and "preprint" in (h.work_type or "").lower():
            h.vor_doi = clean_doi(DOI_RE.search(src).group(0)) if DOI_RE.search(src) else h.vor_doi
    if re.match(r"^\s*(retracted|withdrawn)\b", h.title, flags=re.I):
        h.retracted = True
    if want_abstract:
        parts = []
        for p in a.findall("Abstract/AbstractText"):
            label = p.get("Label")
            txt = squash("".join(p.itertext()))
            parts.append(f"{label}: {txt}" if label else txt)
        h.abstract = "\n".join(parts)
    _mark_preprint(h)


def pubmed_search(http: Http, term: str, retmax: int = 5) -> tuple[list[str], str]:
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + ncbi_params(http, db="pubmed", term=term, retmode="json", retmax=str(retmax))
    status, data = http.get_json(url)
    if status != 200 or not data:
        return [], f"HTTP {status}"
    return list(data.get("esearchresult", {}).get("idlist", [])), ""


def pubmed_fetch_many(http: Http, pmids: list[str], want_abstract: bool = False) -> list[Hit]:
    if not pmids:
        return []
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + ncbi_params(http, db="pubmed", id=",".join(pmids), retmode="xml")
    status, body, _ = http.get(url)
    hits: list[Hit] = []
    if status != 200:
        return hits
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return hits
    for art in root.findall(".//PubmedArticle"):
        h = _hit("pubmed", "pmid:batch", url)
        _fill_from_pubmed_xml(h, art, want_abstract)
        h.query = f"pmid:{h.pmid}"
        h.url = redact_url("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + ncbi_params(http, db="pubmed", id=h.pmid, retmode="xml"))
        hits.append(h)
    return hits


def pubmed_title_search(http: Http, title: str) -> Hit:
    q = re.sub(r"[^\w\s\-]", " ", title)
    q = squash(q)
    term = f"{q}[Title]"
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + ncbi_params(http, db="pubmed", term=term, retmode="json", retmax="5")
    h = _hit("pubmed", f"title:{title[:80]}", url)
    ids, err = pubmed_search(http, term, 5)
    if err:
        h.ok, h.error = False, err
        return h
    if not ids:
        return h
    for ph in pubmed_fetch_many(http, ids):
        h.candidates.append({"source": "pubmed", "pmid": ph.pmid, "doi": ph.doi, "title": ph.title, "first_author": ph.first_author, "year": ph.year, "journal": ph.journal_abbrev or ph.journal, "retracted": ph.retracted, "preprint": ph.preprint, "pmcid": ph.pmcid})
    return h


def openalex_work(http: Http, ident: str) -> Hit:
    """ident is 'doi:<doi>' or 'pmid:<pmid>'."""
    kind, val = ident.split(":", 1)
    path = f"https://doi.org/{val}" if kind == "doi" else f"pmid:{val}"
    url = "https://api.openalex.org/works/" + urllib.parse.quote(path, safe=":/") + (f"?mailto={urllib.parse.quote(http.email)}" if http.email else "")
    h = _hit("openalex", ident, url)
    status, d = http.get_json(url)
    if status == 404:
        return h
    if status != 200 or not d:
        h.ok, h.error = False, f"HTTP {status}"
        return h
    _fill_from_openalex(h, d)
    return h


def _fill_from_openalex(h: Hit, d: dict) -> None:
    h.found = True
    h.title = squash(html.unescape(d.get("title") or d.get("display_name") or ""))
    for au in d.get("authorships") or []:
        raw = au.get("raw_author_name") or (au.get("author") or {}).get("display_name") or ""
        if raw:
            sn = surname_from_display(raw)
            h.authors.append(sn)
            given = squash(raw.replace(sn, "")).strip(" ,") if "," not in raw else raw.split(",", 1)[1].strip()
            h.authors_full.append(sn + (f", {given}" if given else ""))
    h.first_author = h.authors[0] if h.authors else ""
    h.year = str(d.get("publication_year") or "")
    src = ((d.get("primary_location") or {}).get("source") or {})
    h.journal = squash(src.get("display_name") or "")
    b = d.get("biblio") or {}
    h.volume, h.issue = str(b.get("volume") or ""), str(b.get("issue") or "")
    if b.get("first_page"):
        h.pages = str(b["first_page"]) + (f"-{b['last_page']}" if b.get("last_page") else "")
    ids = d.get("ids") or {}
    if ids.get("doi") or d.get("doi"):
        h.doi = clean_doi(ids.get("doi") or d.get("doi"))
    if ids.get("pmid"):
        h.pmid = re.sub(r"\D", "", ids["pmid"].rsplit("/", 1)[-1])
    if ids.get("pmcid"):
        h.pmcid = "PMC" + re.sub(r"\D", "", ids["pmcid"].rsplit("/", 1)[-1])
    h.work_type = d.get("type") or ""
    if d.get("is_retracted"):
        h.retracted = True
        h.retraction_notes.append("OpenAlex is_retracted=true")
    if h.work_type == "preprint" or (src.get("type") == "repository" and doi_prefix(h.doi) in PREPRINT_PREFIXES):
        h.preprint = True
    inv = d.get("abstract_inverted_index")
    if inv:
        words: list[tuple[int, str]] = []
        for w, poss in inv.items():
            for p in poss:
                words.append((p, w))
        words.sort()
        h.abstract = squash(" ".join(w for _, w in words))
    _mark_preprint(h)


def openalex_title_search(http: Http, title: str) -> Hit:
    q = squash(re.sub(r"[^\w\s]", " ", title))
    url = "https://api.openalex.org/works?filter=title.search:" + urllib.parse.quote(q) + "&per-page=5&select=id,doi,title,publication_year,ids,authorships,primary_location,type,is_retracted,biblio" + (f"&mailto={urllib.parse.quote(http.email)}" if http.email else "")
    h = _hit("openalex", f"title:{title[:80]}", url)
    status, d = http.get_json(url)
    if status != 200 or d is None:
        h.ok, h.error = False, f"HTTP {status}"
        return h
    for w in d.get("results") or []:
        c = Hit(source="openalex", query="")
        _fill_from_openalex(c, w)
        h.candidates.append({"source": "openalex", "pmid": c.pmid, "doi": c.doi, "title": c.title, "first_author": c.first_author, "year": c.year, "journal": c.journal, "retracted": c.retracted, "preprint": c.preprint, "pmcid": c.pmcid})
    return h


def europepmc_query(http: Http, query: str, label: str, core: bool = False, page_size: int = 5) -> Hit:
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urllib.parse.urlencode({"query": query, "format": "json", "resultType": "core" if core else "lite", "pageSize": str(page_size)})
    h = _hit("europepmc", label, url)
    status, d = http.get_json(url)
    if status != 200 or d is None:
        h.ok, h.error = False, f"HTTP {status}"
        return h
    results = (d.get("resultList") or {}).get("result") or []
    for r in results:
        c = _europepmc_record(r)
        h.candidates.append({"source": "europepmc", "pmid": c.pmid, "doi": c.doi, "title": c.title, "first_author": c.first_author, "year": c.year, "journal": c.journal, "retracted": c.retracted, "preprint": c.preprint, "pmcid": c.pmcid, "abstract": c.abstract})
    return h


def _europepmc_record(r: dict) -> Hit:
    c = Hit(source="europepmc", query="")
    c.found = True
    c.title = squash(html.unescape(r.get("title") or ""))
    auth = r.get("authorString") or ""
    for part in auth.split(","):
        part = part.strip(" .")
        if part:
            sn = surname_from_display(part)
            c.authors.append(sn)
            initials = part[len(sn):].strip()
            c.authors_full.append(sn + (f", {initials}" if initials else ""))
    c.first_author = c.authors[0] if c.authors else ""
    c.year = str(r.get("pubYear") or "")
    c.journal = squash(r.get("journalTitle") or ((r.get("journalInfo") or {}).get("journal") or {}).get("title") or "")
    ji = r.get("journalInfo") or {}
    c.volume, c.issue = str(ji.get("volume") or r.get("journalVolume") or ""), str(ji.get("issue") or r.get("issue") or "")
    c.pages = str(r.get("pageInfo") or "")
    c.doi = clean_doi(r.get("doi") or "") if r.get("doi") else ""
    c.pmid = str(r.get("pmid") or "")
    c.pmcid = str(r.get("pmcid") or "")
    pt = (r.get("pubTypeList") or {}).get("pubType") or []
    c.work_type = "; ".join(pt) if isinstance(pt, list) else str(pt)
    if any("Retracted Publication" in p for p in pt):
        c.retracted = True
        c.retraction_notes.append("Europe PMC pubType: Retracted Publication")
    for cc in ((r.get("commentCorrectionList") or {}).get("commentCorrection") or []):
        t = (cc.get("type") or "")
        if t == "Retraction in":
            c.retracted = True
            c.retraction_notes.append(f"Europe PMC Retraction in: {cc.get('reference')}")
        elif t == "Expression of concern in":
            c.concern = True
        elif t == "Erratum in":
            c.correction = True
    if r.get("source") == "PPR" or any(p.lower() == "preprint" for p in pt):
        c.preprint = True
    c.abstract = squash(r.get("abstractText") or "")
    _mark_preprint(c)
    return c


def europepmc_by_id(http: Http, kind: str, val: str, core: bool = False) -> Hit:
    q = f'DOI:"{val}"' if kind == "doi" else f"EXT_ID:{val} AND SRC:MED"
    h = europepmc_query(http, q, f"{kind}:{val}", core=core, page_size=3)
    if not h.ok:
        return h
    # pick the candidate whose identifier matches exactly
    match = None
    for c in h.candidates:
        if kind == "doi" and clean_doi(c.get("doi") or "") == val.lower():
            match = c
            break
        if kind == "pmid" and str(c.get("pmid")) == val:
            match = c
            break
    if match is None:
        h.found = False
        return h
    h.found = True
    for k in ("title", "first_author", "year", "journal", "doi", "pmid", "pmcid", "retracted", "preprint", "abstract"):
        setattr(h, k, match.get(k) if match.get(k) is not None else getattr(h, k))
    h.authors = [h.first_author] if h.first_author else []
    if h.retracted:
        h.retraction_notes.append("Europe PMC flags retraction")
    _mark_preprint(h)
    return h


def semanticscholar_by_doi(http: Http, doi: str) -> Hit:
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{urllib.parse.quote(doi, safe='/()')}?fields=title,year,authors,externalIds,venue,publicationTypes"
    h = _hit("semanticscholar", f"doi:{doi}", url)
    status, d = http.get_json(url)
    if status == 404:
        return h
    if status != 200 or not d:
        h.ok, h.error = False, f"HTTP {status}"
        return h
    h.found = True
    h.title = squash(d.get("title") or "")
    h.authors = [surname_from_display(a.get("name") or "") for a in d.get("authors") or []]
    h.first_author = h.authors[0] if h.authors else ""
    h.year = str(d.get("year") or "")
    h.journal = squash(d.get("venue") or "")
    ext = d.get("externalIds") or {}
    h.doi = clean_doi(ext.get("DOI") or doi)
    h.pmid = str(ext.get("PubMed") or "")
    return h


def crossref_bibliographic_search(http: Http, ref: Ref) -> Hit:
    q = " ".join(x for x in [ref.title, ref.first_author, ref.year] if x)
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode({"query.bibliographic": q, "rows": "5", "select": "DOI,title,author,issued,container-title,type,score"})
    h = _hit("crossref", f"title:{ref.title[:80]}", url)
    status, d = http.get_json(url)
    if status != 200 or not d or d.get("status") != "ok":
        h.ok, h.error = False, f"HTTP {status}"
        return h
    for it in (d.get("message") or {}).get("items") or []:
        fam = ((it.get("author") or [{}])[0]).get("family") or ""
        parts = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
        h.candidates.append({
            "source": "crossref", "doi": clean_doi(it.get("DOI") or ""), "pmid": "",
            "title": squash(strip_tags(html.unescape(" ".join(it.get("title") or [])))),
            "first_author": fam, "year": str(parts[0]) if parts and parts[0] else "",
            "journal": squash(" ".join(it.get("container-title") or [])), "retracted": False,
            "preprint": it.get("type") == "posted-content", "pmcid": "",
        })
    return h


# --------------------------------------------------------------------------------------
# Verification logic
# --------------------------------------------------------------------------------------

class Verifier:
    def __init__(self, http: Http, sources: tuple[str, ...] = DEFAULT_SOURCES, resolve_doi_org: bool = True, quiet: bool = False):
        self.http = http
        self.sources = set(sources)
        self.resolve_doi_org = resolve_doi_org
        self.quiet = quiet

    # ---- lookups -------------------------------------------------------------------

    def lookup_doi(self, doi: str) -> list[Hit]:
        hits: list[Hit] = []
        if "crossref" in self.sources:
            hits.append(crossref_by_doi(self.http, doi))
        if self.resolve_doi_org:
            hits.append(doiorg_resolves(self.http, doi))
        if "openalex" in self.sources:
            hits.append(openalex_work(self.http, f"doi:{doi}"))
        if "europepmc" in self.sources:
            hits.append(europepmc_by_id(self.http, "doi", doi))
        if "semanticscholar" in self.sources:
            hits.append(semanticscholar_by_doi(self.http, doi))
        return hits

    def lookup_pmid(self, pmid: str) -> list[Hit]:
        hits: list[Hit] = []
        if "pubmed" in self.sources:
            hits.append(pubmed_by_pmid(self.http, pmid))
        if "europepmc" in self.sources:
            hits.append(europepmc_by_id(self.http, "pmid", pmid))
        if "openalex" in self.sources:
            hits.append(openalex_work(self.http, f"pmid:{pmid}"))
        return hits

    def lookup_title(self, ref: Ref) -> list[Hit]:
        hits: list[Hit] = []
        if "pubmed" in self.sources:
            hits.append(pubmed_title_search(self.http, ref.title))
        if "openalex" in self.sources:
            hits.append(openalex_title_search(self.http, ref.title))
        if "europepmc" in self.sources:
            q = 'TITLE:"' + ref.title.replace('"', "") + '"'
            hits.append(europepmc_query(self.http, q, f"title:{ref.title[:80]}"))
        if "crossref" in self.sources:
            hits.append(crossref_bibliographic_search(self.http, ref))
        return hits

    # ---- field checks ---------------------------------------------------------------

    @staticmethod
    def field_checks(ref: Ref, found: list[Hit]) -> tuple[list[Check], list[str], list[str]]:
        """Compare cited fields with every found record; a field only fails if it disagrees with all."""
        checks: list[Check] = []
        major: list[str] = []
        warnings: list[str] = []
        meta = [h for h in found if h.title or h.first_author or h.year]
        if not meta:
            return checks, major, warnings

        # Title
        if ref.title:
            best, best_h = 0.0, meta[0]
            for h in meta:
                for cand in filter(None, [h.title, f"{h.title}: {h.subtitle}" if h.subtitle else ""]):
                    s = title_similarity(ref.title, cand)
                    if s > best:
                        best, best_h = s, h
            if best >= TITLE_MATCH:
                checks.append(Check("title", ref.title, best_h.title, best_h.source, "match", f"similarity {best}"))
            elif best >= TITLE_WARN:
                checks.append(Check("title", ref.title, best_h.title, best_h.source, "warning", f"similarity {best} (minor wording difference)"))
                warnings.append(f"title differs slightly from {best_h.source} (similarity {best})")
            else:
                checks.append(Check("title", ref.title, best_h.title, best_h.source, "mismatch", f"similarity {best}"))
                major.append(f"title does not match the record behind these identifiers (similarity {best}; source title: \"{best_h.title[:120]}\")")
        else:
            checks.append(Check("title", "", meta[0].title, meta[0].source, "skipped", "no title parsed from citation"))

        # First author
        if ref.first_author:
            actual = [h for h in meta if h.first_author]
            if actual:
                ok = any(surnames_match(ref.first_author, h.first_author) for h in actual)
                # tolerate ordering slips: cited first author appears anywhere in the author list
                anywhere = any(any(surnames_match(ref.first_author, a) for a in h.authors) for h in actual)
                if ok:
                    checks.append(Check("first_author", ref.first_author, actual[0].first_author, actual[0].source, "match"))
                elif anywhere:
                    checks.append(Check("first_author", ref.first_author, actual[0].first_author, actual[0].source, "warning", "cited name is an author but not the first"))
                    warnings.append(f"cited first author '{ref.first_author}' is in the author list but the first author is '{actual[0].first_author}'")
                else:
                    checks.append(Check("first_author", ref.first_author, actual[0].first_author, actual[0].source, "mismatch"))
                    major.append(f"first author mismatch: cited '{ref.first_author}', source says '{actual[0].first_author}'")
        else:
            checks.append(Check("first_author", "", meta[0].first_author, meta[0].source, "skipped", "no author parsed"))

        # Year
        cy = year_int(ref.year)
        if cy:
            years = [(year_int(h.year), h) for h in meta if year_int(h.year)]
            if years:
                diffs = [(abs(cy - y), y, h) for y, h in years]
                d, y, h = min(diffs, key=lambda t: t[0])
                if d == 0:
                    checks.append(Check("year", ref.year, str(y), h.source, "match"))
                elif d <= YEAR_WARN:
                    checks.append(Check("year", ref.year, str(y), h.source, "warning", "off by one (online-first vs print?)"))
                    warnings.append(f"year off by one: cited {cy}, source {y}")
                else:
                    checks.append(Check("year", ref.year, str(y), h.source, "mismatch"))
                    major.append(f"year mismatch: cited {cy}, source {y}")

        # Journal
        if ref.journal:
            cands = []
            for h in meta:
                cands += [h.journal, h.journal_abbrev]
            cands = [c for c in cands if c]
            if cands:
                if journals_match(ref.journal, cands):
                    checks.append(Check("journal", ref.journal, cands[0], meta[0].source, "match"))
                else:
                    checks.append(Check("journal", ref.journal, cands[0], meta[0].source, "warning", "journal name differs"))
                    warnings.append(f"journal differs: cited '{ref.journal}', source '{cands[0]}'")

        # Volume / pages (informational)
        if ref.volume:
            vols = [h.volume for h in meta if h.volume]
            if vols and ref.volume.strip() not in [v.strip() for v in vols]:
                checks.append(Check("volume", ref.volume, vols[0], meta[0].source, "warning"))
                warnings.append(f"volume differs: cited {ref.volume}, source {vols[0]}")
        if ref.pages:
            cp = re.sub(r"[^\w]", "", ref.pages.split("-")[0].split("–")[0])
            pages = [h.pages for h in meta if h.pages]
            if pages and cp and not any(re.sub(r"[^\w]", "", p.split("-")[0]).lower() == cp.lower() for p in pages):
                checks.append(Check("pages", ref.pages, pages[0], meta[0].source, "warning"))
                warnings.append(f"first page differs: cited {ref.pages}, source {pages[0]}")
        return checks, major, warnings

    # ---- main entry ------------------------------------------------------------------

    def verify(self, ref: Ref) -> Result:
        res = Result(ref_id=ref.ref_id, cited={k: v for k, v in asdict(ref).items() if k not in ("raw",) and v})
        res.cited["raw"] = ref.raw
        all_hits: list[Hit] = []
        found: list[Hit] = []
        notes = res.notes
        issues = res.issues

        doi_hits: list[Hit] = []
        pmid_hits: list[Hit] = []
        if ref.doi:
            doi_hits = self.lookup_doi(ref.doi)
            all_hits += doi_hits
        if ref.pmid:
            pmid_hits = self.lookup_pmid(ref.pmid)
            all_hits += pmid_hits

        doi_found = [h for h in doi_hits if h.found]
        doi_ok = [h for h in doi_hits if h.ok]
        pmid_found = [h for h in pmid_hits if h.found]
        pmid_ok = [h for h in pmid_hits if h.ok]

        doi_missing = bool(ref.doi) and not doi_found and len(doi_ok) >= 1
        pmid_missing = bool(ref.pmid) and not pmid_found and len(pmid_ok) >= 1
        doi_unknown = bool(ref.doi) and not doi_found and not doi_ok
        pmid_unknown = bool(ref.pmid) and not pmid_found and not pmid_ok

        # Enrich: a DOI-only citation gets its PubMed record via the PMID that OpenAlex/Europe PMC report
        if doi_found and not ref.pmid and "pubmed" in self.sources:
            pm = next((h.pmid for h in doi_found if h.pmid), "")
            if pm:
                ph = pubmed_by_pmid(self.http, pm)
                all_hits.append(ph)
                if ph.found and ph.doi and ph.doi == ref.doi:
                    doi_found.append(ph)
                elif ph.found and not ph.doi:
                    doi_found.append(ph)

        found = doi_found + pmid_found
        if doi_found:
            res.found_by = "doi"
        elif pmid_found:
            res.found_by = "pmid"

        # DOI and PMID must point at the same work
        if doi_found and pmid_found:
            same = False
            for dh in doi_found:
                for ph in pmid_found:
                    if (dh.pmid and ph.pmid and dh.pmid == ph.pmid) or (dh.doi and ph.doi and dh.doi == ph.doi):
                        same = True
                    elif dh.title and ph.title and title_similarity(dh.title, ph.title) >= TITLE_MATCH:
                        same = True
            if not same:
                dt = next((h.title for h in doi_found if h.title), "")
                pt = next((h.title for h in pmid_found if h.title), "")
                issues.append(f"DOI and PMID point to different works: DOI -> \"{dt[:90]}\"; PMID -> \"{pt[:90]}\"")
                res.flags.append("IDENTIFIER_CONFLICT")

        # Title-only path
        title_hits: list[Hit] = []
        if not found and not ref.doi and not ref.pmid:
            if ref.title and len(ref.title.split()) >= 3:
                title_hits = self.lookup_title(ref)
                all_hits += title_hits
                accepted = self._accept_title_candidate(ref, title_hits, res)
                if accepted:
                    found = [accepted]
                    res.found_by = "title"
                    res.flags.append("TITLE_MATCHED")
                    notes.append("no DOI/PMID in the citation; matched by title search, then confirmed by author/year")
            else:
                notes.append("no DOI, PMID or usable title could be parsed from this citation")
        elif not found and (ref.doi or ref.pmid) and ref.title and len(ref.title.split()) >= 3 and (doi_missing or pmid_missing):
            # identifiers are dead; see whether the title exists elsewhere so the user gets the right fix
            title_hits = self.lookup_title(ref)
            all_hits += title_hits
            cand = self._best_title_candidate(ref, title_hits)
            if cand and cand[0] >= TITLE_MATCH:
                c = cand[1]
                notes.append(f"a work with this title does exist ({c.get('source')}: DOI {c.get('doi') or '-'}, PMID {c.get('pmid') or '-'}); the cited identifier is wrong")
                res.flags.append("IDENTIFIER_WRONG_BUT_TITLE_EXISTS")

        # Field checks
        checks, major, warnings = self.field_checks(ref, found) if found else ([], [], [])
        res.checks = checks

        # Flags from records
        retracted = any(h.retracted for h in found)
        concern = any(h.concern for h in found)
        correction = any(h.correction for h in found)
        preprint = any(h.preprint for h in found)
        vor = next((h.vor_doi for h in found if h.vor_doi), "")
        if retracted:
            res.flags.append("RETRACTED")
            for h in found:
                for n in h.retraction_notes:
                    if n not in notes:
                        notes.append(n)
        if concern:
            res.flags.append("EXPRESSION_OF_CONCERN")
        if correction:
            res.flags.append("CORRECTION_PUBLISHED")
        if preprint:
            res.flags.append("PREPRINT")
            server = next((h.preprint_server for h in found if h.preprint_server), "")
            notes.append(f"this is a preprint{(' (' + server + ')') if server else ''}; it has not been peer reviewed" + (f"; a published version exists: DOI {vor}" if vor else ""))
            if vor:
                res.flags.append("PUBLISHED_VERSION_EXISTS")

        # Consensus record
        res.resolved = self._consensus(found)
        res.sources_confirming = sorted({h.source for h in found if h.source != "doi.org"})
        res.hits = [asdict(h) for h in all_hits]
        for h in res.hits:
            h.pop("abstract", None)
            h["candidates"] = h.get("candidates", [])[:5]
            for c in h["candidates"]:
                c.pop("abstract", None)

        # Status
        if doi_missing:
            issues.append(f"DOI {ref.doi} does not exist in CrossRef/doi.org/OpenAlex")
        if pmid_missing:
            issues.append(f"PMID {ref.pmid} does not exist in PubMed")
        if doi_unknown:
            issues.append(f"DOI {ref.doi} could not be checked (all sources failed)")
        if pmid_unknown:
            issues.append(f"PMID {ref.pmid} could not be checked (all sources failed)")
        issues.extend(major)

        if not found:
            if (ref.doi or ref.pmid) and (doi_missing or pmid_missing) and not (doi_unknown or pmid_unknown):
                res.status = "NOT_FOUND"
            elif title_hits:
                responded = [h for h in title_hits if h.ok]
                if len(responded) >= 2:
                    res.status = "NOT_FOUND"
                    if not any(h.candidates for h in responded):
                        issues.append("no work with this title in PubMed, OpenAlex, Europe PMC or CrossRef")
                else:
                    res.status = "UNVERIFIABLE"
                    issues.append("title search failed in most sources (network?)")
            else:
                res.status = "UNVERIFIABLE"
        elif retracted:
            res.status = "RETRACTED"
            issues.insert(0, "this work has been RETRACTED; do not cite it as evidence")
        elif major or "IDENTIFIER_CONFLICT" in res.flags or doi_missing or pmid_missing:
            res.status = "MISMATCH"
        elif warnings:
            res.status = "VERIFIED_WITH_WARNINGS"
            issues.extend(warnings)
        else:
            res.status = "VERIFIED"
            if warnings:
                issues.extend(warnings)

        # Confidence
        n_src = len(res.sources_confirming)
        if res.status in ("VERIFIED", "VERIFIED_WITH_WARNINGS", "RETRACTED"):
            if res.found_by == "title":
                res.confidence = "medium" if n_src >= 1 else "low"
            else:
                res.confidence = "high" if n_src >= 2 else "medium"
        else:
            res.confidence = "high" if (doi_ok or pmid_ok or len([h for h in title_hits if h.ok]) >= 2) else "low"
        if found and ref.parse_confidence == "low" and res.found_by != "title":
            notes.append("few cited fields could be parsed, so only the identifier was checked")
        return res

    # ---- helpers -----------------------------------------------------------------------

    @staticmethod
    def _best_title_candidate(ref: Ref, title_hits: list[Hit]) -> Optional[tuple[float, dict]]:
        best: Optional[tuple[float, dict]] = None
        for h in title_hits:
            for c in h.candidates:
                s = title_similarity(ref.title, c.get("title") or "")
                if best is None or s > best[0]:
                    best = (s, c)
        return best

    def _accept_title_candidate(self, ref: Ref, title_hits: list[Hit], res: Result) -> Optional[Hit]:
        # Gather candidates from every source, dedupe by DOI/PMID, score each
        seen: dict[str, dict] = {}
        for h in title_hits:
            for c in h.candidates:
                key = (c.get("doi") or "").lower() or (c.get("pmid") or "") or norm_title(c.get("title") or "")
                if not key:
                    continue
                if key not in seen:
                    seen[key] = dict(c)
                else:
                    # merge identifiers across sources
                    for k in ("doi", "pmid", "pmcid"):
                        if not seen[key].get(k) and c.get(k):
                            seen[key][k] = c[k]
        scored: list[tuple[float, bool, bool, bool, dict]] = []
        for c in seen.values():
            s = title_similarity(ref.title, c.get("title") or "")
            author_ok = (not ref.first_author) or (not c.get("first_author")) or surnames_match(ref.first_author, c["first_author"])
            cy, sy = year_int(ref.year), year_int(c.get("year", ""))
            year_ok = (cy is None or sy is None) or abs(cy - sy) <= YEAR_WARN
            journal_ok = bool(ref.journal) and bool(c.get("journal")) and journals_match(ref.journal, [c["journal"]])
            scored.append((s, author_ok, year_ok, journal_ok, c))
        # best title similarity first; among ties prefer author+year agreement, then journal agreement
        scored.sort(key=lambda t: (-t[0], not (t[1] and t[2]), not t[3]))
        if scored:
            res.notes.append("title candidates: " + "; ".join(f"{c.get('source')}:{s}:{(c.get('title') or '')[:60]}" for s, _, _, _, c in scored[:3]))
        strong = [t for t in scored if t[0] >= TITLE_MATCH]
        accepted = [t for t in strong if t[1] and t[2]]
        if accepted:
            # several distinct works can share a title (co-publications, letters with the paper's title):
            # use the cited journal to disambiguate, and say so when it cannot be decided
            if len(accepted) > 1:
                with_journal = [t for t in accepted if t[3]]
                if len(with_journal) == 1:
                    accepted = with_journal
                else:
                    res.flags.append("MULTIPLE_MATCHES")
                    res.notes.append(f"{len(accepted)} distinct works match this title and author/year; add the DOI or PMID to the citation to make it unambiguous. Matched: "
                                     + "; ".join(f"{(t[4].get('journal') or '?')} {t[4].get('year')} DOI {t[4].get('doi') or '-'}" for t in accepted[:4]))
            c = accepted[0][4]
            # Promote the candidate to a full record via its identifiers for a proper cross-check
            if c.get("pmid") and "pubmed" in self.sources:
                h = pubmed_by_pmid(self.http, c["pmid"])
                if h.found:
                    return h
            if c.get("doi") and "crossref" in self.sources:
                h = crossref_by_doi(self.http, c["doi"])
                if h.found:
                    return h
            return Hit(source=c.get("source", "title-search"), query=f"title:{ref.title[:80]}", found=True, title=c.get("title", ""), first_author=c.get("first_author", ""), authors=[c.get("first_author", "")], year=c.get("year", ""), journal=c.get("journal", ""), doi=c.get("doi", ""), pmid=c.get("pmid", ""), pmcid=c.get("pmcid", ""), retracted=bool(c.get("retracted")), preprint=bool(c.get("preprint")), retrieved=utcnow())
        if strong:
            s, _, _, _, c = strong[0]
            res.flags.append("TITLE_EXISTS_AUTHOR_YEAR_DIFFER")
            res.issues.append(f"a paper with this title exists but the cited author/year do not match it (source: {c.get('first_author')} {c.get('year')}, {c.get('journal') or ''}, DOI {c.get('doi') or '-'})")
        return None

    @staticmethod
    def _consensus(found: list[Hit]) -> dict:
        if not found:
            return {}
        order = {"pubmed": 0, "crossref": 1, "openalex": 2, "europepmc": 3, "semanticscholar": 4, "doi.org": 9}
        fs = sorted(found, key=lambda h: order.get(h.source, 5))
        def first(attr: str) -> Any:
            for h in fs:
                v = getattr(h, attr)
                if v:
                    return v
            return ""
        return {
            "title": str(first("title")).rstrip(". "), "first_author": first("first_author"), "authors": first("authors"),
            "authors_full": first("authors_full"),
            "year": first("year"), "journal": first("journal"), "journal_abbrev": first("journal_abbrev"),
            "volume": first("volume"), "issue": first("issue"), "pages": first("pages"),
            "doi": first("doi"), "pmid": first("pmid"), "pmcid": first("pmcid"), "type": first("work_type"),
            "retracted": any(h.retracted for h in fs), "preprint": any(h.preprint for h in fs),
            "sources": sorted({h.source for h in fs if h.source != "doi.org"}),
        }


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

BLOCKING = {"NOT_FOUND", "MISMATCH", "RETRACTED"}
REVIEW = {"UNVERIFIABLE", "VERIFIED_WITH_WARNINGS"}


def _vancouver_name(full: str) -> str:
    """'Nidorf, Stefan M' -> 'Nidorf SM'; 'Nidorf, SM' -> 'Nidorf SM'; 'LoDoCo2 Trial Investigators' unchanged."""
    if "," not in full:
        return full
    fam, given = [p.strip() for p in full.split(",", 1)]
    if re.fullmatch(r"[A-Z]{1,4}", given):
        return f"{fam} {given}"
    initials = "".join(tok[0] for tok in re.split(r"[\s\-.]+", given) if tok and tok[0].isalpha())
    return f"{fam} {initials.upper()}" if initials else fam


def vancouver(rec: dict) -> str:
    authors = rec.get("authors_full") or rec.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    names = [_vancouver_name(a) for a in authors]
    au = ", ".join(names[:6]) + (", et al" if len(names) > 6 else "")
    parts = [f"{au}." if au else "", f"{rec.get('title','').rstrip('.')}." if rec.get("title") else "",
             f"{rec.get('journal_abbrev') or rec.get('journal','')}." if (rec.get('journal_abbrev') or rec.get('journal')) else "",
             f"{rec.get('year','')}" + (f";{rec.get('volume')}" if rec.get("volume") else "") + (f"({rec.get('issue')})" if rec.get("issue") else "") + (f":{rec.get('pages')}" if rec.get("pages") else "") + "."]
    s = " ".join(p for p in parts if p)
    if rec.get("doi"):
        s += f" doi:{rec['doi']}"
    if rec.get("pmid"):
        s += f" PMID: {rec['pmid']}"
    return squash(s)


def build_audit(results: list[Result], source: str, opts: dict) -> dict:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    flags: dict[str, int] = {}
    for r in results:
        for f in r.flags:
            flags[f] = flags.get(f, 0) + 1
    blocking = [r.ref_id for r in results if r.status in BLOCKING]
    review = [r.ref_id for r in results if r.status in REVIEW or "PREPRINT" in r.flags]
    return {
        "tool": TOOL_NAME, "version": __version__, "generated": utcnow(), "source": source,
        "sources_queried": opts.get("sources", list(DEFAULT_SOURCES)),
        "total_references": len(results), "counts": counts, "flags": flags,
        "blocking_ref_ids": blocking, "needs_review_ref_ids": review,
        "submission_safe": not blocking,
        "fully_verified": all(r.status == "VERIFIED" for r in results) and bool(results),
        "method_statement": (
            f"All {len(results)} references were checked on {today()} with citecheck v{__version__} against "
            + ", ".join(opts.get("sources", list(DEFAULT_SOURCES))) + " (identifier resolution, then cited title, first author, year and journal compared with the source record; retraction and preprint status flagged)."
        ),
        "records": [
            {
                "ref_id": r.ref_id, "status": r.status, "confidence": r.confidence, "found_by": r.found_by,
                "flags": r.flags, "sources_confirming": r.sources_confirming, "issues": r.issues, "notes": r.notes,
                "cited": r.cited, "resolved": r.resolved, "checks": [asdict(c) for c in r.checks], "lookups": r.hits,
            }
            for r in results
        ],
    }


def audit_markdown(audit: dict) -> str:
    recs = audit["records"]
    lines = [f"# Reference audit - {Path(audit['source']).name}", "",
             f"Generated {audit['generated']} by citecheck v{audit['version']}. Sources: {', '.join(audit['sources_queried'])}.", "",
             "| Status | Count |", "|---|---|"]
    for k in ("VERIFIED", "VERIFIED_WITH_WARNINGS", "MISMATCH", "NOT_FOUND", "RETRACTED", "UNVERIFIABLE"):
        if audit["counts"].get(k):
            lines.append(f"| {k} | {audit['counts'][k]} |")
    if audit["flags"]:
        lines += ["", "Flags: " + ", ".join(f"{k} x{v}" for k, v in sorted(audit["flags"].items()))]
    lines += ["", f"**Submission safe (no NOT_FOUND / MISMATCH / RETRACTED):** {'yes' if audit['submission_safe'] else 'NO'}", ""]
    lines += ["| Ref | Status | Conf. | Sources | Flags | Cited (first author, year, title) | Issues |", "|---|---|---|---|---|---|---|"]
    for r in recs:
        c = r["cited"]
        cited = f"{c.get('first_author','?')} {c.get('year','')} - {(c.get('title') or c.get('raw') or '')[:70]}"
        lines.append(f"| {r['ref_id']} | {r['status']} | {r['confidence']} | {', '.join(r['sources_confirming']) or '-'} | {', '.join(r['flags']) or '-'} | {cited.replace('|','/')} | {'; '.join(r['issues'])[:200].replace('|','/') or '-'} |")
    problems = [r for r in recs if r["status"] != "VERIFIED" or r["flags"]]
    if problems:
        lines += ["", "## Details for references needing attention", ""]
        for r in problems:
            lines += [f"### {r['ref_id']} - {r['status']}" + (f" [{', '.join(r['flags'])}]" if r["flags"] else ""), "",
                      f"- Cited: `{(r['cited'].get('raw') or '')[:300]}`"]
            if r["resolved"]:
                lines.append(f"- Source record: {vancouver(r['resolved'])}")
            for i in r["issues"]:
                lines.append(f"- Issue: {i}")
            for n in r["notes"]:
                lines.append(f"- Note: {n}")
            for ck in r["checks"]:
                if ck["outcome"] in ("warning", "mismatch"):
                    lines.append(f"- {ck['field']}: cited \"{ck['cited']}\" vs {ck['source']} \"{ck['source_value']}\" -> {ck['outcome']} {ck['detail']}")
            urls = [l["url"] for l in r["lookups"] if l.get("found") and l.get("url")]
            if urls:
                lines.append("- Evidence: " + " ; ".join(urls[:4]))
            lines.append("")
    lines += ["", "## Method statement (paste into your methods / cover letter if useful)", "", audit["method_statement"], ""]
    lines += ["## What the statuses mean", "",
              "- VERIFIED: identifiers resolve and the cited title, first author, year and journal agree with the source record.",
              "- VERIFIED_WITH_WARNINGS: the work exists, but a minor field differs (year off by one, journal spelling, page numbers). Fix the field.",
              "- MISMATCH: the work exists, but a major field differs (title, first author, year by more than one), or the DOI and PMID point to different papers. Treat as a wrong citation until fixed.",
              "- NOT_FOUND: the DOI/PMID does not exist, or no database has a paper with this title. Treat as fabricated until you can produce the source.",
              "- RETRACTED: the paper exists but has been retracted. Do not cite it as evidence.",
              "- UNVERIFIABLE: the tool could not check it (network, or nothing parseable). Check by hand.",
              "", "This tool checks that a reference exists and is cited correctly. It does not check that the paper says what your sentence claims; use `citecheck evidence` and the check-claims skill for that."]
    return "\n".join(lines)


def print_summary(audit: dict, quiet: bool = False) -> None:
    if quiet:
        return
    print(f"citecheck v{audit['version']} - {audit['total_references']} references from {Path(audit['source']).name}")
    print("-" * 100)
    for r in audit["records"]:
        c = r["cited"]
        cited = f"{(c.get('first_author') or '?')[:18]} {c.get('year','')} {(c.get('title') or c.get('raw') or '')[:48]}"
        flags = ("[" + ",".join(r["flags"]) + "]") if r["flags"] else ""
        issue = (r["issues"][0][:60] if r["issues"] else "")
        print(f"{r['ref_id'][:14]:<14} {r['status']:<22} {cited:<75} {flags} {issue}")
    print("-" * 100)
    print("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(audit["counts"].items())) + (" | flags: " + ", ".join(f"{k}={v}" for k, v in sorted(audit["flags"].items())) if audit["flags"] else ""))
    print(f"submission_safe={audit['submission_safe']}  blocking={audit['blocking_ref_ids']}  needs_review={audit['needs_review_ref_ids']}")


# --------------------------------------------------------------------------------------
# Evidence packs (for claim checking)
# --------------------------------------------------------------------------------------

def jats_to_text(xml: str) -> str:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return squash(strip_tags(xml))
    out: list[str] = []
    skip = {"ref-list", "xref", "table-wrap-foot", "back"}

    def walk(el: ET.Element, depth: int) -> None:
        tag = el.tag.split("}")[-1]
        if tag in skip:
            return
        if tag == "title":
            out.append("\n" + "#" * min(2 + depth, 5) + " " + squash("".join(el.itertext())) + "\n")
            return
        if tag in ("p", "abstract", "caption", "label", "td", "th", "list-item", "fig", "table-wrap"):
            txt = squash("".join(el.itertext()))
            if tag == "p" and txt:
                out.append(txt + "\n")
                return
        for ch in list(el):
            walk(ch, depth + 1 if tag in ("sec",) else depth)
        if tag in ("td", "th") and el.text:
            out.append(squash(el.text) + " | ")
    walk(root, 0)
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_evidence_for(http: Http, rec: dict, want_fulltext: bool = True) -> dict:
    """rec: a resolved record with doi/pmid/pmcid. Returns {tier, abstract, fulltext, sources}."""
    ev: dict[str, Any] = {"tier": "none", "abstract": "", "fulltext": "", "sources": []}
    pmid, doi, pmcid = rec.get("pmid") or "", rec.get("doi") or "", rec.get("pmcid") or ""
    if pmid:
        h = pubmed_by_pmid(http, pmid, want_abstract=True)
        if h.found and h.abstract:
            ev["abstract"] = h.abstract
            ev["sources"].append(h.url)
            pmcid = pmcid or h.pmcid
    if not ev["abstract"] and doi:
        h = europepmc_by_id(http, "doi", doi, core=True)
        if h.found and h.abstract:
            ev["abstract"] = h.abstract
            ev["sources"].append(h.url)
            pmcid = pmcid or h.pmcid
    if not ev["abstract"] and doi:
        h = openalex_work(http, f"doi:{doi}")
        if h.found and h.abstract:
            ev["abstract"] = h.abstract
            ev["sources"].append(h.url)
            pmcid = pmcid or h.pmcid
    if not ev["abstract"] and doi:
        h = crossref_by_doi(http, doi)
        if h.found and h.abstract:
            ev["abstract"] = h.abstract
            ev["sources"].append(h.url)
    if ev["abstract"]:
        ev["tier"] = "abstract"
    if want_fulltext and pmcid:
        url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
        status, body, _ = http.get(url)
        if status == 200 and body.strip():
            txt = jats_to_text(body)
            if len(txt) > 500:
                ev["fulltext"] = txt
                ev["tier"] = "full_text"
                ev["sources"].append(url)
    ev["pmcid"] = pmcid
    return ev


def write_evidence_pack(out_dir: Path, ref_id: str, rec: dict, ev: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-.]+", "_", ref_id)[:80]
    p = out_dir / f"{safe}.md"
    lines = [f"# Evidence pack - {ref_id}", "",
             f"- Source record: {vancouver(rec)}",
             f"- Evidence tier available: **{ev['tier']}**" + (" (full text via Europe PMC open-access XML)" if ev["tier"] == "full_text" else " (abstract only: claims about subgroups, secondary endpoints or methods detail cannot be confirmed from this)" if ev["tier"] == "abstract" else " (nothing retrievable: mark claims UNVERIFIABLE)"),
             f"- Retrieved {utcnow()} from: " + ("; ".join(ev["sources"]) or "-"), ""]
    if rec.get("retracted"):
        lines += ["> **RETRACTED** - this paper has been retracted. Do not use it as evidence.", ""]
    if rec.get("preprint"):
        lines += ["> **PREPRINT** - not peer reviewed.", ""]
    if ev["abstract"]:
        lines += ["## Abstract", "", ev["abstract"], ""]
    if ev["fulltext"]:
        lines += ["## Full text (open access)", "", ev["fulltext"], ""]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# --------------------------------------------------------------------------------------
# Claim extraction
# --------------------------------------------------------------------------------------

CITE_NUMERIC_RE = re.compile(r"\[(\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*)\]|(?<![\w.])\^(\d{1,3}(?:[,\-–]\d{1,3})*)\^|(?<=[a-z\)\.])(\d{1,3}(?:[,\-–]\d{1,3})*)(?=[\s.,;])")
CITE_PANDOC_RE = re.compile(r"\[(@[^\]]+)\]|(?<!\w)@([A-Za-z][\w:.\-]+)")
CITE_APA_RE = re.compile(r"\(((?:[A-Z][\w'’\-]+(?:\s(?:et\s+al\.?|and|&)\s*[A-Z]?[\w'’\-]*)*,?\s*(?:19|20)\d{2}[a-z]?(?:,\s*p+\.\s*\d+)?(?:;\s*)?)+)\)")
NUMBER_RE = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?%?")
DIRECTION_WORDS = re.compile(r"\b(increas\w*|decreas\w*|reduc\w*|lower\w*|higher|improv\w*|worsen\w*|associat\w*|predict\w*|significant\w*|non-?inferior\w*|superior\w*|no difference|did not|neutral|null|benefit\w*|harm\w*|risk|hazard|odds|ratio|mortality|incidence|prevalence|sensitivity|specificity|AUC|AUROC|accuracy|recommend\w*|guideline\w*|class\s+[I]+|level\s+[ABC])\b", re.I)


def expand_numeric(tok: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"\s*,\s*", tok):
        m = re.match(r"(\d+)\s*[\-–]\s*(\d+)$", part.strip())
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < b - a <= 50:
                out += [str(i) for i in range(a, b + 1)]
            else:
                out += [m.group(1), m.group(2)]
        elif part.strip():
            out.append(part.strip())
    return out


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\b(et al|Fig|Figs|vs|e\.g|i\.e|Dr|Prof|No|approx|ref|Refs?)\.\s", lambda m: m.group(0).replace(". ", ".\u00a0"), text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\[\(\"“])", text)
    return [squash(p.replace("\u00a0", " ")) for p in parts if squash(p)]


def extract_claims(text: str, style: str = "auto") -> list[dict]:
    body = text
    m = REF_HEADING_RE.search(text)
    if m:
        body = text[:m.start()]
    body = re.sub(r"^---\n.*?\n---\n", "", body, flags=re.S)       # yaml front matter
    body = re.sub(r"```.*?```", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    claims: list[dict] = []
    cid = 0
    for para in re.split(r"\n\s*\n", body):
        if not para.strip() or para.lstrip().startswith("#"):
            continue
        for sent in split_sentences(para):
            cites: list[str] = []
            kinds: list[str] = []
            for pm in CITE_PANDOC_RE.finditer(sent):
                tok = pm.group(1) or pm.group(2)
                for k in re.findall(r"@([A-Za-z][\w:.\-]+)", tok):
                    cites.append(k.rstrip("."))
                kinds.append("pandoc")
            if not cites and style in ("auto", "numeric"):
                for nm in CITE_NUMERIC_RE.finditer(sent):
                    tok = nm.group(1) or nm.group(2) or nm.group(3)
                    if tok:
                        # avoid treating ordinary numbers as superscript cites unless bracketed/caret
                        if nm.group(3) and not re.search(r"[\.\)]$", sent[:nm.start()]):
                            continue
                        cites += expand_numeric(tok)
                        kinds.append("numeric")
            if not cites and style in ("auto", "apa"):
                for am in CITE_APA_RE.finditer(sent):
                    for piece in re.split(r";\s*", am.group(1)):
                        cites.append(squash(piece))
                    kinds.append("author-year")
            if not cites:
                continue
            cid += 1
            numbers = [n.strip() for n in NUMBER_RE.findall(sent) if not re.fullmatch(r"(19|20)\d{2}", n.strip())]
            dirs = sorted({d.lower() for d in DIRECTION_WORDS.findall(sent)})
            kind = "numeric" if numbers else ("directional" if dirs else "attributive")
            claims.append({
                "id": cid, "sentence": sent, "cites": sorted(set(cites), key=lambda x: (len(x), x)), "cite_style": kinds[0] if kinds else "",
                "numbers": numbers, "direction_words": dirs, "claim_type": kind,
                "verdict": "", "tier": "", "source_evidence": "", "severity": "", "note": "",
            })
    return claims


def claims_markdown(claims: list[dict], manuscript: str) -> str:
    lines = [f"# Claims to check - {Path(manuscript).name}", "",
             f"{len(claims)} sentences carry a citation. For each, read the evidence pack for the cited reference(s) and record a verdict:",
             "SUPPORTED / PARTIALLY-SUPPORTED / UNSUPPORTED / CONTRADICTED / MISATTRIBUTED / OWN-DATA / UNVERIFIABLE, with the evidence tier (full_text | abstract | none) and a quoted snippet.", "",
             "| # | Type | Cites | Sentence | Numbers | Verdict | Tier | Evidence (quote) |", "|---|---|---|---|---|---|---|---|"]
    for c in claims:
        lines.append(f"| {c['id']} | {c['claim_type']} | {', '.join(c['cites'])} | {c['sentence'][:160].replace('|','/')} | {', '.join(c['numbers'])[:40]} |  |  |  |")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Find (search-then-cite)
# --------------------------------------------------------------------------------------

def bibtex_entry(rec: dict, key: str = "") -> str:
    authors = rec.get("authors_full") or rec.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    au = " and ".join(authors)
    fa = norm_surname(rec.get("first_author") or (authors[0] if authors else "ref")).title().replace(" ", "")
    key = key or f"{fa}_{rec.get('year','')}_{(rec.get('pmid') or clean_doi(rec.get('doi','')).replace('/','_').replace('.','_'))}"
    key = re.sub(r"[^\w\-:]", "", key)
    f = [f"@article{{{key},", f"  author    = {{{au}}},", f"  title     = {{{rec.get('title','')}}},", f"  journal   = {{{rec.get('journal','')}}},", f"  year      = {{{rec.get('year','')}}},"]
    if rec.get("volume"):
        f.append(f"  volume    = {{{rec['volume']}}},")
    if rec.get("issue"):
        f.append(f"  number    = {{{rec['issue']}}},")
    if rec.get("pages"):
        f.append(f"  pages     = {{{rec['pages'].replace('-', '--')}}},")
    if rec.get("doi"):
        f.append(f"  doi       = {{{rec['doi']}}},")
    if rec.get("pmid"):
        f.append(f"  pmid      = {{{rec['pmid']}}},")
    f.append("  verified  = {true},")
    f.append(f"  verified_by = {{{'+'.join(rec.get('sources') or [])}}},")
    f.append(f"  verified_on = {{{today()}}},")
    if rec.get("retracted"):
        f.append("  note      = {RETRACTED - do not cite as evidence},")
    elif rec.get("preprint"):
        f.append("  note      = {Preprint - not peer reviewed},")
    f.append("}")
    return "\n".join(f)


def find_candidates(http: Http, verifier: Verifier, query: str, max_results: int = 5, year_from: str = "", year_to: str = "") -> list[Result]:
    term = query
    if year_from or year_to:
        term += f' AND ("{year_from or "1900"}"[Date - Publication] : "{year_to or "3000"}"[Date - Publication])'
    pmids, err = pubmed_search(http, term, max_results)
    cands: dict[str, dict] = {}
    for h in pubmed_fetch_many(http, pmids):
        cands[h.doi or f"pmid:{h.pmid}"] = {"pmid": h.pmid, "doi": h.doi, "title": h.title, "first_author": h.first_author, "year": h.year, "journal": h.journal_abbrev or h.journal}
    if "openalex" in verifier.sources:
        url = "https://api.openalex.org/works?search=" + urllib.parse.quote(query) + f"&per-page={max_results}&select=id,doi,title,publication_year,ids,authorships,primary_location,type,is_retracted,biblio" + (f"&filter=publication_year:{year_from or 1900}-{year_to or 2100}" if (year_from or year_to) else "") + (f"&mailto={urllib.parse.quote(http.email)}" if http.email else "")
        status, d = http.get_json(url)
        for w in (d or {}).get("results") or []:
            c = Hit(source="openalex", query="")
            _fill_from_openalex(c, w)
            k = c.doi or f"pmid:{c.pmid}" or c.title
            if k not in cands:
                cands[k] = {"pmid": c.pmid, "doi": c.doi, "title": c.title, "first_author": c.first_author, "year": c.year, "journal": c.journal}
    results: list[Result] = []
    for i, (k, c) in enumerate(list(cands.items())[: max_results * 2], 1):
        ref = Ref(ref_id=f"cand_{i}", raw=k, title=c["title"], first_author=c["first_author"], authors=[c["first_author"]] if c["first_author"] else [], year=c["year"], journal=c["journal"], doi=c["doi"], pmid=c["pmid"], parse_confidence="high")
        results.append(verifier.verify(ref))
    return results


# --------------------------------------------------------------------------------------
# Self-test fixtures (live)
# --------------------------------------------------------------------------------------

SELFTEST_FIXTURES: list[dict] = [
    {"name": "real DOI, cited correctly (LoDoCo2, NEJM 2020)", "expect": "VERIFIED",
     "ref": dict(title="Colchicine in Patients with Chronic Coronary Disease", first_author="Nidorf", year="2020", journal="N Engl J Med", doi="10.1056/NEJMoa2021372")},
    {"name": "real PMID, cited correctly (Warsi, Neurologist 2024)", "expect": "VERIFIED",
     "ref": dict(title="Risk Factors of Acute Ischemic Stroke and Mortality Among Adults With Endocardial Fibroelastosis", first_author="Warsi", year="2024", journal="Neurologist", pmid="39034931")},
    {"name": "fabricated DOI", "expect": "NOT_FOUND",
     "ref": dict(title="Colchicine in Patients with Chronic Coronary Disease", first_author="Nidorf", year="2020", doi="10.1056/NEJMoa9999999")},
    {"name": "fabricated PMID", "expect": "NOT_FOUND",
     "ref": dict(title="Low-dose colchicine and cardiovascular outcomes", first_author="Smith", year="2021", pmid="99999999")},
    {"name": "Frankenstein: real DOI, invented first author", "expect": "MISMATCH",
     "ref": dict(title="Colchicine in Patients with Chronic Coronary Disease", first_author="Ebrahimi", year="2020", journal="N Engl J Med", doi="10.1056/NEJMoa2021372")},
    {"name": "Frankenstein: real DOI, wrong title (COLCOT title on LoDoCo2 DOI)", "expect": "MISMATCH",
     "ref": dict(title="Efficacy and Safety of Low-Dose Colchicine after Myocardial Infarction", first_author="Nidorf", year="2020", doi="10.1056/NEJMoa2021372")},
    {"name": "wrong PMID that points to an unrelated real paper", "expect": "MISMATCH",
     "ref": dict(title="Colchicine in Patients with Chronic Coronary Disease", first_author="Nidorf", year="2020", pmid="39034931")},
    {"name": "DOI and PMID from two different papers", "expect": "MISMATCH",
     "ref": dict(title="Colchicine in Patients with Chronic Coronary Disease", first_author="Nidorf", year="2020", doi="10.1056/NEJMoa2021372", pmid="39034931")},
    {"name": "retracted paper (Mehra, Lancet 2020)", "expect": "RETRACTED",
     "ref": dict(title="Hydroxychloroquine or chloroquine with or without a macrolide for treatment of COVID-19: a multinational registry analysis", first_author="Mehra", year="2020", journal="Lancet", doi="10.1016/S0140-6736(20)31180-6")},
    {"name": "title only, real paper, no identifiers", "expect": "VERIFIED",
     "ref": dict(title="Colchicine in Patients with Chronic Coronary Disease", first_author="Nidorf", year="2020", journal="N Engl J Med")},
    {"name": "title only, fabricated paper", "expect": "NOT_FOUND",
     "ref": dict(title="Intravenous Turmeric and 30-Day Mortality in Cardiogenic Shock: The TURMERIC-SHOCK Randomized Trial", first_author="Smith", year="2023", journal="Lancet")},
    {"name": "year off by one (online-first vs print)", "expect": "VERIFIED_WITH_WARNINGS",
     "ref": dict(title="Colchicine in Patients with Chronic Coronary Disease", first_author="Nidorf", year="2021", journal="N Engl J Med", doi="10.1056/NEJMoa2021372")},
    {"name": "preprint cited (medRxiv DOI prefix)", "expect": "PREPRINT_FLAG",
     "ref": dict(doi="SELFTEST_PREPRINT_DOI")},
]


def run_selftest(http: Http, verifier: Verifier, out_dir: Optional[Path]) -> int:
    print(f"citecheck v{__version__} self-test (live network). This proves the pipeline on your machine.\n")
    # pick a current preprint DOI from CrossRef so the fixture never goes stale
    status, d = http.get_json("https://api.crossref.org/works?filter=prefix:10.1101,type:posted-content&rows=1&select=DOI,title")
    pre_doi = ""
    try:
        pre_doi = clean_doi(d["message"]["items"][0]["DOI"])
    except Exception:
        pass
    results = []
    fails = 0
    for fx in SELFTEST_FIXTURES:
        kw = dict(fx["ref"])
        if kw.get("doi") == "SELFTEST_PREPRINT_DOI":
            if not pre_doi:
                print(f"SKIP  {fx['name']} (could not fetch a preprint DOI from CrossRef)")
                continue
            kw["doi"] = pre_doi
        ref = Ref(ref_id=fx["name"][:40], raw=json.dumps(kw), **kw)
        if ref.first_author and not ref.authors:
            ref.authors = [ref.first_author]
        ref.parse_confidence = "high" if (ref.doi or ref.pmid) else "medium"
        res = verifier.verify(ref)
        results.append(res)
        if fx["expect"] == "PREPRINT_FLAG":
            ok = "PREPRINT" in res.flags
            got = f"{res.status} flags={res.flags}"
        else:
            ok = res.status == fx["expect"]
            got = res.status
        fails += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {fx['name']}\n      expected {fx['expect']:<22} got {got}" + (f"\n      {res.issues[0][:110]}" if res.issues else ""))
    print(f"\n{len(results) - fails}/{len(results)} fixtures behaved as expected. HTTP calls: {http.calls}, cache hits: {http.cache_hits}.")
    if out_dir:
        audit = build_audit(results, "selftest", {"sources": sorted(verifier.sources)})
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "selftest_audit.json").write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
        (out_dir / "selftest_audit.md").write_text(audit_markdown(audit), encoding="utf-8")
        print(f"audit written to {out_dir / 'selftest_audit.json'}")
    return 1 if fails else 0


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "citecheck"


def make_http(args: argparse.Namespace, out_dir: Optional[Path]) -> Http:
    # project runs cache next to their audit (qc/); one-off commands use a per-user cache dir
    cache = None if getattr(args, "no_cache", False) else ((out_dir / ".citecheck-cache.json") if out_dir else default_cache_dir() / "cache.json")
    return Http(email=getattr(args, "email", "") or DEFAULT_EMAIL, timeout=args.timeout, cache_path=cache, ttl_days=getattr(args, "cache_ttl_days", 7), quiet=getattr(args, "quiet", False))


def parse_sources(s: str) -> tuple[str, ...]:
    toks = tuple(t.strip().lower() for t in s.split(",") if t.strip())
    bad = [t for t in toks if t not in ALL_SOURCES]
    if bad:
        raise SystemExit(f"unknown source(s): {', '.join(bad)}; choose from {', '.join(ALL_SOURCES)}")
    return toks or DEFAULT_SOURCES


def cmd_verify(args: argparse.Namespace) -> int:
    if args.strict and args.offline:
        eprint("--strict cannot be combined with --offline")
        return 2
    out_dir = Path(args.out_dir) if args.out_dir else None
    if args.ids:
        refs = refs_from_ids(args.ids)
        source = "command-line identifiers"
    else:
        if not args.input:
            eprint("give an input file or --ids")
            return 2
        p = Path(args.input)
        if not p.exists():
            eprint(f"input not found: {p}")
            return 2
        refs = parse_input(p, args.format)
        source = str(p)
    if args.limit:
        refs = refs[: args.limit]
    if not refs:
        eprint("no references detected (try --format, or check that the file has a References section)")
        return 3
    if args.offline:
        results = []
        for r in refs:
            res = Result(ref_id=r.ref_id, status="UNVERIFIABLE", confidence="low", cited={k: v for k, v in asdict(r).items() if v})
            res.notes.append("offline: parsed only, nothing verified")
            results.append(res)
    else:
        http = make_http(args, out_dir)
        verifier = Verifier(http, parse_sources(args.sources), resolve_doi_org=not args.no_doi_resolve, quiet=args.quiet)
        results = []
        for i, r in enumerate(refs, 1):
            if not args.quiet:
                eprint(f"[{i}/{len(refs)}] {r.ref_id}: {(r.doi and 'doi:' + r.doi) or (r.pmid and 'pmid:' + r.pmid) or (r.title[:60] or r.raw[:60])}")
            results.append(verifier.verify(r))
        http.save_cache()
    audit = build_audit(results, source, {"sources": list(parse_sources(args.sources))})
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "reference_audit.json").write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
        (out_dir / "reference_audit.md").write_text(audit_markdown(audit), encoding="utf-8")
    if args.json:
        print(json.dumps(audit, indent=2, ensure_ascii=False))
    else:
        print_summary(audit, args.quiet)
        if out_dir:
            print(f"written: {out_dir / 'reference_audit.json'} and reference_audit.md")
    if audit["blocking_ref_ids"]:
        return 1
    if args.strict and any(r.status != "VERIFIED" for r in results):
        return 1
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    http = make_http(args, None)
    verifier = Verifier(http, parse_sources(args.sources), resolve_doi_org=not args.no_doi_resolve, quiet=True)
    refs = refs_from_ids([" ".join(args.query)])
    res = verifier.verify(refs[0])
    http.save_cache()
    out = {"status": res.status, "confidence": res.confidence, "found_by": res.found_by, "flags": res.flags, "sources_confirming": res.sources_confirming, "issues": res.issues, "notes": res.notes, "resolved": res.resolved}
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f"status: {res.status} (confidence {res.confidence}, found by {res.found_by or '-'}, sources: {', '.join(res.sources_confirming) or '-'})")
        if res.flags:
            print("flags: " + ", ".join(res.flags))
        for i in res.issues:
            print("issue: " + i)
        for n in res.notes:
            print("note: " + n)
        if res.resolved:
            print("record: " + vancouver(res.resolved))
    return 0 if res.status in ("VERIFIED", "VERIFIED_WITH_WARNINGS") else 1


def cmd_evidence(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    http = make_http(args, out_dir)
    p = Path(args.input)
    if not p.exists():
        eprint(f"input not found: {p}")
        return 2
    records: list[dict]
    if p.suffix.lower() == ".json" and "records" in p.read_text(encoding="utf-8", errors="replace")[:2000]:
        audit = json.loads(p.read_text(encoding="utf-8"))
        records = audit["records"]
    else:
        refs = parse_input(p, args.format)
        if not refs:
            eprint("no references detected")
            return 3
        verifier = Verifier(http, parse_sources(args.sources), quiet=args.quiet)
        results = [verifier.verify(r) for r in refs]
        audit = build_audit(results, str(p), {"sources": list(parse_sources(args.sources))})
        (out_dir).mkdir(parents=True, exist_ok=True)
        (out_dir / "reference_audit.json").write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
        (out_dir / "reference_audit.md").write_text(audit_markdown(audit), encoding="utf-8")
        records = audit["records"]
    ev_dir = out_dir / "evidence"
    index = []
    for rec in records:
        rid = rec["ref_id"]
        if args.only and rid not in args.only:
            continue
        resolved = rec.get("resolved") or {}
        if not resolved or rec.get("status") in ("NOT_FOUND", "UNVERIFIABLE"):
            index.append({"ref_id": rid, "status": rec.get("status"), "tier": "none", "file": None, "note": "reference not resolved; nothing to fetch"})
            if not args.quiet:
                eprint(f"{rid}: skipped ({rec.get('status')})")
            continue
        ev = fetch_evidence_for(http, resolved, want_fulltext=not args.abstract_only)
        path = write_evidence_pack(ev_dir, rid, resolved, ev)
        index.append({"ref_id": rid, "status": rec.get("status"), "tier": ev["tier"], "file": str(path), "pmid": resolved.get("pmid"), "doi": resolved.get("doi"), "pmcid": ev.get("pmcid"), "retracted": resolved.get("retracted"), "preprint": resolved.get("preprint"), "chars": len(ev["fulltext"] or ev["abstract"])})
        if not args.quiet:
            eprint(f"{rid}: {ev['tier']} ({len(ev['fulltext'] or ev['abstract'])} chars)")
    http.save_cache()
    ev_dir.mkdir(parents=True, exist_ok=True)
    (ev_dir / "index.json").write_text(json.dumps({"generated": utcnow(), "source": str(p), "packs": index}, indent=2, ensure_ascii=False), encoding="utf-8")
    tiers = {}
    for i in index:
        tiers[i["tier"]] = tiers.get(i["tier"], 0) + 1
    print(f"evidence packs: {len(index)} ({', '.join(f'{k}={v}' for k, v in sorted(tiers.items()))}) -> {ev_dir}")
    return 0


def cmd_claims(args: argparse.Namespace) -> int:
    p = Path(args.input)
    if not p.exists():
        eprint(f"input not found: {p}")
        return 2
    text = read_text(p)
    claims = extract_claims(text, args.style)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "claims_to_check.json").write_text(json.dumps({"manuscript": str(p), "generated": utcnow(), "claims": claims}, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "claims_to_check.md").write_text(claims_markdown(claims, str(p)), encoding="utf-8")
    types = {}
    for c in claims:
        types[c["claim_type"]] = types.get(c["claim_type"], 0) + 1
    print(f"{len(claims)} cited sentences extracted ({', '.join(f'{k}={v}' for k, v in sorted(types.items()))}) -> {out_dir / 'claims_to_check.json'}")
    return 0 if claims else 3


def cmd_find(args: argparse.Namespace) -> int:
    http = make_http(args, Path(args.out_dir) if args.out_dir else None)
    verifier = Verifier(http, parse_sources(args.sources), quiet=True)
    results = find_candidates(http, verifier, " ".join(args.query), args.max, args.year_from, args.year_to)
    http.save_cache()
    good = [r for r in results if r.status in ("VERIFIED", "VERIFIED_WITH_WARNINGS", "RETRACTED") and r.resolved]
    if args.json:
        print(json.dumps([{"status": r.status, "flags": r.flags, "sources": r.sources_confirming, "record": r.resolved, "vancouver": vancouver(r.resolved)} for r in good], indent=2, ensure_ascii=False))
    else:
        print(f"{len(good)} verified candidates for: {' '.join(args.query)}\n")
        for i, r in enumerate(good, 1):
            tag = " ".join(f"[{f}]" for f in r.flags)
            print(f"{i}. {vancouver(r.resolved)}  {tag}\n   confirmed by: {', '.join(r.sources_confirming)}")
    if args.bib:
        with open(args.bib, "a", encoding="utf-8") as fh:
            for r in good:
                fh.write(bibtex_entry(r.resolved) + "\n\n")
        print(f"\nappended {len(good)} BibTeX entries (with verified_on stamps) to {args.bib}")
    return 0 if good else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="citecheck", description="Verify that references exist and are cited correctly, using CrossRef, PubMed, OpenAlex and Europe PMC. Standard library only; no API keys.")
    ap.add_argument("--version", action="version", version=f"citecheck {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser, out_default: Optional[str] = "qc") -> None:
        sp.add_argument("--email", default="", help="contact email for polite API pools (or set CITECHECK_EMAIL)")
        sp.add_argument("--sources", default=",".join(DEFAULT_SOURCES), help=f"comma list from {', '.join(ALL_SOURCES)}")
        sp.add_argument("--timeout", type=int, default=20)
        sp.add_argument("--no-cache", action="store_true", help="do not read/write the local response cache")
        sp.add_argument("--cache-ttl-days", type=int, default=7)
        sp.add_argument("--no-doi-resolve", action="store_true", help="skip the doi.org HEAD check")
        sp.add_argument("--quiet", action="store_true")
        if out_default is not None:
            sp.add_argument("--out-dir", default=out_default, help="where to write audit files (default: qc/)")

    v = sub.add_parser("verify", help="verify references in a file (bib, ris, md, docx, txt, json, csv, tsv, tex) or given as --ids")
    v.add_argument("input", nargs="?")
    v.add_argument("--ids", nargs="+", help="DOIs, PMIDs or quoted titles instead of a file")
    v.add_argument("--format", default="auto", choices=["auto", "bibtex", "ris", "json", "csv", "tsv", "text", "tex"])
    v.add_argument("--offline", action="store_true", help="parse only; every row becomes UNVERIFIABLE")
    v.add_argument("--strict", action="store_true", help="exit 1 unless every reference is VERIFIED")
    v.add_argument("--limit", type=int, default=0)
    v.add_argument("--json", action="store_true", help="print the full audit JSON to stdout")
    common(v)
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("resolve", help="look up one DOI, PMID or title and print the consensus record")
    r.add_argument("query", nargs="+")
    r.add_argument("--json", action="store_true")
    common(r, out_default=None)
    r.set_defaults(func=cmd_resolve)

    e = sub.add_parser("evidence", help="fetch abstracts and open-access full text for each verified reference (for claim checking)")
    e.add_argument("input", help="reference file, or an existing qc/reference_audit.json")
    e.add_argument("--format", default="auto", choices=["auto", "bibtex", "ris", "json", "csv", "tsv", "text", "tex"])
    e.add_argument("--only", nargs="+", help="ref_ids to fetch")
    e.add_argument("--abstract-only", action="store_true")
    common(e)
    e.set_defaults(func=cmd_evidence)

    c = sub.add_parser("claims", help="extract every sentence that carries a citation, as a checklist for claim verification")
    c.add_argument("input")
    c.add_argument("--style", default="auto", choices=["auto", "numeric", "pandoc", "apa"])
    c.add_argument("--out-dir", default="qc")
    c.set_defaults(func=cmd_claims)

    f = sub.add_parser("find", help="search PubMed and OpenAlex, verify each hit, print Vancouver + optional BibTeX")
    f.add_argument("query", nargs="+")
    f.add_argument("--max", type=int, default=5)
    f.add_argument("--year-from", default="")
    f.add_argument("--year-to", default="")
    f.add_argument("--bib", default="", help="append verified BibTeX entries to this file")
    f.add_argument("--json", action="store_true")
    common(f, out_default="")
    f.set_defaults(func=cmd_find)

    s = sub.add_parser("selftest", help="run the live fixtures: real, fabricated, frankenstein, retracted, preprint, title-only")
    common(s, out_default="")
    s.set_defaults(func=cmd_selftest)
    return ap


def cmd_selftest(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir) if args.out_dir else None
    http = make_http(args, out_dir)
    verifier = Verifier(http, parse_sources(args.sources), resolve_doi_org=not args.no_doi_resolve, quiet=True)
    code = run_selftest(http, verifier, out_dir)
    http.save_cache()
    return code


def main(argv: Optional[list[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        eprint("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
