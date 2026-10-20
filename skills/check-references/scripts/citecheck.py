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
OPENALEX_API_KEY = os.environ.get("OPENALEX_API_KEY", "").strip()

TITLE_MATCH = 0.92      # >= this: titles agree
TITLE_WARN = 0.80       # between WARN and MATCH: warning; below WARN: major mismatch
YEAR_WARN = 1           # |cited - source| <= 1 -> warning (online-first vs print); > 1 -> major

ALL_SOURCES = ("crossref", "pubmed", "openalex", "europepmc", "datacite", "semanticscholar")
DEFAULT_SOURCES = ("crossref", "pubmed", "openalex", "europepmc", "datacite")

# Minimum seconds between calls to one host (polite defaults; NCBI allows 3/s without a key).
HOST_INTERVAL = {
    "eutils.ncbi.nlm.nih.gov": 0.11 if NCBI_API_KEY else 0.35,
    "pmc.ncbi.nlm.nih.gov": 0.35,
    "api.crossref.org": 0.12,
    "api.openalex.org": 0.12,
    "www.ebi.ac.uk": 0.25,
    "api.semanticscholar.org": 1.2,
    "api.datacite.org": 0.25,
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

# Wiley SICI DOIs legitimately contain '<' and '>' (10.1002/(SICI)1097-0258(19980430)17:8<857::AID-SIM777>3.0.CO;2-E)
DOI_RE = re.compile(r"10\.\d{4,9}/(?:\(SICI\)[^\s\"']+|[^\s\"'<>\]\[{}]+)", re.I)
PMID_RE = re.compile(r"\b(?:PMID|PubMed\s+ID)\s*:?\s*(\d{5,9})\b", re.I)
PUBMED_URL_RE = re.compile(r"(?:pubmed\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pubmed)/(\d{5,9})", re.I)
PMCID_RE = re.compile(r"\b(PMC\d{5,9})\b", re.I)
YEAR_RE = re.compile(r"\b(19\d{2}|20[0-4]\d)\b")
URL_RE = re.compile(r"https?://\S+", re.I)
# Publisher label on a retracted/withdrawn article's title (uppercase + separator; a paper ABOUT retractions does not match)
RETRACTED_LABEL_RE = re.compile(r"^\s*(?:RETRACTED|WITHDRAWN)(?:\s+ARTICLE)?\s*[:\-–]")
# A record that IS a notice (erratum, correction, retraction notice ...) rather than the article
NOTICE_PREFIX_RE = re.compile(
    r"^\s*(?:correction|corrigendum|erratum|errata|author correction|publisher correction|retraction(?:\s+notice|\s+note)?|"
    r"notice of retraction|expression of concern|withdrawal notice|retracted article)\b\s*(?:to|for|of)?\s*[:\-–]", re.I)


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
    author_parse_failed: bool = False   # the citation carried an author block the parser could not read
    extra_dois: list[str] = field(default_factory=list)   # further DOIs found in the same item (supplements, errata)
    is_web_resource: bool = False   # URL-only / organisation web page: not a journal article
    parse_notes: list[str] = field(default_factory=list)


class InputError(Exception):
    """A problem with the input file that the user must fix (exit code 2)."""


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
    notice_type: str = ""      # this record IS a correction/erratum/retraction notice, not the article
    notice_for: str = ""       # DOI of the article the notice refers to, when known
    weak_retraction: bool = False   # a hint (CrossRef update-to) that needs corroboration
    abstract: str = ""
    licence: str = ""
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
    long_raw = b if len(na) <= len(nb) else a
    # Cited titles often drop a subtitle; treat clean containment as a near-match, unless the longer
    # title is a notice ("Correction to: <article title>") -- that is a different record, not a subtitle.
    if len(short) >= 25 and short in long_ and len(short) / len(long_) >= 0.55 and not NOTICE_PREFIX_RE.match(long_raw or ""):
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
    while len(toks) >= 2 and re.fullmatch(r"(?:Jr|Sr|II|III|IV|2nd|3rd|4th)\.?", toks[-1]):   # 'Smith AB Jr', 'Stokes J 3rd'
        toks.pop()
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
    """'N Engl J Med' == 'The New England journal of medicine'; 'BMJ' != 'BMJ Open'; 'Lancet' != 'Lancet Digital Health'."""
    ct = norm_journal_tokens(cited)
    if not ct:
        return True

    def abbrev_of(short: list[str], full: list[str]) -> bool:
        # one-to-one: same number of (non-stopword) tokens, each abbreviated token a prefix of its counterpart
        return len(short) == len(full) and all(f.startswith(s) or s.startswith(f) for s, f in zip(short, full))

    for cand in candidates:
        kt = norm_journal_tokens(cand)
        if not kt:
            continue
        if ct == kt or abbrev_of(ct, kt):
            return True
        a, b = set(ct), set(kt)
        if min(len(a), len(b)) >= 2 and len(a & b) / len(a | b) >= 0.6:
            return True
    return False


def clean_doi(doi: str) -> str:
    d = (doi or "").strip()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", d, flags=re.I)
    d = re.split(r"[?#&]", d, maxsplit=1)[0]              # a DOI copied from a URL keeps ?query=... or #fragment
    d = re.sub(r"/(?:full|abstract|pdf|epdf|fulltext|html|summary)$", "", d, flags=re.I)
    d = d.rstrip(".,;:)]}\"'")
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

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def read_docx_text(path: Path) -> str:
    """Word paragraphs, one per block (blank line between). Word's automatic list numbering (w:numPr) is
    not stored as text, so numbered paragraphs get a synthetic 'N. ' label; superscript runs that are
    digits become ^n^ so citation markers survive."""
    try:
        with zipfile.ZipFile(path) as zf:
            try:
                info = zf.getinfo("word/document.xml")
            except KeyError:
                raise InputError(f"{path.name} is not a Word document (no word/document.xml inside)")
            if info.file_size > 60_000_000 or info.file_size / max(info.compress_size, 1) > 250:
                raise InputError(f"{path.name}: document.xml is suspiciously large ({info.file_size:,} bytes); refusing to parse")
            xml = zf.read("word/document.xml")
    except zipfile.BadZipFile:
        raise InputError(f"{path.name} is not a valid .docx file (not a zip archive). Re-save it from Word as .docx, or export the reference list to .txt")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise InputError(f"{path.name}: cannot parse document.xml ({exc})")
    ns = {"w": _W_NS}
    out: list[str] = []
    counter = 0
    for p in root.findall(".//w:p", ns):
        numbered = p.find("w:pPr/w:numPr", ns) is not None
        pieces: list[str] = []
        for r in p.findall(".//w:r", ns):
            txt = "".join(t.text or "" for t in r.findall("w:t", ns))
            if not txt:
                continue
            va = r.find("w:rPr/w:vertAlign", ns)
            if va is not None and va.get(f"{{{_W_NS}}}val") == "superscript" and re.fullmatch(r"[\d,\-–\s]+", txt):
                pieces.append(f"^{txt.strip()}^")
            else:
                pieces.append(txt)
        text = "".join(pieces).strip()
        if not text:
            continue
        if numbered and not ITEM_START_RE.match(text):
            counter += 1
            text = f"{counter}. {text}"
        elif not numbered:
            counter = 0
        out.append(text)
    return "\n\n".join(out)


BINARY_EXT = {".pdf": "PDF", ".doc": "legacy Word (.doc)", ".rtf": "RTF", ".odt": "OpenDocument", ".pages": "Pages", ".xlsx": "Excel", ".pptx": "PowerPoint"}


def decode_bytes(raw: bytes, name: str = "") -> str:
    """BOM-aware decoding: UTF-16 (Notepad/Word 'Unicode'), UTF-8 with or without BOM, then cp1252 (Word 'Plain text' on Windows)."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if b"\x00" in raw[:4096]:
        raise InputError(f"{name} does not look like a text file (binary content). Save the reference list as .txt, .docx, .bib or .ris")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        eprint(f"[citecheck] {name}: not UTF-8; decoding as Windows-1252")
        return raw.decode("cp1252", errors="replace")


def read_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in BINARY_EXT:
        raise InputError(f"{BINARY_EXT[ext]} files cannot be read. In Word use File > Save As > Word Document (.docx), or copy the reference list into a .txt file and run verify on that")
    if ext == ".docx":
        return read_docx_text(path)
    raw = path.read_bytes()
    if raw[:5] == b"%PDF-":
        raise InputError(f"{path.name} is a PDF. Export the manuscript or its reference list to .docx or .txt first")
    return decode_bytes(raw, path.name)


def detect_format(path: Path, override: str = "auto") -> str:
    if override and override != "auto":
        return override
    ext = path.suffix.lower()
    return {
        ".bib": "bibtex", ".ris": "ris", ".json": "json", ".csv": "csv", ".tsv": "tsv",
        ".docx": "text", ".md": "text", ".qmd": "text", ".rmd": "text", ".txt": "text", ".tex": "tex", ".bbl": "tex",
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


def _bib_entries(text: str) -> list[tuple[str, str, str]]:
    """Brace-balanced scan: (type, body, raw) for every @type{...} or @type(...) entry, wherever the
    closing brace sits (one-line entries from doi.org/CrossRef, '}}' endings, CRLF files)."""
    out: list[tuple[str, str, str]] = []
    start_re = re.compile(r"@(\w+)\s*([{(])")
    pos, n = 0, len(text)
    while True:
        m = start_re.search(text, pos)
        if not m:
            break
        close_ch = "}" if m.group(2) == "{" else ")"
        j, brace, in_quote, end = m.end(), 0, False, None
        while j < n:
            ch = text[j]
            if ch == '"' and brace == 0 and text[j - 1] != "\\":
                in_quote = not in_quote
            elif not in_quote:
                if ch == "{":
                    brace += 1
                elif ch == "}":
                    if brace == 0 and close_ch == "}":
                        end = j
                        break
                    brace -= 1
                elif ch == ")" and close_ch == ")" and brace == 0:
                    end = j
                    break
            j += 1
        if end is None:
            break
        out.append((m.group(1).lower(), text[m.end():end], text[m.start():end + 1]))
        pos = end + 1
    return out


def _resolve_bib_macro(value: str, raw_value: str, macros: dict[str, str]) -> str:
    """Substitute @string macros in bare values and '#'-concatenations."""
    if "#" in raw_value or (raw_value.strip() and raw_value.strip()[0] not in "{\"" and not raw_value.strip().isdigit()):
        parts = []
        for piece in raw_value.split("#"):
            piece = piece.strip()
            if piece.startswith(("{", '"')):
                parts.append(re.sub(r"^[{\"]|[}\"]$", "", piece))
            else:
                parts.append(macros.get(piece.lower(), piece))
        return squash("".join(parts))
    return value


def parse_bibtex(text: str) -> list[Ref]:
    refs: list[Ref] = []
    macros: dict[str, str] = {}
    for etype, body, raw in _bib_entries(text):
        if etype in {"comment", "preamble"}:
            continue
        if etype == "string":
            sm = re.match(r"\s*([\w\-:]+)\s*=\s*(.*)$", body, flags=re.S)
            if sm:
                macros[sm.group(1).lower()] = squash(re.sub(r"^[{\"]|[}\"]$", "", sm.group(2).strip()))
            continue
        km = re.match(r"\s*([^,\s]+)\s*,", body)
        if not km:
            continue
        key, body = km.group(1), body[km.end():]
        f = _bib_fields(body)
        if macros:
            for fname in ("journal", "journaltitle", "booktitle", "publisher"):
                rm = re.search(r"\b" + fname + r"\s*=\s*([^,\n]+(?:\s*#\s*[^,\n]+)*)", body, flags=re.I)
                if rm and fname in f:
                    f[fname] = _resolve_bib_macro(f[fname], rm.group(1), macros)
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
            ref_id=key, key=key, raw=squash(raw)[:600],
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
            # RIS 'ID' is the reference manager's record number, never a PMID. A bare number in AN
            # is a PMID only when the record says it came from PubMed/MEDLINE.
            from_pubmed = any(re.search(r"pubmed|medline|nlm", v, re.I) for v in cur.get("DB", []) + cur.get("DP", []))
            for cand in cur.get("AN", []) + cur.get("N1", []) + cur.get("UR", []) + cur.get("L1", []):
                pm = PMID_RE.search(cand) or PUBMED_URL_RE.search(cand) or (from_pubmed and re.fullmatch(r"(\d{5,9})", cand.strip()))
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
    """Accepts a list of records, {"references": [...]}, a citecheck audit, or CSL-JSON (Zotero, Mendeley,
    pandoc): keys are matched case-insensitively and CSL aliases (DOI, PMID, author[{family,given}],
    issued.date-parts, container-title, page) are understood."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"not valid JSON: {exc}")
    if isinstance(data, dict):
        items = data.get("references") or data.get("items")
        if items is None and isinstance(data.get("records"), list) and data.get("tool") == TOOL_NAME:
            items = [r.get("cited", r) for r in data["records"]]
        if items is None:
            items = [data] if any(k.lower() in {"title", "doi", "pmid"} for k in data) else []
    else:
        items = data
    if not isinstance(items, list):
        raise InputError("JSON must be a list of reference objects, or an object with a 'references' list")
    refs: list[Ref] = []
    for i, it in enumerate(items, 1):
        if isinstance(it, str):
            refs.append(parse_free_citation(it, f"ref_{i}"))
            continue
        if not isinstance(it, dict):
            continue
        low = {str(k).lower().replace("_", "-"): v for k, v in it.items()}

        def g(*names: str) -> Any:
            for nme in names:
                v = low.get(nme)
                if v not in (None, "", [], {}):
                    return v
            return ""
        authors_raw = g("authors", "author", "creators")
        authors: list[str] = []
        authors_full: list[str] = []
        if isinstance(authors_raw, str):
            authors_raw = [a.strip() for a in re.split(r";|\s+and\s+|,\s*(?=[A-Z][a-z]+(?:\s+[A-Z]{1,3})?\s*(?:,|$))", authors_raw) if a.strip()]
        for a in authors_raw if isinstance(authors_raw, list) else []:
            if isinstance(a, dict):
                fam = a.get("family") or a.get("familyName") or a.get("last") or a.get("lastName") or ""
                giv = a.get("given") or a.get("givenName") or a.get("first") or ""
                lit = a.get("literal") or a.get("name") or ""
                sn = squash(fam) or surname_from_display(lit)
            else:
                sn, giv = surname_from_display(str(a)), ""
            if sn:
                authors.append(sn)
                authors_full.append(sn + (f", {squash(giv)}" if giv else ""))
        year = ""
        issued = g("issued", "date", "published", "year", "publication-year", "pubyear")
        if isinstance(issued, dict):
            parts = issued.get("date-parts") or [[]]
            year = str(parts[0][0]) if parts and parts[0] else str(year_int(str(issued.get("raw") or issued.get("literal") or "")) or "")
        else:
            year = str(year_int(str(issued)) or "")
        title = g("title")
        if isinstance(title, list):
            title = " ".join(str(t) for t in title)
        doi_raw = g("doi")
        ref = Ref(
            ref_id=str(g("id", "ref-id", "key", "citation-key") or f"ref_{i}"),
            raw=json.dumps(it, ensure_ascii=False)[:600],
            title=squash(str(title)), authors=authors,
            first_author=squash(str(g("first-author"))) or (authors[0] if authors else ""),
            year=year, journal=squash(str(g("journal", "container-title", "container-title-short", "publicationtitle", "source"))),
            volume=str(g("volume")), issue=str(g("issue", "number")), pages=str(g("pages", "page")),
            doi=clean_doi(str(doi_raw)) if doi_raw else "", pmid=re.sub(r"\D", "", str(g("pmid", "pubmed-id", "pubmed"))),
            pmcid=str(g("pmcid")), url=str(g("url")), style_guess="json",
        )
        if not ref.doi and ref.url and DOI_RE.search(ref.url):
            ref.doi = clean_doi(DOI_RE.search(ref.url).group(0))
        if not ref.pmid and ref.url and PUBMED_URL_RE.search(ref.url):
            ref.pmid = PUBMED_URL_RE.search(ref.url).group(1)
        ref.parse_confidence = "high" if (ref.doi or ref.pmid) else ("medium" if (ref.title and ref.year) else "low")
        refs.append(ref)
    return refs


def parse_delimited(text: str, delimiter: str) -> list[Ref]:
    rows = list(csv.DictReader(io.StringIO(text), delimiter=delimiter))
    refs: list[Ref] = []
    for i, row in enumerate(rows, 1):
        low = {(k or "").strip().lstrip("﻿").lower(): (v or "").strip() for k, v in row.items()}
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


def _detex(s: str) -> str:
    s = s.replace("~", " ").replace("--", "-").replace("\\&", "&").replace("\\%", "%")
    s = re.sub(r"\\['\"^`~=.uvHtcdbr]\s*\{?(\w)\}?", r"\1", s)          # accents: \'e -> e
    s = re.sub(r"\{\\(?:em|it|bf|sc)\s+([^{}]*)\}", r"\1", s)            # {\em text}
    s = re.sub(r"\\(?:emph|textit|textbf|textsc|url|href)\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\[A-Za-z]+\s*", " ", s)                              # remaining macros
    return squash(s.replace("{", "").replace("}", ""))


def parse_tex_bibitems(text: str) -> list[Ref]:
    """\\bibitem entries, including BibTeX-generated .bbl output (display-order authors, \\newblock separators)."""
    refs = []
    for i, m in enumerate(re.finditer(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}(.*?)(?=\\bibitem|\\end\{thebibliography\}|\Z)", text, flags=re.S), 1):
        key, body = m.group(1), m.group(2)
        blocks = [_detex(b) for b in re.split(r"\\newblock", body) if squash(_detex(b))]
        if len(blocks) >= 3:
            authors_blk, title_blk, rest = blocks[0], blocks[1], " ".join(blocks[2:])
            ref = Ref(ref_id=key, key=key, raw=squash(_detex(body))[:600], style_guess="bbl")
            for chunk in re.split(r",\s*|\s+and\s+|\s*&\s*", authors_blk.rstrip(" ."))[:200]:
                chunk = chunk.strip(" .")
                if chunk and not chunk.lower().startswith("et al") and chunk.lower() != "others":
                    ref.authors.append(surname_from_display(chunk))
            ref.authors = [a for a in ref.authors if a]
            ref.first_author = ref.authors[0] if ref.authors else ""
            ref.title = title_blk.rstrip(" .")
            dm = DOI_RE.search(rest) or DOI_RE.search(body)
            if dm:
                ref.doi = clean_doi(dm.group(0))
            ym = re.findall(r"\b(19\d{2}|20[0-4]\d)\b", rest)
            ref.year = ym[-1] if ym else ""
            jm = re.match(r"([^,0-9]+?)\s*,", rest)
            ref.journal = jm.group(1).strip() if jm else ""
            vm = re.search(r"(\d+)\s*(?:\((\w+)\))?\s*:\s*([\w\-–]+)", rest)
            if vm:
                ref.volume, ref.issue, ref.pages = vm.group(1), vm.group(2) or "", vm.group(3)
            ref.parse_confidence = "high" if ref.doi else ("medium" if (ref.title and ref.year and ref.first_author) else "low")
        else:
            ref = parse_free_citation(" ".join(blocks) if blocks else _detex(body), key)
            ref.key = key
        refs.append(ref)
    return refs


REF_HEADING_RE = re.compile(
    r"(?im)^\s*(?:#+\s*)?(?:\*\*|__)?\s*(?:\d+\.?\s*)?"
    r"(references?(?:\s+and\s+notes)?|reference list|bibliography|works cited|literature(?:\s+cited)?|sources|citations)"
    r"\s*:?\s*(?:\*\*|__)?\s*$")
# Where a reference list ends inside a manuscript: tables, figure legends, appendices (with or without '#')
SECTION_END_RE = re.compile(r"(?im)^\s*(?:#+\s*)?(?:\*\*|__)?\s*(?:tables?|figures?|fig\.?|figure legends?|legends?|supplement\w*|appendi\w*|acknowledg\w*)\b")
ITEM_START_RE = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})[.)]|(\d{1,3})\s{2,}|[-*•]\s+)\s*")


def reference_section(text: str) -> str:
    m = REF_HEADING_RE.search(text)
    if not m:
        return text
    tail = text[m.end():]
    # stop at the next section that is not part of the list (tables, figure legends, appendix ...)
    stop = SECTION_END_RE.search(tail)
    return tail[:stop.start()] if stop else tail


def looks_like_citation(txt: str) -> bool:
    """A citation START: identifiers, or an anchored author list. A '(2021)' somewhere inside a body
    sentence is not enough -- that would send manuscript prose to the search APIs."""
    t = squash(txt)
    t = re.sub(r"^\s*(?:\[\d{1,3}\]|\d{1,3}[.)])\s*", "", t)
    return bool(DOI_RE.search(t) or PMID_RE.search(t) or PUBMED_URL_RE.search(t)
                or AUTHOR_LIST_VANCOUVER.match(t) or APA_START_RE.match(t)
                or re.match(r"^[A-Z][\w&'’\- ]{3,120}?(?:Group|Investigators|Committee|Collaborat\w+|Consortium|Organization|Organisation|Society|Association|Institute|College|Agency)\b", t))


def split_reference_items(section: str, strict: bool = False) -> list[tuple[str, str]]:
    """Return (label, text) items. Label is the numeric label when present.
    strict=True (no References heading found): keep only items shaped like citations, so body
    sentences are not mistaken for references."""
    lines = [ln.rstrip() for ln in section.splitlines()]
    items: list[tuple[str, str]] = []
    cur_label, cur = "", ""
    nonempty = [ln for ln in lines if ln.strip()]
    numbered = sum(1 for ln in nonempty if ITEM_START_RE.match(ln)) >= 2
    if numbered:
        prev_blank = False
        for ln in lines:
            if not ln.strip():
                prev_blank = True
                continue
            m = ITEM_START_RE.match(ln)
            # an unnumbered paragraph that itself starts like a citation is its own item, not a continuation
            if m or (prev_blank and cur and looks_like_citation(ln) and _item_complete(cur)):
                if cur:
                    items.append((cur_label, squash(cur)))
                cur_label = (next((g for g in m.groups() if g), "") or "") if m else ""
                cur = ln[m.end():] if m else ln.strip()
            else:
                cur = (cur + " " + ln.strip()) if cur else ln.strip()
            prev_blank = False
        if cur:
            items.append((cur_label, squash(cur)))
    else:
        # One reference per line (PubMed 'Cite', APA lists, Word paragraphs) is the commonest shape:
        # split per line when most lines start like a citation; otherwise fall back to blank-line paragraphs.
        starts = sum(1 for ln in nonempty if looks_like_citation(ln))
        if len(nonempty) >= 2 and starts >= max(2, int(0.6 * len(nonempty))):
            for ln in nonempty:
                if looks_like_citation(ln) or not items:
                    items.append(("", squash(ln)))
                else:   # continuation of a wrapped reference
                    lbl, prev = items[-1]
                    items[-1] = (lbl, squash(prev + " " + ln))
        else:
            para: list[str] = []
            for ln in lines + [""]:
                if ln.strip():
                    para.append(ln.strip())
                elif para:
                    items.append(("", squash(" ".join(para))))
                    para = []
    # keep only things that look like references
    good = []
    for label, txt in items:
        if len(txt) < 25 and not URL_RE.search(txt):
            continue
        if strict:
            if looks_like_citation(txt) or label:
                good.append((label, txt))
        elif DOI_RE.search(txt) or PMID_RE.search(txt) or YEAR_RE.search(txt) or URL_RE.search(txt) or len(txt.split()) >= 8:
            good.append((label, txt))
    return good


def _item_complete(txt: str) -> bool:
    """Does an accumulated item already look like a whole reference (ends in year/pages/DOI/URL)?"""
    t = txt.rstrip(" .")
    return bool(DOI_RE.search(t) or URL_RE.search(t) or re.search(r"(?:19|20)\d{2}\s*(?:;[\d\w()\s:,\-–]*)?$", t) or re.search(r"\d+\s*[\-–]\s*\d+$|\(\d{4}\)", t))


_UC = "A-ZÀ-ÖØ-Þ"      # uppercase Latin letters including Ö, Ø, Ç, É ...
_LC = "a-zß-öø-ÿ"
_PARTICLES_RE = r"(?:(?:[Vv]an|[Vv]on|[Dd]e|[Dd]er|[Dd]en|[Dd]el|[Dd]ella|[Dd]a|[Dd]i|[Ll]a|[Ll]e|[Dd]u|[Tt]en|[Tt]er|[Dd]os|[Dd]as|[Ee]l|[Aa]l|[Bb]in|[Ii]bn|[Mm]ac)\s)*"
# A surname must contain a lowercase letter, so an all-caps word (a shouted title, a block of initials) can
# never be read as a surname. That keeps the author-list regex linear on adversarial input.
_SURNAME_RE = rf"[{_UC}][{_UC}{_LC}'’\-]*[{_LC}][{_UC}{_LC}'’\-]*(?:\s[{_UC}][{_UC}{_LC}'’\-]*[{_LC}][{_UC}{_LC}'’\-]*)?"
_INITIALS_RE = r"(?:[A-Z]{1,3}(?:-[A-Z])?\b\.?|[A-Z]\.(?:\s?-?[A-Z]\.)*)"
_SUFFIX_RE = r"(?:\s(?:Jr|Sr|II|III|IV|2nd|3rd|4th)\b\.?)?"
_AUTHOR_ONE = _PARTICLES_RE + _SURNAME_RE + r"\s" + _INITIALS_RE + _SUFFIX_RE
_SEP_RE = r"(?:,\s*|;\s*|\s*&\s*|\s+and\s+)"
AUTHOR_LIST_VANCOUVER = re.compile(
    r"^(?P<authors>" + _AUTHOR_ONE + r"(?:" + _SEP_RE + _AUTHOR_ONE + r"){0,80}"
    r"(?:,?\s*(?:et\s+al\.?|and\s+others|\.{3}))?"
    r"(?:,?\s*(?:for\s+the\s+|on\s+behalf\s+of\s+the\s+)?[A-Z][\w'’\-\s]{2,80}?(?:Group|Investigators|Committee|Collaborat\w+|Consortium|Study|Network|Team)\b\.?)?)"
    r"(?:\s*[.;]|(?<=\.))\s+(?P<rest>.*)$"
)
# APA reference start: 'Nidorf, S. M.,' / 'van der Meer, P.' -- anchored, used to recognise citation starts
APA_START_RE = re.compile(rf"^{_PARTICLES_RE}[{_UC}][{_UC}{_LC}'’\-]+,\s+[A-Z]\.?(?:\s?-?[A-Z]\.?)*,?")
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
    # all DOIs in the item; prefer one introduced by 'doi:' or doi.org over one buried in a publisher URL
    dois = [clean_doi(x.group(0)) for x in DOI_RE.finditer(text)]
    dois = [d for d in dict.fromkeys(dois) if d]
    if dois:
        explicit = re.search(r"(?:doi\s*:?\s*|doi\.org/)(" + DOI_RE.pattern + r")", text, flags=re.I)
        ref.doi = clean_doi(explicit.group(1)) if explicit else dois[0]
        ref.extra_dois = [d for d in dois if d != ref.doi]
    pm = PMID_RE.search(text) or PUBMED_URL_RE.search(text)
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
    body = re.sub(r"^\s*(?:et\s+al\.?|\.{3})\s*", "", body)      # a list that starts with 'et al.' has lost its authors

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
        gm = None if m else re.match(r"^(?P<group>[A-Z][\w&'’\- ]{3,120}?(?:Group|Investigators|Committee|Collaborat\w+|Consortium|Organization|Organisation|Society|Association|Institute|College|Agency|Excellence)\b\.?)\s*[.;]?\s+(?P<rest>[A-Z].*)$", body)
        if gm:                                   # organisation or trial group as the author
            ref.authors = [gm.group("group").rstrip(" .")]
            rest = gm.group("rest")
        elif m:
            authors_part, rest = m.group("authors"), m.group("rest")
            for chunk in re.split(r",\s*|;\s*|\s+and\s+|\s*&\s*", authors_part):
                chunk = chunk.strip(" .")
                if not chunk or chunk.lower().startswith("et al") or re.search(r"\b(Group|Investigators|Committee|Consortium|Collaborat)", chunk):
                    continue
                ref.authors.append(surname_from_display(chunk))
        else:
            rest = body
            # a leading segment with commas/initials that the author regex rejected (all caps, odd characters):
            # remember that an author block existed so the verifier does not quietly skip the author check
            lead = body.split(". ", 1)[0]
            if len(lead) <= 250 and (re.search(r"\b[A-Z]{1,3}\b", lead) and ("," in lead or " and " in lead)) and not DOI_RE.search(lead):
                ref.author_parse_failed = True
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
    # Web pages and organisation documents (WHO fact sheet, guideline landing page): URL, no identifiers,
    # no journal. These are not journal articles and must not be matched to one by title.
    if ref.url and not ref.doi and not ref.pmid and not ref.journal and not PUBMED_URL_RE.search(ref.url) and "doi.org" not in ref.url.lower():
        ref.is_web_resource = True
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
    used: dict[str, int] = {}
    for i, (label, txt) in enumerate(items, 1):
        rid = f"ref_{label}" if label else f"ref_{i}"
        if rid in used:                      # duplicate labels must not collide (evidence/claims map by ref_id)
            used[rid] += 1
            rid = f"{rid}_{used[rid]}"
        else:
            used[rid] = 1
        ref = parse_free_citation(txt, rid)
        # an item holding two DOIs or two year-tails is usually two references merged by the splitter
        tails = len(re.findall(r"(?:19|20)\d{2}\s*;\s*\d", txt)) + len(APA_YEAR_RE.findall(txt))
        if len(ref.extra_dois) >= 1 and tails >= 2 or tails >= 3 or len(txt) > 700:
            ref.parse_notes.append("this item may contain more than one reference merged together; check the list formatting (one reference per line or per numbered item)")
        refs.append(ref)
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
    NEGATIVE_TTL = 24 * 3600     # 'not found' answers go stale faster: new DOIs, new retractions

    def __init__(self, email: str = "", timeout: int = 20, cache_path: Optional[Path] = None, ttl_days: int = 7, quiet: bool = False):
        self.email = email or DEFAULT_EMAIL
        self.timeout = timeout
        self.cache_path = cache_path
        self.ttl = ttl_days * 86400
        self.quiet = quiet
        self.last_call: dict[str, float] = {}
        self.intervals = dict(HOST_INTERVAL)
        if not self.email:
            self.intervals["api.crossref.org"] = 0.25   # CrossRef public pool is 5 requests/s; the polite pool needs a mailto
        self.cache: dict[str, dict] = {}
        self.calls = 0            # HTTP attempts, including retries
        self.gets = 0             # completed get() requests (cache hits excluded)
        self.cache_hits = 0
        self.transport_failures = 0
        if cache_path and cache_path.exists():
            try:
                loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                loaded = {}
            now = time.time()
            for k, ent in (loaded.items() if isinstance(loaded, dict) else []):
                if not isinstance(ent, dict) or not isinstance(ent.get("ts"), (int, float)) or ent["ts"] > now + 300:
                    continue   # malformed or future-dated entries are not trusted
                ent["final_url"] = redact_url(ent.get("final_url", ""))   # scrub caches written by older versions
                self.cache[redact_url(k)] = ent

    def user_agent_for(self, host: str) -> str:
        """The contact address goes only to CrossRef, which reads it from the User-Agent. NCBI and OpenAlex
        receive it as a query parameter; every other host (doi.org, Europe PMC, publishers) gets none."""
        base = f"{TOOL_NAME}/{__version__} (https://github.com/TW5897/citation-guardrails)"
        if host == "api.crossref.org" and self.email:
            return f"{base[:-1]}; mailto:{self.email})"
        return base

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
        gap = self.intervals.get(host, 0.3)
        last = self.last_call.get(host, 0.0)
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        self.last_call[host] = time.monotonic()

    def get(self, url: str, method: str = "GET", accept: str = "", follow_redirects: bool = True, use_cache: bool = True) -> tuple[int, str, str]:
        """Returns (status, body, final_url). status 0 = transport failure; 3xx is returned as-is when
        follow_redirects is False (final_url then holds the Location header, which is never fetched).
        Caches 200 for ttl days and 404/410 for 24 h. Cached URLs are redacted (no email, no key)."""
        key = f"{method} {redact_url(url)}"
        now = time.time()
        ent = self.cache.get(key) if use_cache else None
        if ent:
            age = now - ent.get("ts", 0)
            ttl = self.NEGATIVE_TTL if ent.get("status") in (404, 410) else self.ttl
            if 0 <= age < ttl:
                self.cache_hits += 1
                return ent["status"], ent.get("body", ""), ent.get("final_url", redact_url(url))
        host = urllib.parse.urlparse(url).netloc
        headers = {"User-Agent": self.user_agent_for(host)}
        if accept:
            headers["Accept"] = accept
        opener = urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(_NoRedirect)
        delay = 1.0
        status, body, final_url = 0, "", redact_url(url)
        for attempt in range(4):
            self._throttle(host)
            self.calls += 1
            req = urllib.request.Request(url, headers=headers, method=method)
            try:
                with opener.open(req, timeout=self.timeout) as resp:
                    status = resp.status
                    final_url = redact_url(resp.geturl())
                    body = "" if method == "HEAD" else resp.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as e:
                status = e.code
                loc = e.headers.get("Location") if e.headers else None
                final_url = redact_url(loc or e.geturl() or url)
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
                if attempt < 1:            # one retry for transport failures; a dead network should fail fast
                    time.sleep(delay)
                    continue
                break
        self.gets += 1
        if status == 0:
            self.transport_failures += 1
        if use_cache and status in (200, 404, 410) and len(body) < 2_000_000:
            self.cache[key] = {"status": status, "body": body, "final_url": final_url, "ts": now, "v": __version__}
        return status, body, final_url

    def get_json(self, url: str) -> tuple[int, Any]:
        status, body, _ = self.get(url, accept="application/json")
        if status != 200:
            return status, None
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def openalex_params(http: Http) -> str:
    """mailto / api_key query suffix for OpenAlex ('&k=v' pairs, without the leading '?' or '&')."""
    parts = []
    if http.email:
        parts.append("mailto=" + urllib.parse.quote(http.email))
    if OPENALEX_API_KEY:
        parts.append("api_key=" + urllib.parse.quote(OPENALEX_API_KEY))
    return "&".join(parts)


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
    # 'updated-by' = notices that update THIS work (the authoritative direction; Retraction Watch entries live here).
    for u in m.get("updated-by") or []:
        t = (u.get("type") or "").lower()
        src = u.get("source") or "publisher"
        label = f"{u.get('label') or t} (CrossRef updated-by, source {src}, DOI {u.get('DOI')})"
        if t in ("retraction", "withdrawal", "removal"):
            h.retracted = True
            h.retraction_notes.append(label)
        elif "concern" in t:
            h.concern = True
            h.retraction_notes.append(label)
        elif t in ("correction", "erratum", "corrigendum", "new_edition"):
            h.correction = True
    # 'update-to' = this record updates another work, i.e. it is itself a notice... except that some
    # publishers deposit it on the retracted article. Decide from the title: a notice title says so.
    update_to = m.get("update-to") or []
    is_notice_title = bool(NOTICE_PREFIX_RE.match(h.title))
    if update_to and (is_notice_title or h.work_type in ("journal-article",) and not RETRACTED_LABEL_RE.match(h.title) and all((u.get("type") or "").lower() in ("correction", "erratum", "corrigendum") for u in update_to)):
        if is_notice_title or not RETRACTED_LABEL_RE.match(h.title):
            h.notice_type = (update_to[0].get("type") or "notice").lower()
            h.notice_for = clean_doi(update_to[0].get("DOI") or "")
    elif is_notice_title:
        h.notice_type = NOTICE_PREFIX_RE.match(h.title).group(0).strip(" :-–").lower()
    for u in update_to:
        t = (u.get("type") or "").lower()
        if t in ("retraction", "withdrawal", "removal") and not h.notice_type:
            h.weak_retraction = True
            h.retraction_notes.append(f"{u.get('label') or t} (CrossRef update-to, DOI {u.get('DOI')})")
        elif "concern" in t and not h.notice_type:
            h.concern = True
    if RETRACTED_LABEL_RE.match(h.title):
        h.retracted = True
        h.retraction_notes.append("title carries the publisher's RETRACTED/WITHDRAWN label (CrossRef)")
    if h.weak_retraction and h.retracted:
        h.weak_retraction = False
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
    """Registry check only: doi.org answers a registered DOI with a redirect. The redirect is NOT followed,
    so no publisher site is contacted and the user's IP and query never leave doi.org."""
    url = "https://doi.org/" + urllib.parse.quote(doi, safe="/()<>;:")
    h = _hit("doi.org", f"doi:{doi}", url)
    status, _, location = http.get(url, method="HEAD", follow_redirects=False)
    if status == 0:
        h.ok, h.error = False, "transport failure"
    elif status in (404, 410):
        h.found = False
    elif 200 <= status < 400:
        h.found = True
        h.doi = doi
        target_host = urllib.parse.urlparse(location).netloc if location else ""
        h.url = url + (f"  (redirects to {target_host})" if target_host and target_host != "doi.org" else "")
    else:
        h.ok, h.error = False, f"HTTP {status}"
    return h


def datacite_by_doi(http: Http, doi: str) -> Hit:
    """DataCite registers Zenodo, arXiv (10.48550), figshare, Dryad and most dataset/preprint DOIs that CrossRef lacks."""
    url = "https://api.datacite.org/dois/" + urllib.parse.quote(doi, safe="")
    h = _hit("datacite", f"doi:{doi}", url)
    status, d = http.get_json(url)
    if status == 404:
        return h
    if status != 200 or not d:
        h.ok, h.error = False, f"HTTP {status}"
        return h
    a = ((d.get("data") or {}).get("attributes") or {})
    h.found = True
    titles = a.get("titles") or []
    h.title = squash(html.unescape((titles[0] or {}).get("title") or "")) if titles else ""
    for c in a.get("creators") or []:
        fam = squash(c.get("familyName") or "") or surname_from_display(c.get("name") or "")
        giv = squash(c.get("givenName") or "")
        if fam:
            h.authors.append(fam)
            h.authors_full.append(fam + (f", {giv}" if giv else ""))
    h.first_author = h.authors[0] if h.authors else ""
    h.year = str(a.get("publicationYear") or "")
    h.journal = squash(((a.get("container") or {}).get("title")) or a.get("publisher") or "")
    h.doi = clean_doi(a.get("doi") or doi)
    types = a.get("types") or {}
    h.work_type = squash(types.get("resourceTypeGeneral") or types.get("resourceType") or "")
    if "preprint" in (str(types.get("resourceType") or "") + str(types.get("resourceTypeGeneral") or "")).lower():
        h.preprint = True
    _mark_preprint(h)
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
        book = root.find(".//PubmedBookArticle")
        if book is not None:
            _fill_from_pubmed_book(h, book, want_abstract)
            return h
        h.found = False
        return h
    _fill_from_pubmed_xml(h, art, want_abstract)
    return h


def _fill_from_pubmed_book(h: Hit, art: ET.Element, want_abstract: bool) -> None:
    """Bookshelf records (StatPearls, GeneReviews, NICE guidance on Bookshelf) come back as PubmedBookArticle."""
    h.found = True
    bd = art.find("BookDocument")
    if bd is None:
        return
    h.pmid = bd.findtext("PMID") or ""
    book = bd.find("Book")
    book_title = squash("".join(book.find("BookTitle").itertext())) if book is not None and book.find("BookTitle") is not None else ""
    art_title = squash("".join(bd.find("ArticleTitle").itertext())) if bd.find("ArticleTitle") is not None else ""
    h.title = art_title or book_title
    h.journal = book_title if art_title else squash((book.findtext("Publisher/PublisherName") if book is not None else "") or "")
    for al in list(bd.findall("AuthorList")) + (list(book.findall("AuthorList")) if book is not None else []):
        for au in al.findall("Author"):
            last = au.findtext("LastName")
            if last:
                h.authors.append(squash(last))
                h.authors_full.append(squash(last) + (f", {squash(au.findtext('ForeName') or au.findtext('Initials') or '')}" if (au.findtext("ForeName") or au.findtext("Initials")) else ""))
        if h.authors:
            break
    h.first_author = h.authors[0] if h.authors else ""
    if book is not None:
        h.year = book.findtext("PubDate/Year") or (book.findtext("PubDate/MedlineDate") or "")[:4]
    h.work_type = "Book (PubMed Bookshelf)"
    for i in art.findall(".//PubmedBookData/ArticleIdList/ArticleId"):
        if i.get("IdType") == "doi" and i.text:
            h.doi = clean_doi(i.text)
    if want_abstract:
        h.abstract = "\n".join(squash("".join(p.itertext())) for p in bd.findall("Abstract/AbstractText"))


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
    if RETRACTED_LABEL_RE.match(h.title):
        h.retracted = True
        h.retraction_notes.append("title carries the publisher's RETRACTED label (PubMed)")
    if NOTICE_PREFIX_RE.match(h.title) or any(p in ("Published Erratum", "Retraction of Publication") for p in ptypes):
        h.notice_type = "retraction notice" if "Retraction of Publication" in ptypes else "correction/erratum"
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
    fetched = pubmed_fetch_many(http, ids)
    if not fetched:                       # esearch found records but efetch failed: the search is NOT a clean negative
        h.ok, h.error = False, "efetch failed after esearch returned candidates"
        return h
    for ph in fetched:
        h.candidates.append({"source": "pubmed", "pmid": ph.pmid, "doi": ph.doi, "title": ph.title, "first_author": ph.first_author, "year": ph.year, "journal": ph.journal_abbrev or ph.journal, "retracted": ph.retracted, "preprint": ph.preprint, "pmcid": ph.pmcid, "type": ph.work_type, "volume": ph.volume, "pages": ph.pages})
    return h


def openalex_work(http: Http, ident: str) -> Hit:
    """ident is 'doi:<doi>' or 'pmid:<pmid>'."""
    kind, val = ident.split(":", 1)
    path = f"https://doi.org/{val}" if kind == "doi" else f"pmid:{val}"
    extra = openalex_params(http)
    url = "https://api.openalex.org/works/" + urllib.parse.quote(path, safe=":/") + (f"?{extra}" if extra else "")
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
    extra = openalex_params(http)
    url = "https://api.openalex.org/works?filter=title.search:" + urllib.parse.quote(q) + "&per-page=5&select=id,doi,title,publication_year,ids,authorships,primary_location,type,is_retracted,biblio" + (f"&{extra}" if extra else "")
    h = _hit("openalex", f"title:{title[:80]}", url)
    status, d = http.get_json(url)
    if status != 200 or d is None:
        h.ok, h.error = False, f"HTTP {status}"
        return h
    for w in d.get("results") or []:
        c = Hit(source="openalex", query="")
        _fill_from_openalex(c, w)
        h.candidates.append({"source": "openalex", "pmid": c.pmid, "doi": c.doi, "title": c.title, "first_author": c.first_author, "year": c.year, "journal": c.journal, "retracted": c.retracted, "preprint": c.preprint, "pmcid": c.pmcid, "type": c.work_type, "volume": c.volume, "pages": c.pages})
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
        h.candidates.append({"source": "europepmc", "pmid": c.pmid, "doi": c.doi, "title": c.title, "first_author": c.first_author, "year": c.year, "journal": c.journal, "retracted": c.retracted, "preprint": c.preprint, "pmcid": c.pmcid, "abstract": c.abstract, "type": c.work_type, "volume": c.volume, "pages": c.pages})
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
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode({"query.bibliographic": q, "rows": "5", "select": "DOI,title,author,issued,container-title,type,score,volume,page"})
    h = _hit("crossref", f"title:{ref.title[:80]}", url)
    status, d = http.get_json(url)
    if status != 200 or not d or d.get("status") != "ok":
        h.ok, h.error = False, f"HTTP {status}"
        return h
    for it in (d.get("message") or {}).get("items") or []:
        if it.get("type") in ("journal", "journal-issue", "journal-volume", "book-series", "report-series"):
            continue   # container-level records are never a citable paper
        fam = ((it.get("author") or [{}])[0]).get("family") or ""
        parts = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
        h.candidates.append({
            "source": "crossref", "doi": clean_doi(it.get("DOI") or ""), "pmid": "",
            "title": squash(strip_tags(html.unescape(" ".join(it.get("title") or [])))),
            "first_author": fam, "year": str(parts[0]) if parts and parts[0] else "",
            "journal": squash(" ".join(it.get("container-title") or [])), "retracted": False,
            "preprint": it.get("type") == "posted-content", "pmcid": "", "type": it.get("type") or "",
            "volume": str(it.get("volume") or ""), "pages": str(it.get("page") or ""),
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
        cr: Optional[Hit] = None
        if "crossref" in self.sources:
            cr = crossref_by_doi(self.http, doi)
            hits.append(cr)
        if "datacite" in self.sources and (cr is None or (cr.ok and not cr.found) or doi_prefix(doi) in ("10.5281", "10.48550", "10.6084", "10.5061", "10.17605")):
            hits.append(datacite_by_doi(self.http, doi))
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
                elif len(years) == 1 and (h.preprint or h.source in ("openalex", "semanticscholar", "datacite")):
                    # one secondary index disagreeing about the year of a preprint/dataset is not proof of a wrong citation
                    checks.append(Check("year", ref.year, str(y), h.source, "warning", "single secondary source disagrees"))
                    warnings.append(f"year differs from the only source that answered ({h.source}: {y} vs cited {cy}) (single source)")
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
        notes, issues = res.notes, res.issues
        notes.extend(ref.parse_notes)
        all_hits: list[Hit] = []

        # A web page or organisation document is not a journal article: never match it to one by title.
        if ref.is_web_resource and not ref.doi and not ref.pmid:
            res.status, res.confidence = "UNVERIFIABLE", "low"
            res.flags.append("WEB_RESOURCE")
            issues.append("web page / organisation document (URL, no DOI, PMID or journal): not checkable against bibliographic databases; open the URL and confirm it says what is claimed")
            return res

        def has_meta(h: Hit) -> bool:
            return h.source != "doi.org" and bool(h.title or h.first_author or h.year)

        doi_hits = self.lookup_doi(ref.doi) if ref.doi else []
        pmid_hits = self.lookup_pmid(ref.pmid) if ref.pmid else []
        all_hits += doi_hits + pmid_hits

        doi_found = [h for h in doi_hits if h.found]
        doi_ok = [h for h in doi_hits if h.ok]
        doiorg = next((h for h in doi_hits if h.source == "doi.org"), None)
        crossref = next((h for h in doi_hits if h.source == "crossref"), None)
        datacite = next((h for h in doi_hits if h.source == "datacite"), None)
        pmid_found = [h for h in pmid_hits if h.found]
        pmid_ok = [h for h in pmid_hits if h.ok]

        # doi.org is the registry of record for every agency. A DOI that doi.org does not know and that neither
        # CrossRef nor DataCite holds does not exist, whatever a secondary index (OpenAlex) returns for it.
        registry_missing = (doiorg is not None and doiorg.ok and not doiorg.found
                            and (crossref is None or (crossref.ok and not crossref.found))
                            and (datacite is None or (datacite.ok and not datacite.found)))
        if registry_missing and any(has_meta(h) for h in doi_found):
            notes.append("a secondary index (" + ", ".join(h.source for h in doi_found if has_meta(h)) + ") returns a record for this DOI, but doi.org, CrossRef and DataCite do not: the DOI is not registered")
            doi_found = []
        doi_missing = bool(ref.doi) and not doi_found and (registry_missing or len(doi_ok) >= 1)
        pmid_missing = bool(ref.pmid) and not pmid_found and len(pmid_ok) >= 1
        doi_unknown = bool(ref.doi) and not doi_found and not doi_ok
        pmid_unknown = bool(ref.pmid) and not pmid_found and not pmid_ok

        # Enrich a DOI-only citation with its PubMed record (retraction markers live there)
        doi_meta = [h for h in doi_found if has_meta(h)]
        if doi_meta and not ref.pmid and "pubmed" in self.sources:
            pm = next((h.pmid for h in doi_meta if h.pmid), "")
            if pm:
                ph = pubmed_by_pmid(self.http, pm)
                all_hits.append(ph)
                if ph.found and (not ph.doi or ph.doi == ref.doi):
                    doi_found.append(ph)
                    doi_meta.append(ph)

        found = doi_found + pmid_found
        meta_found = [h for h in found if has_meta(h)]
        res.found_by = "doi" if doi_meta else ("pmid" if pmid_found else "")

        # DOI and PMID must point at the same work
        if doi_meta and pmid_found:
            same = any(
                (dh.pmid and ph.pmid and dh.pmid == ph.pmid) or (dh.doi and ph.doi and dh.doi == ph.doi)
                or (dh.title and ph.title and title_similarity(dh.title, ph.title) >= TITLE_MATCH)
                for dh in doi_meta for ph in pmid_found)
            if not same:
                dt = next((h.title for h in doi_meta if h.title), "")
                pt = next((h.title for h in pmid_found if h.title), "")
                issues.append(f"DOI and PMID point to different works: DOI -> \"{dt[:90]}\"; PMID -> \"{pt[:90]}\"")
                res.flags.append("IDENTIFIER_CONFLICT")

        if ref.pmcid and meta_found:
            rec_pmc = next((h.pmcid for h in meta_found if h.pmcid), "")
            if rec_pmc and rec_pmc.upper() != ref.pmcid.upper():
                res.flags.append("PMCID_MISMATCH")
                issues.append(f"cited PMCID {ref.pmcid} is not this paper's PMCID ({rec_pmc})")

        # Title-only path
        title_hits: list[Hit] = []
        if not meta_found and not ref.doi and not ref.pmid:
            if ref.title and len(ref.title.split()) >= 3:
                title_hits = self.lookup_title(ref)
                all_hits += title_hits
                accepted = self._accept_title_candidate(ref, title_hits, res)
                if accepted:
                    found, meta_found = [accepted], [accepted]
                    res.found_by = "title"
                    res.flags.append("TITLE_MATCHED")
                    corroborated = getattr(accepted, "_corroborated", []) or []
                    notes.append("no DOI/PMID in the citation; matched by title search and confirmed by " + ("/".join(corroborated) if corroborated else "title alone"))
            else:
                notes.append("no DOI, PMID or usable title could be parsed from this citation")
        elif not meta_found and (ref.doi or ref.pmid) and ref.title and len(ref.title.split()) >= 3 and (doi_missing or pmid_missing):
            # identifiers are dead; see whether the title exists elsewhere so the user gets the right fix
            title_hits = self.lookup_title(ref)
            all_hits += title_hits
            cand = self._best_title_candidate(ref, title_hits)
            if cand and cand[0] >= TITLE_MATCH:
                c = cand[1]
                notes.append(f"a work with this title does exist ({c.get('source')}: DOI {c.get('doi') or '-'}, PMID {c.get('pmid') or '-'}); the cited identifier is wrong")
                res.flags.append("IDENTIFIER_WRONG_BUT_TITLE_EXISTS")

        # Field checks: only records that carry metadata count; a bare doi.org redirect compares nothing
        checks, major, warnings = self.field_checks(ref, meta_found) if meta_found else ([], [], [])
        res.checks = checks
        if meta_found and ref.author_parse_failed and not ref.first_author:
            warnings.append("the author list could not be parsed from the citation, so the first author was NOT checked; compare it by hand")
            res.flags.append("AUTHOR_NOT_CHECKED")
        if any("(single source)" in w for w in warnings):
            res.flags.append("SINGLE_SOURCE_CONFLICT")
        if ref.extra_dois:
            res.flags.append("MULTIPLE_DOIS")
            notes.append("further DOI(s) in the same item were not checked: " + ", ".join(ref.extra_dois))

        # Notices and post-publication events
        notice = next((h for h in meta_found if h.notice_type), None)
        if notice and not NOTICE_PREFIX_RE.match(ref.title or ""):
            major.append(f"the cited DOI is a {notice.notice_type} notice" + (f" for DOI {notice.notice_for}" if notice.notice_for else "") + ", not the article itself; cite the original article")
            res.flags.append("NOTICE_DOI_CITED")
        strong = [h for h in meta_found if h.retracted]
        weak = [h for h in meta_found if h.weak_retraction]
        retracted = bool(strong) or len(weak) >= 2
        if weak and not retracted:
            res.flags.append("RETRACTION_HINT")
            notes.append("CrossRef lists a retraction link for this DOI that no other source confirms; check the publisher page before citing")
        concern = any(h.concern for h in meta_found)
        correction = any(h.correction for h in meta_found)
        preprint = any(h.preprint for h in meta_found)
        vor = next((h.vor_doi for h in meta_found if h.vor_doi), "")
        if retracted:
            res.flags.append("RETRACTED")
            for h in meta_found:
                for n in h.retraction_notes:
                    if n not in notes:
                        notes.append(n)
        if concern:
            res.flags.append("EXPRESSION_OF_CONCERN")
        if correction:
            res.flags.append("CORRECTION_PUBLISHED")
        if preprint:
            res.flags.append("PREPRINT")
            server = next((h.preprint_server for h in meta_found if h.preprint_server), "")
            notes.append(f"this is a preprint{(' (' + server + ')') if server else ''}; it has not been peer reviewed" + (f"; a published version exists: DOI {vor}" if vor else ""))
            if vor:
                res.flags.append("PUBLISHED_VERSION_EXISTS")

        # Consensus record and evidence
        res.resolved = self._consensus(meta_found)
        res.sources_confirming = sorted({h.source for h in meta_found})
        res.hits = [asdict(h) for h in all_hits]
        for h in res.hits:
            h.pop("abstract", None)
            h["candidates"] = h.get("candidates", [])[:5]
            for c in h["candidates"]:
                c.pop("abstract", None)

        # Status
        if doi_missing:
            issues.append(f"DOI {ref.doi} is not registered (doi.org" + (", CrossRef" if crossref else "") + (", DataCite" if datacite else "") + ")")
        if pmid_missing:
            issues.append(f"PMID {ref.pmid} does not exist in PubMed")
        if doi_unknown:
            issues.append(f"DOI {ref.doi} could not be checked (every source failed)")
        if pmid_unknown:
            issues.append(f"PMID {ref.pmid} could not be checked (every source failed)")
        issues.extend(major)

        if not meta_found:
            if found:
                res.status = "UNVERIFIABLE"
                res.flags.append("DOI_RESOLVES_NO_METADATA")
                issues.append("the DOI resolves at doi.org but no database returned a bibliographic record, so the cited title, author and year were NOT compared; open the DOI and check it by hand")
            elif (ref.doi or ref.pmid) and (doi_missing or pmid_missing) and not (doi_unknown or pmid_unknown):
                res.status = "NOT_FOUND"
            elif "TITLE_ONLY_UNCONFIRMED" in res.flags:
                res.status = "UNVERIFIABLE"
            elif title_hits:
                responded = [h for h in title_hits if h.ok]
                errored = [h for h in title_hits if not h.ok]
                if len(responded) >= 3 or (len(responded) >= 2 and not errored):
                    res.status = "NOT_FOUND"
                    if not any(h.candidates for h in responded):
                        issues.append("no work with this title in " + ", ".join(sorted({h.source for h in responded})))
                else:
                    res.status = "UNVERIFIABLE"
                    issues.append(f"title search incomplete: {len(errored)} of {len(title_hits)} sources failed (rate limit or network); re-run later")
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

        # Confidence = how many independent databases returned a record (doi.org never counts)
        n_src = len(res.sources_confirming)
        if res.status in ("VERIFIED", "VERIFIED_WITH_WARNINGS", "RETRACTED", "MISMATCH"):
            res.confidence = "high" if n_src >= 2 else ("medium" if n_src == 1 else "low")
            if res.found_by == "title" and res.confidence == "high":
                res.confidence = "medium"
        elif res.status == "NOT_FOUND":
            answered = len({h.source for h in all_hits if h.ok and h.source != "doi.org"})
            res.confidence = "high" if answered >= 2 else ("medium" if answered == 1 else "low")
        else:
            res.confidence = "low"
        if meta_found and ref.parse_confidence == "low" and res.found_by != "title" and not ref.title and not ref.first_author:
            notes.append("few cited fields could be parsed, so only the identifier was checked")
            if len(ref.raw.split()) > 4:
                res.flags.append("FIELDS_NOT_CHECKED")
                if res.status == "VERIFIED":
                    res.status = "VERIFIED_WITH_WARNINGS"
                    issues.append("the citation text could not be parsed into title/author/year, so only the identifier was verified")
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
        letter_like = re.compile(r"letter|comment|editorial|erratum|correction|reply|news", re.I)
        cited_first_page = re.sub(r"[^\w]", "", (ref.pages or "").split("-")[0].split("–")[0]).lower()
        for c in seen.values():
            if "(issn)" in (c.get("doi") or "") or (c.get("type") or "") in ("journal", "journal-issue", "journal-volume"):
                continue   # a journal's own record, not a paper
            s = title_similarity(ref.title, c.get("title") or "")
            both_authors = bool(ref.first_author and c.get("first_author"))
            author_conf = both_authors and surnames_match(ref.first_author, c["first_author"])
            author_bad = both_authors and not author_conf
            cy, sy = year_int(ref.year), year_int(c.get("year", ""))
            year_conf = cy is not None and sy is not None and abs(cy - sy) <= YEAR_WARN
            year_bad = cy is not None and sy is not None and abs(cy - sy) > YEAR_WARN
            # a record whose volume AND first page both differ from the citation is a different work
            # (the letter that reuses a trial's title), not a typo
            cand_first_page = re.sub(r"[^\w]", "", (c.get("pages") or "").split("-")[0]).lower()
            vol_page_bad = bool(ref.volume and c.get("volume") and ref.volume.strip() != str(c["volume"]).strip()
                                and cited_first_page and cand_first_page and cited_first_page != cand_first_page)
            is_letter = bool(letter_like.search(c.get("type") or "")) and not letter_like.search(ref.title or "")
            journal_ok = bool(ref.journal) and bool(c.get("journal")) and journals_match(ref.journal, [c["journal"]])
            if author_bad or year_bad or vol_page_bad or (is_letter and not journal_ok and (ref.volume or ref.pages)):
                scored.append((s, False, False, journal_ok, dict(c, _reject=f"author_bad={author_bad} year_bad={year_bad} vol_page_bad={vol_page_bad} letter={is_letter}")))
                continue
            scored.append((s, author_conf, year_conf, journal_ok, c))
        # best title similarity first; among ties prefer author+year agreement, then journal agreement
        scored.sort(key=lambda t: (-t[0], not (t[1] and t[2]), not t[3]))
        if scored:
            res.notes.append("title candidates: " + "; ".join(f"{c.get('source')}:{s}:{(c.get('title') or '')[:60]}" for s, _, _, _, c in scored[:3]))
        strong = [t for t in scored if t[0] >= TITLE_MATCH]
        # Positive corroboration is required: the cited author or the cited year must agree with the record.
        accepted = [t for t in strong if "_reject" not in t[4] and (t[1] or t[2])]
        if strong and not accepted and not ref.first_author and not year_int(ref.year):
            res.flags.append("TITLE_ONLY_UNCONFIRMED")
            res.issues.append("a work with this title exists, but the citation gives no author or year to confirm it is the one meant; add the DOI or PMID. Candidates: "
                              + "; ".join(f"{(t[4].get('first_author') or '?')} {t[4].get('year')} {(t[4].get('journal') or '')[:40]} DOI {t[4].get('doi') or '-'}" for t in strong[:3]))
            return None
        if accepted:
            for t in accepted:
                t[4]["_corroborated"] = [k for k, ok in (("author", t[1]), ("year", t[2]), ("journal", t[3])) if ok]
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
            corroborated = c.get("_corroborated", [])
            # Promote the candidate to a full record via its identifiers for a proper cross-check
            h: Optional[Hit] = None
            if c.get("pmid") and "pubmed" in self.sources:
                ph = pubmed_by_pmid(self.http, c["pmid"])
                if ph.found:
                    h = ph
            if h is None and c.get("doi") and "crossref" in self.sources:
                ch = crossref_by_doi(self.http, c["doi"])
                if ch.found:
                    h = ch
            if h is None:
                h = Hit(source=c.get("source", "title-search"), query=f"title:{ref.title[:80]}", found=True, title=c.get("title", ""), first_author=c.get("first_author", ""), authors=[c.get("first_author", "")], year=c.get("year", ""), journal=c.get("journal", ""), volume=str(c.get("volume") or ""), pages=str(c.get("pages") or ""), doi=c.get("doi", ""), pmid=c.get("pmid", ""), pmcid=c.get("pmcid", ""), retracted=bool(c.get("retracted")), preprint=bool(c.get("preprint")), work_type=c.get("type", ""), retrieved=utcnow())
            setattr(h, "_corroborated", corroborated)
            return h
        if strong:
            s, _, _, _, c = strong[0]
            res.flags.append("TITLE_EXISTS_AUTHOR_YEAR_DIFFER")
            res.issues.append(f"a paper with this title exists but the cited author/year/volume do not match it (source: {c.get('first_author')} {c.get('year')}, {c.get('journal') or ''}, DOI {c.get('doi') or '-'})")
        return None

    @staticmethod
    def _consensus(found: list[Hit]) -> dict:
        if not found:
            return {}
        order = {"pubmed": 0, "crossref": 1, "datacite": 2, "openalex": 3, "europepmc": 4, "semanticscholar": 5, "doi.org": 9}
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
    unchecked = [r.ref_id for r in results if r.status == "UNVERIFIABLE"]
    review = [r.ref_id for r in results if r.status in REVIEW or any(f in r.flags for f in ("PREPRINT", "MULTIPLE_MATCHES", "TITLE_MATCHED", "RETRACTION_HINT", "AUTHOR_NOT_CHECKED"))]
    checked = len(results) - len(unchecked)
    sources = opts.get("sources", list(DEFAULT_SOURCES))
    offline = bool(opts.get("offline"))
    if offline:
        method = f"Offline run on {today()}: {len(results)} references were parsed but NO database was queried. Nothing has been verified."
    elif checked == 0:
        method = f"On {today()} none of the {len(results)} references could be checked (every lookup failed: no network, proxy, or rate limit). Nothing has been verified."
    else:
        method = (f"{checked} of {len(results)} references were checked on {today()} with citecheck v{__version__} against "
                  + ", ".join(sources) + " (identifier resolution, then cited title, first author, year and journal compared with the source record; retraction and preprint status flagged)."
                  + (f" {len(unchecked)} could not be checked and need manual verification." if unchecked else "")
                  + (f" {opts['cache_hits']} responses came from the local cache (up to 7 days old); use --no-cache for a fresh run." if opts.get("cache_hits") else ""))
    return {
        "tool": TOOL_NAME, "version": __version__, "generated": utcnow(), "source": source,
        "sources_queried": sources, "offline": offline,
        "total_references": len(results), "checked": checked, "unchecked": len(unchecked), "counts": counts, "flags": flags,
        "blocking_ref_ids": blocking, "unchecked_ref_ids": unchecked, "needs_review_ref_ids": review,
        # safe = nothing blocking AND every reference was actually checked
        "submission_safe": not blocking and not unchecked and checked > 0,
        "fully_verified": all(r.status == "VERIFIED" for r in results) and bool(results),
        "method_statement": method,
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
    if audit.get("unchecked"):
        verdict = f"NOT CHECKED: {audit['unchecked']} of {audit['total_references']} references could not be verified" + (" (offline run)" if audit.get("offline") else "") + "; the others " + ("have no blocking problems" if not audit["blocking_ref_ids"] else "include blocking problems")
    else:
        verdict = "yes" if audit["submission_safe"] else "NO"
    lines += ["", f"**Submission safe (every reference checked; no NOT_FOUND / MISMATCH / RETRACTED):** {verdict}", ""]
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
    print(f"submission_safe={audit['submission_safe']}  blocking={audit['blocking_ref_ids']}  unchecked={audit.get('unchecked_ref_ids', [])}  needs_review={audit['needs_review_ref_ids']}")
    if audit.get("unchecked"):
        print(f"WARNING: {audit['unchecked']} reference(s) were NOT checked" + (" (offline run)" if audit.get("offline") else " (network failure or rate limit; re-run, or use --no-cache)") + ". Nothing unchecked is verified.")


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
    ev["licence"] = ""
    if want_fulltext and pmcid:
        url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
        status, body, _ = http.get(url, use_cache=False)      # publishers' full text is never written to the cache
        if status == 200 and body.strip():
            txt = jats_to_text(body)
            if len(txt) > 500:
                ev["fulltext"] = txt
                ev["tier"] = "full_text"
                ev["sources"].append(url)
                ev["licence"] = jats_licence(body)
    ev["pmcid"] = pmcid
    return ev


def jats_licence(xml: str) -> str:
    """The licence statement of a JATS article (CC BY, CC BY-NC-ND ...), kept with the text it covers."""
    m = re.search(r"<license[^>]*>(.*?)</license>", xml, flags=re.S | re.I)
    txt = squash(strip_tags(html.unescape(m.group(1)))) if m else ""
    href = re.search(r'(?:xlink:href|href)="(https?://creativecommons\.org/[^"]+)"', xml)
    ref = re.search(r"<ali:license_ref[^>]*>([^<]+)</ali:license_ref>", xml, flags=re.I)
    url = (href.group(1) if href else "") or (ref.group(1).strip() if ref else "")
    if not txt and not url:
        return "not stated in the XML; assume all rights reserved by the publisher"
    return (txt[:300] + (" " + url if url and url not in txt else "")).strip()


def write_evidence_pack(out_dir: Path, ref_id: str, rec: dict, ev: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-.]+", "_", ref_id)[:80]
    p = out_dir / f"{safe}.md"
    lines = [f"# Evidence pack - {ref_id}", "",
             f"- Source record: {vancouver(rec)}",
             f"- Evidence tier available: **{ev['tier']}**" + (" (full text via Europe PMC open-access XML)" if ev["tier"] == "full_text" else " (abstract only: claims about subgroups, secondary endpoints or methods detail cannot be confirmed from this)" if ev["tier"] == "abstract" else " (nothing retrievable: mark claims UNVERIFIABLE)"),
             f"- Retrieved {utcnow()} from: " + ("; ".join(ev["sources"]) or "-"),
             "- Copyright: the abstract and any full text below belong to their publisher/authors" + (f"; licence: {ev['licence']}" if ev.get("licence") else "") + ". This pack is for your own claim checking. Do not commit it to a public repository or redistribute it.", ""]
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

CITE_NUMERIC_RE = re.compile(
    r"\[(\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*)\]"                                                   # [1], [1-3], [2, 5]
    r"|\^(\d{1,3}(?:[,\-–]\d{1,3})*)\^"                                                         # pandoc superscript ^1^
    r"|(?:(?<=[a-z\)])|(?<=[A-Za-z\)]\.))(\d{1,3}(?:[,\-–]\d{1,3})*)(?=[\s,;]|\.(?:\s|$)|$)")   # AMA bare superscript 'disease.12' -- never after a digit (0.77)
CITE_PANDOC_RE = re.compile(r"\[(@[^\]]+)\]|(?<!\w)@([A-Za-z][\w:.\-]+)")
CITE_TEX_RE = re.compile(r"\\(?:cite[pt]?|parencite|textcite|autocite|citeauthor|citeyear|nocite|footcite)\*?(?:\[[^\]]*\]){0,2}\{([^}]+)\}")
_APA_NAME = _PARTICLES_RE + "[" + _UC + "][" + _UC + _LC + "'’\\-]+"
_APA_YEARS = r"(?:19|20)\d{2}[a-z]?(?:,\s*(?:19|20)\d{2}[a-z]?)*"
CITE_APA_RE = re.compile(
    r"\((?:e\.g\.,?\s*|see\s+(?:also\s+)?|cf\.\s*|but see\s+)?("
    r"(?:" + _APA_NAME + r"(?:\s+et\s+al\.?|(?:,|\s+and|\s*&)\s+" + _APA_NAME + r"){0,3},?\s+" + _APA_YEARS + r"(?:,\s*p+\.\s*\d+(?:[\-–]\d+)?)?(?:;\s*)?){1,8}"
    r")\)")
CITE_APA_NARRATIVE_RE = re.compile(r"\b(" + _APA_NAME + r"(?:\s+et\s+al\.?|(?:\s+and|\s*&)\s+" + _APA_NAME + r")?)\s*\((" + _APA_YEARS + r")\)")
NUMBER_RE = re.compile(r"(?<![\w.])(?<![A-Za-z]-)(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?%?")   # not the 19 of COVID-19, but keep 0.96 in '0.61-0.96'
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
    # protect abbreviations ('et al. (2020)', 'Fig. 1') with a non-whitespace placeholder (\s matches NBSP too)
    text = re.sub(r"\b(et al|Fig|Figs|vs|e\.g|i\.e|Dr|Prof|No|approx|ref|Refs?|cf)\.\s", lambda m: m.group(0)[:-1] + "\x01", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\[\(\"“])", text)
    return [squash(p.replace("\x01", " ")) for p in parts if squash(p.replace("\x01", " "))]


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
            for tm in CITE_TEX_RE.finditer(sent):
                cites += [k.strip() for k in tm.group(1).split(",") if k.strip()]
                kinds.append("latex")
            if not cites and style in ("auto", "numeric"):
                bracketed = [nm for nm in CITE_NUMERIC_RE.finditer(sent) if nm.group(1) or nm.group(2)]
                for nm in CITE_NUMERIC_RE.finditer(sent):
                    tok = nm.group(1) or nm.group(2) or nm.group(3)
                    if not tok:
                        continue
                    if nm.group(3):
                        # bare AMA superscripts only when the sentence has no bracketed/caret cites, never with a leading zero
                        if bracketed or tok.startswith("0"):
                            continue
                    cites += expand_numeric(tok)
                    kinds.append("numeric")
            if not cites and style in ("auto", "apa"):
                for am in CITE_APA_RE.finditer(sent):
                    for piece in re.split(r";\s*", am.group(1)):
                        cites.append(squash(piece))
                    kinds.append("author-year")
                for am in CITE_APA_NARRATIVE_RE.finditer(sent):
                    cites.append(squash(f"{am.group(1)}, {am.group(2)}"))
                    kinds.append("author-year")
            if not cites:
                continue
            cid += 1
            sent_wo_cites = CITE_TEX_RE.sub(" ", CITE_PANDOC_RE.sub(" ", CITE_NUMERIC_RE.sub(" ", sent)))
            numbers = [n.strip() for n in NUMBER_RE.findall(sent_wo_cites) if not re.fullmatch(r"(19|20)\d{2}", n.strip())]
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
    cands: dict[str, dict] = {}
    if "pubmed" in verifier.sources:
        pmids, err = pubmed_search(http, term, max_results)
        for h in pubmed_fetch_many(http, pmids):
            cands[h.doi or f"pmid:{h.pmid}"] = {"pmid": h.pmid, "doi": h.doi, "title": h.title, "first_author": h.first_author, "year": h.year, "journal": h.journal_abbrev or h.journal}
    if "openalex" in verifier.sources:
        extra = openalex_params(http)
        url = "https://api.openalex.org/works?search=" + urllib.parse.quote(query) + f"&per-page={max_results}&select=id,doi,title,publication_year,ids,authorships,primary_location,type,is_retracted,biblio" + (f"&filter=publication_year:{year_from or 1900}-{year_to or 2100}" if (year_from or year_to) else "") + (f"&{extra}" if extra else "")
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
    {"name": "preprint cited (medRxiv DOI prefix)", "expect": "VERIFIED", "expect_flags": ["PREPRINT"],
     "ref": dict(doi="SELFTEST_PREPRINT_DOI")},
    {"name": "fabricated title/authors on a real Zenodo dataset DOI (DataCite)", "expect": "MISMATCH",
     "ref": dict(title="A deep learning electrocardiogram atlas of one million patients with heart failure", first_author="Smith", year="2019", journal="Nature", doi="10.5281/zenodo.23464852")},
    {"name": "paper ABOUT retractions whose title starts with 'Retracted' (not retracted)", "expect": "VERIFIED",
     "ref": dict(title="Retracted Publications in the Biomedical Literature from Open Access Journals", first_author="Wang", year="2019", journal="Sci Eng Ethics", doi="10.1007/s11948-018-0040-6")},
    {"name": "erratum DOI cited as if it were the article", "expect": "MISMATCH", "expect_flags": ["NOTICE_DOI_CITED"],
     "ref": dict(title="Carotid Atherosclerosis in Patients with Atrial Fibrillation", first_author="Wang", year="2019", journal="Curr Atheroscler Rep", doi="10.1007/s11883-019-0818-2")},
    {"name": "Wiley SICI DOI with angle brackets (Newcombe, Stat Med 1998)", "expect": "VERIFIED",
     "text": "Newcombe RG. Two-sided confidence intervals for the single proportion: comparison of seven methods. Stat Med. 1998;17(8):857-872. doi:10.1002/(SICI)1097-0258(19980430)17:8<857::AID-SIM777>3.0.CO;2-E"},
    {"name": "PubMed Bookshelf record (StatPearls) cited by PMID", "expect": "VERIFIED",
     "ref": dict(pmid="30252328")},
    {"name": "web page (WHO fact sheet) with a URL: not a journal article", "expect": "UNVERIFIABLE", "expect_flags": ["WEB_RESOURCE"],
     "text": "World Health Organization. Cardiovascular diseases (CVDs) fact sheet. https://www.who.int/news-room/fact-sheets/detail/cardiovascular-diseases-(cvds)"},
    {"name": "Frankenstein with a non-ASCII first author (Özdemir on the LoDoCo2 DOI)", "expect": "MISMATCH",
     "text": "Özdemir A, Fiolet ATL, Mosterd A, et al. Colchicine in Patients with Chronic Coronary Disease. N Engl J Med. 2020;383(19):1838-1847. doi:10.1056/NEJMoa2021372"},
    {"name": "Frankenstein with a Jr suffix (Smith AB Jr on the SPRINT DOI)", "expect": "MISMATCH",
     "text": "Smith AB Jr, Williamson JD, Whelton PK, et al. A Randomized Trial of Intensive versus Standard Blood-Pressure Control. N Engl J Med. 2015;373(22):2103-2116. doi:10.1056/NEJMoa1511939"},
    {"name": "DOI embedded in a publisher URL with a query string", "expect": "VERIFIED",
     "text": "Nidorf SM, Fiolet ATL, Mosterd A, et al. Colchicine in Patients with Chronic Coronary Disease. N Engl J Med. 2020;383(19):1838-1847. Available from: https://www.nejm.org/doi/full/10.1056/NEJMoa2021372?query=recirc_curatedRelated_article"},
    {"name": "'et al.'-only author list resolves to the trial, not the letter sharing its title", "expect": "VERIFIED",
     "text": "et al. Colchicine in Patients with Chronic Coronary Disease. N Engl J Med. 2020;383(19):1838-1847."},
    {"name": "BMJ Open paper cited as BMJ (sister journal)", "expect": "VERIFIED_WITH_WARNINGS",
     "text": "Himmelreich JCL, Harskamp RE, Geelhoed B, et al. Validating risk models versus age alone for atrial fibrillation in a young Dutch population cohort: should atrial fibrillation risk prediction be expanded to younger community members? BMJ. 2022;12(2):e057476. doi:10.1136/bmjopen-2021-057476"},
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
        if "text" in fx:
            ref = parse_free_citation(fx["text"], fx["name"][:40])
        else:
            kw = dict(fx["ref"])
            if kw.get("doi") == "SELFTEST_PREPRINT_DOI":
                if not pre_doi:
                    print(f"SKIP  {fx['name']} (could not fetch a preprint DOI from CrossRef)")
                    continue
                kw["doi"] = pre_doi
            raw = (kw.get("doi") or kw.get("pmid") or "") if len(kw) == 1 else json.dumps(kw)
            ref = Ref(ref_id=fx["name"][:40], raw=raw, **kw)
            if ref.first_author and not ref.authors:
                ref.authors = [ref.first_author]
            ref.parse_confidence = "high" if (ref.doi or ref.pmid) else "medium"
        res = verifier.verify(ref)
        results.append(res)
        want_flags = fx.get("expect_flags", [])
        ok = res.status == fx["expect"] and all(f in res.flags for f in want_flags)
        got = res.status + (f" flags={res.flags}" if want_flags or not ok else "")
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
        raise InputError(f"unknown source(s): {', '.join(bad)}; choose from {', '.join(ALL_SOURCES)}")
    return toks or DEFAULT_SOURCES


def display_path(p: Path) -> str:
    """Paths written into shareable audit files: relative to the working directory when possible, never the user's home."""
    try:
        return str(p.resolve().relative_to(Path.cwd().resolve()))
    except Exception:
        return p.name


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
        source = display_path(p)
    if args.limit:
        refs = refs[: args.limit]
    if not refs:
        eprint("no references detected. Supported inputs: .md .txt .docx .bib .ris .tex .json .csv .tsv with a 'References' heading or one reference per line/numbered item. "
               "For a pasted list without a heading, make sure each reference starts with its authors, or add a line 'References' above it. PDFs are not read.")
        return 3
    sources = list(parse_sources(args.sources))
    cache_hits = 0
    if args.offline:
        results = []
        for r in refs:
            res = Result(ref_id=r.ref_id, status="UNVERIFIABLE", confidence="low", cited={k: v for k, v in asdict(r).items() if v})
            res.notes.append("offline: parsed only, nothing verified")
            results.append(res)
    else:
        http = make_http(args, out_dir)
        verifier = Verifier(http, tuple(sources), resolve_doi_org=not args.no_doi_resolve, quiet=args.quiet)
        results = []
        for i, r in enumerate(refs, 1):
            if not args.quiet:
                eprint(f"[{i}/{len(refs)}] {r.ref_id}: {(r.doi and 'doi:' + r.doi) or (r.pmid and 'pmid:' + r.pmid) or (r.title[:60] or r.raw[:60])}")
            before_fail, before_gets = http.transport_failures, http.gets
            results.append(verifier.verify(r))
            # Fail fast when the network is down: every lookup for the first reference failed at transport level
            if i == 1 and http.gets - before_gets > 0 and http.transport_failures - before_fail >= http.gets - before_gets and not args.keep_going and len(refs) > 1:
                eprint("[citecheck] every request for the first reference failed (no internet, proxy, or firewall). Stopping; remaining references are marked UNVERIFIABLE. Use --keep-going to try them anyway.")
                for r2 in refs[i:]:
                    res = Result(ref_id=r2.ref_id, status="UNVERIFIABLE", confidence="low", cited={k: v for k, v in asdict(r2).items() if v})
                    res.issues.append("not attempted: network unreachable")
                    results.append(res)
                break
        http.save_cache()
        cache_hits = http.cache_hits
    audit = build_audit(results, source, {"sources": sources, "offline": bool(args.offline), "cache_hits": cache_hits})
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
    if audit["unchecked"]:
        return 4
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    http = make_http(args, None)
    verifier = Verifier(http, parse_sources(args.sources), resolve_doi_org=not args.no_doi_resolve, quiet=True)
    if len(args.query) > 1 and DOI_RE.search(args.query[0]) and not any(len(q.split()) > 1 for q in args.query):
        # PowerShell splits 10.1016/S0140-6736(20)31180-6 into several tokens; the truncated DOI would resolve to the wrong record
        eprint("the DOI looks split by your shell at the parentheses; put it in quotes: resolve \"10.1016/S0140-6736(20)31180-6\"")
        return 2
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
    records: list[dict]
    audit: Optional[dict] = None
    if args.ids:
        refs = refs_from_ids(args.ids)
        p = Path("command-line identifiers")
    else:
        if not args.input:
            eprint("give a reference file, an existing qc/reference_audit.json, or --ids")
            return 2
        p = Path(args.input)
        if not p.exists():
            eprint(f"input not found: {p}")
            return 2
        refs = []
        if p.suffix.lower() == ".json":
            try:
                data = json.loads(read_text(p))
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and data.get("tool") == TOOL_NAME and isinstance(data.get("records"), list):
                audit = data
        if audit is None:
            refs = parse_input(p, args.format)
            if not refs:
                eprint("no references detected")
                return 3
    if audit is None:
        verifier = Verifier(http, parse_sources(args.sources), quiet=args.quiet)
        results = [verifier.verify(r) for r in refs]
        audit = build_audit(results, display_path(p) if not args.ids else "command-line identifiers", {"sources": list(parse_sources(args.sources)), "cache_hits": http.cache_hits})
        out_dir.mkdir(parents=True, exist_ok=True)
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
        index.append({"ref_id": rid, "status": rec.get("status"), "tier": ev["tier"], "file": path.name, "licence": ev.get("licence", ""), "pmid": resolved.get("pmid"), "doi": resolved.get("doi"), "pmcid": ev.get("pmcid"), "retracted": resolved.get("retracted"), "preprint": resolved.get("preprint"), "chars": len(ev["fulltext"] or ev["abstract"])})
        if not args.quiet:
            eprint(f"{rid}: {ev['tier']} ({len(ev['fulltext'] or ev['abstract'])} chars)")
    http.save_cache()
    ev_dir.mkdir(parents=True, exist_ok=True)
    (ev_dir / "index.json").write_text(json.dumps({"generated": utcnow(), "source": display_path(p) if not args.ids else "command-line identifiers",
                                                   "note": "Packs contain publishers' abstracts and open-access full text under their own licences (stated in each pack). They are for your own reading while checking claims: do not commit them to public repositories or redistribute them.",
                                                   "packs": index}, indent=2, ensure_ascii=False), encoding="utf-8")
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
    # 'verified' means at least two independent databases returned the record and nothing is retracted
    verified = [r for r in results if r.status in ("VERIFIED", "VERIFIED_WITH_WARNINGS") and r.resolved and len(r.sources_confirming) >= 2][: args.max]
    single = [r for r in results if r.status in ("VERIFIED", "VERIFIED_WITH_WARNINGS") and r.resolved and len(r.sources_confirming) < 2]
    retracted = [r for r in results if r.status == "RETRACTED" and r.resolved]
    if args.json:
        print(json.dumps({
            "verified": [{"status": r.status, "flags": r.flags, "sources": r.sources_confirming, "record": r.resolved, "vancouver": vancouver(r.resolved)} for r in verified],
            "single_source_unconfirmed": [{"sources": r.sources_confirming, "vancouver": vancouver(r.resolved)} for r in single],
            "retracted_excluded": [{"flags": r.flags, "vancouver": vancouver(r.resolved)} for r in retracted],
        }, indent=2, ensure_ascii=False))
    else:
        print(f"{len(verified)} verified candidates (confirmed by 2+ databases) for: {' '.join(args.query)}\n")
        for i, r in enumerate(verified, 1):
            tag = " ".join(f"[{f}]" for f in r.flags)
            print(f"{i}. {vancouver(r.resolved)}  {tag}\n   confirmed by: {', '.join(r.sources_confirming)}")
        if single:
            print(f"\n{len(single)} candidate(s) seen in only one database (not verified; check by hand):")
            for r in single:
                print(f"   - {vancouver(r.resolved)}  [{', '.join(r.sources_confirming)}]")
        if retracted:
            print(f"\n{len(retracted)} RETRACTED paper(s) matched the query and were excluded:")
            for r in retracted:
                print(f"   - {vancouver(r.resolved)}")
    if args.bib:
        chosen = verified
        if args.pick:
            chosen = [verified[i - 1] for i in args.pick if 1 <= i <= len(verified)]
        if chosen:
            Path(args.bib).parent.mkdir(parents=True, exist_ok=True)
            with open(args.bib, "a", encoding="utf-8") as fh:
                for r in chosen:
                    fh.write(bibtex_entry(r.resolved) + "\n\n")
            print(f"\nappended {len(chosen)} verified BibTeX entr{'y' if len(chosen) == 1 else 'ies'} (with verified_on stamps) to {args.bib}" + ("" if args.pick else "; use --pick N to write only the one you chose"))
        else:
            print("\nnothing written to --bib: no verified candidate" + (" among the picked numbers" if args.pick else ""))
    return 0 if verified else 1


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
    v.add_argument("--keep-going", action="store_true", help="do not stop early when the network is unreachable")
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
    e.add_argument("input", nargs="?", help="reference file, or an existing qc/reference_audit.json")
    e.add_argument("--ids", nargs="+", help="DOIs or PMIDs instead of a file")
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
    f.add_argument("--pick", nargs="+", type=int, help="with --bib: write only these candidate numbers")
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
    except InputError as exc:
        eprint(f"citecheck: {exc}")
        return 2
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError, KeyError, ET.ParseError) as exc:
        eprint(f"citecheck: cannot process the input ({type(exc).__name__}: {exc}). If this is a reference file, check its format; if it keeps failing, please report it with the file type.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
