---
name: check-references
description: >
  Verify that every reference in a manuscript, bibliography or reference list actually exists and is
  cited correctly, by querying CrossRef, PubMed, OpenAlex, Europe PMC and DataCite and comparing the cited
  title, first author, year and journal with the source record. Catches fabricated DOIs/PMIDs,
  "Frankenstein" citations (a real DOI paired with an invented author or title), DOI/PMID pairs that
  point to different papers, erratum DOIs cited as the article, retracted papers, preprints and
  ambiguous title-only citations. Use this whenever the user mentions references, citations, a
  bibliography, a reference list, .bib/.ris files, hallucinated or fabricated citations, "check my
  refs", or is about to submit, circulate or post a manuscript, abstract, protocol, thesis, editorial or
  review that cites literature, even if they do not ask for a formal audit. Always run it after any
  AI-assisted drafting or revision touched references.
allowed-tools: Bash(python "${CLAUDE_SKILL_DIR}/scripts/citecheck.py" *), Bash(python3 "${CLAUDE_SKILL_DIR}/scripts/citecheck.py" *), Bash(py -3 "${CLAUDE_SKILL_DIR}/scripts/citecheck.py" *), Read, Glob, Grep
---

# check-references

You are auditing whether references are **real and cited correctly**. You never verify a reference
from memory, and you never "fix" a reference by inventing a plausible alternative. The bundled script
does the lookups; you interpret the audit, explain it, and apply corrections only from the source
records the script returned, and only with the user's permission.

This skill checks **existence and metadata**. Whether the paper actually supports the sentence that
cites it is a separate question: hand off to `check-claims` afterwards.

## The script

`${CLAUDE_SKILL_DIR}/scripts/citecheck.py` (Claude Code substitutes the directory of this file).
Standard-library Python 3.10+, no dependencies, no API keys. If the file is missing, stop and tell the
user to install the `check-references` skill; do not verify anything from memory.

```bash
python "${CLAUDE_SKILL_DIR}/scripts/citecheck.py" verify <manuscript-or-bibliography> --out-dir qc
```

Use `python` on Windows (`python3` is often a Store stub) and `python3` elsewhere; `py -3` also works
on Windows. Always put DOIs that contain parentheses in quotes.

Accepted inputs: `.md` `.qmd` `.txt` `.docx` (the References section is detected; Word auto-numbering
and one-reference-per-line lists are fine), `.bib`, `.ris`, `.tex`/`.bbl`, `.json` (including CSL-JSON
from Zotero/Mendeley), `.csv`/`.tsv` (columns `title, authors, year, journal, doi, pmid`), or ad-hoc
identifiers with `--ids "10.1056/NEJMoa2021372" 32865380 "a quoted title"`. PDFs are refused with an
explanation: ask for the `.docx` or a text export.

Useful options: `--strict` (exit 1 unless every row is VERIFIED), `--sources`
(default `crossref,pubmed,openalex,europepmc,datacite`; add `semanticscholar` if wanted),
`--email you@x.org` (CrossRef polite pool; or set `CITECHECK_EMAIL`), `--no-cache` (fresh lookups for
the final pre-submission run), `--limit N`, `--json`, `--offline` (parse only; nothing is verified).
`NCBI_API_KEY` raises the PubMed rate limit. Responses are cached in `qc/.citecheck-cache.json`
(positives 7 days, negatives 24 h), so re-runs after edits are fast.

If the manuscript uses pandoc `[@key]` citations, verify the `.bib` file, not the manuscript.

Exit codes: 0 clean; 1 a blocking row (or, with `--strict`, any non-VERIFIED row); 2 input or usage
error; 3 no references detected; 4 nothing blocking but some rows could not be checked.

## Procedure

1. **Find the input.** Manuscript, `.bib`, or a pasted reference list (save pasted text to a `.txt`
   first). Confirm which file is the source of truth for references.
2. **Run** `verify ... --out-dir qc` and show the one-line summary. Exit 3 means the list was not
   recognised: check for a `References` heading or one reference per line, or ask where the references are.
   Exit 4, or any `UNVERIFIABLE` row, means part of the list was **not checked**: say so plainly.
3. **Read** `qc/reference_audit.json` (machine) and `qc/reference_audit.md` (human). Report in this
   order, every time:
   1. `RETRACTED`, `NOT_FOUND`, `MISMATCH` rows (these block submission): cited text, what the sources
      hold (`resolved`), the `issues` line, and the flags.
   2. `UNVERIFIABLE` rows: not checked, not fine. Explain why (web resource, DOI resolving without
      metadata, network).
   3. `VERIFIED_WITH_WARNINGS` rows: the paper exists; a field needs fixing, or something could not be
      compared (`AUTHOR_NOT_CHECKED`, `FIELDS_NOT_CHECKED`, `SINGLE_SOURCE_CONFLICT`).
   4. Flags needing a human look: `PREPRINT`, `MULTIPLE_MATCHES`, `TITLE_MATCHED`, `RETRACTION_HINT`,
      `MULTIPLE_DOIS`, `BOOK_CHAPTER`.
   5. Counts, `checked`/`unchecked`, and the `submission_safe` value. `submission_safe` is true only
      when every reference was checked and none is blocking.
4. **Propose fixes, from the source record only.** Each record in the audit JSON carries the source
   record's fields in `resolved` and a ready-made Vancouver string in `vancouver`.
   - `MISMATCH`: replace the wrong field(s) with the source record's; say which field was wrong.
   - `MISMATCH` + `NOTICE_DOI_CITED`: the DOI belongs to an erratum/correction notice; cite the
     original article (the note gives its DOI when CrossRef knows it).
   - `MISMATCH` + `IDENTIFIER_CONFLICT` / `PMCID_MISMATCH` / `MULTIPLE_PMIDS`: the identifiers in the item
     belong to different papers; keep the one that matches the cited title and drop the others.
   - `MISMATCH` + `DIFFERENT_WORK`: several fields (title wording, year, volume, first page) point to a sibling
     paper; the DOI/PMID is the wrong one of two related papers. Resolve from the cited title.
   - `NOT_FOUND` + `IDENTIFIER_WRONG_BUT_TITLE_EXISTS`: **the paper is real** but the cited DOI/PMID
     is wrong; use the identifier in the note and re-run. Never call this reference fabricated.
   - `NOT_FOUND` + `TITLE_EXISTS_AUTHOR_YEAR_DIFFER`: a paper with this title exists but the cited
     author or year is wrong; show the candidate and let the user decide.
   - Plain `NOT_FOUND`: no database has this reference. Offer `find-reference` to look for a real
     source for the sentence, or recommend deleting the citation. Never substitute a similar-sounding
     paper without the user confirming it supports the claim.
   - `RETRACTED`: recommend removal, or citing it only as a retracted paper with the retraction notice.
   - `UNVERIFIABLE` + `WEB_RESOURCE`: a web page or organisation document; ask the user to confirm the
     URL opens and says what is claimed. `LAYOUT_NOT_RECOGNISED`: the citation style could not be parsed
     (unusual Harvard/MLA variants, odd punctuation); ask for a DOI/PMID or a Vancouver/APA version. `DOI_RESOLVES_NO_METADATA`: open the DOI and compare by hand.
     `TITLE_ONLY_UNCONFIRMED`: the title exists but no author/year was cited; add the DOI.
   - `PREPRINT`: say so; with `PUBLISHED_VERSION_EXISTS`, offer the published DOI.
   - `MULTIPLE_MATCHES`: several works share the title (co-publications); add the DOI or PMID.
5. **Edit only with permission**, change only the reference text, and re-run `verify`. A reference
   is not fixed until the re-run says `VERIFIED`.
6. **Never soften the verdict.** If `submission_safe` is false, say the manuscript is not safe to
   submit, and say whether that is because of blocking rows, unchecked rows, or both.
7. **Hand off**: when existence is clean, suggest `check-claims` to confirm each cited sentence is
   supported by its source.

## Reading the statuses

| Status | Meaning | Action |
|---|---|---|
| `VERIFIED` | Identifiers resolve; title, first author, year and journal agree with the sources | None |
| `VERIFIED_WITH_WARNINGS` | Exists; a minor field differs, or a field could not be compared | Fix the field / compare by hand |
| `MISMATCH` | Exists, but title or first author differs, year differs by more than one, the DOI is a notice, or DOI and PMID point to different papers | Treat as a wrong citation until corrected |
| `NOT_FOUND` | DOI/PMID not registered, or no database holds a paper with this title | Treat as fabricated until the user produces the source (see the flags above) |
| `RETRACTED` | Exists but is retracted or withdrawn | Do not cite as evidence |
| `UNVERIFIABLE` | Could not be checked: web resource, DOI without a metadata record, network failure, offline | Check by hand; never count as verified |

`confidence: high` means two or more independent databases returned the record. `found_by: title`
(flag `TITLE_MATCHED`) means the citation carried no DOI/PMID and was matched on title plus the cited
author or year; ask the user to add the identifier the audit found.

## What leaves the machine

Only reference identifiers and the parsed title, first author and year of each reference, sent to
CrossRef, PubMed, OpenAlex, Europe PMC, DataCite and doi.org (doi.org redirects are not followed,
so publisher sites are never contacted). Manuscript prose is never sent by the script. You, the model,
read the manuscript; say so if the user asks where their text goes.

## Do not

- Do not mark anything verified without a `VERIFIED` row from the script in this session.
- Do not recall DOIs, PMIDs, page numbers or author lists from memory, even to "help".
- Do not rewrite references in a style the manuscript does not use.
- Do not call a manuscript clean while any blocking row or `UNVERIFIABLE` row remains.
- Do not treat `PREPRINT` or `CORRECTION_PUBLISHED` as errors; report them as facts the author should know.

## Self-test

If the user doubts the tool, run `python "${CLAUDE_SKILL_DIR}/scripts/citecheck.py" selftest`. It
verifies about two dozen live fixtures: real DOI and PMID, fabricated DOI and PMID, Frankenstein
citations (invented first author, wrong title, non-ASCII and suffixed names), a wrong PMID that points
to another real paper, a DOI/PMID conflict, a retracted paper, a paper whose title merely starts with
"Retracted", an erratum DOI cited as the article, fabricated metadata on a real Zenodo DOI, a Wiley
SICI DOI, a PubMed Bookshelf record, a web page, a URL-embedded DOI, title-only real and fabricated
papers, a year-off-by-one citation, a sister-journal error and a preprint. Each line prints PASS or
FAIL with the expected and observed status.

## Related skills

- `check-claims`: does each cited sentence say what the source says? Run after this skill.
- `find-reference`: find and verify a real source for a sentence that has none, or whose reference was `NOT_FOUND`.
- `cite-safely`: the writing-time discipline (`[VERIFY]` markers, provenance labels) that makes this audit short.
