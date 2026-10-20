---
name: check-references
description: >
  Verify that every reference in a manuscript, bibliography or reference list actually exists and is
  cited correctly, by querying CrossRef, PubMed, OpenAlex and Europe PMC and comparing the cited
  title, first author, year and journal with the source record. Catches fabricated DOIs/PMIDs,
  "Frankenstein" citations (a real DOI paired with an invented author or title), DOI/PMID pairs that
  point to different papers, retracted papers, preprints and ambiguous title-only citations. Use this
  whenever the user mentions references, citations, a bibliography, a reference list, .bib/.ris files,
  hallucinated or fabricated citations, "check my refs", or is about to submit, circulate or post a
  manuscript, abstract, protocol, thesis, editorial or review that cites literature, even if they do not
  ask for a formal audit. Always run it after any AI-assisted drafting or revision touched references.
allowed-tools: Bash, Read, Write, Glob, Grep
---

# check-references

You are auditing whether references are **real and cited correctly**. You never verify a reference
from memory, and you never "fix" a reference by inventing a plausible alternative. The bundled
script does the lookups; you interpret the audit, explain it to the user, and apply corrections only
from the source records the script returned.

This skill checks **existence and metadata**. Whether the paper actually supports the sentence that
cites it is a separate question: hand off to `check-claims` afterwards.

## The script

`scripts/citecheck.py` sits next to this file. It is standard-library Python 3.10+ with no
dependencies and no API keys. Resolve its absolute path from wherever this skill was loaded.

```bash
# Linux / macOS
python3 <skill-dir>/scripts/citecheck.py verify <manuscript-or-bibliography> --out-dir qc
# Windows (python3 is often a Store stub; use python or the py launcher)
python <skill-dir>\scripts\citecheck.py verify <file> --out-dir qc
py -3 <skill-dir>\scripts\citecheck.py verify <file> --out-dir qc
```

Accepted inputs: `.md` `.qmd` `.txt` `.docx` (the References section is detected), `.bib`, `.ris`,
`.tex` (`\bibitem`), `.json`, `.csv`/`.tsv` (columns `title, authors, year, journal, doi, pmid`),
or ad-hoc identifiers with `--ids 10.1056/NEJMoa2021372 32865380 "a quoted title"`.

Useful options: `--strict` (exit 1 unless every row is VERIFIED), `--sources` (default
`crossref,pubmed,openalex,europepmc`; add `semanticscholar` if wanted), `--email you@x.org`
(polite API pools; or set `CITECHECK_EMAIL`), `--limit N`, `--json`, `--offline` (parse only).
Set `NCBI_API_KEY` to raise the PubMed rate limit. Responses are cached in `qc/.citecheck-cache.json`
for 7 days, so re-runs after edits are fast.

If the user's manuscript uses pandoc `[@key]` citations, verify the `.bib` file, not the manuscript.
If the input is a PDF, ask for the source document or a text export; the script does not read PDFs.

## Procedure

1. **Find the input.** Manuscript, `.bib`, or the reference list the user pasted (save pasted text to a
   `.txt` first). Confirm which file is the source of truth for references.
2. **Run** `verify ... --out-dir qc`. Show the one-line summary the script prints. If it reports
   `no references detected`, the References heading was probably not found: try `--format text` on an
   extracted reference list, or ask the user where the references are.
3. **Read** `qc/reference_audit.json` (machine) and `qc/reference_audit.md` (human). Report in this
   order, every time:
   1. `RETRACTED`, `NOT_FOUND`, `MISMATCH` rows (these block submission), each with the cited text,
      what the sources actually hold (`resolved`), and the specific `issues` line.
   2. `VERIFIED_WITH_WARNINGS` rows (the paper exists; a field needs fixing).
   3. `UNVERIFIABLE` rows and any `PREPRINT`, `MULTIPLE_MATCHES`, `TITLE_MATCHED` flags (need a human
      look or an identifier added).
   4. The counts and the `submission_safe` verdict.
4. **Propose fixes, from the source record only.**
   - `MISMATCH` with a correct identifier: replace the cited fields with the `resolved` record (the
     audit gives a ready Vancouver string). Say which field was wrong.
   - `MISMATCH` flagged `IDENTIFIER_WRONG_BUT_TITLE_EXISTS`: the paper is real but the DOI/PMID is
     wrong; use the identifier the note reports.
   - `NOT_FOUND`: say plainly that no database has this reference. Offer `find-reference` to look for
     a real source for the sentence, or recommend deleting the citation. Never substitute a
     similar-sounding paper without the user confirming it supports the claim.
   - `RETRACTED`: recommend removal, or citing it only as a retracted paper with the retraction notice.
   - `PREPRINT`: say so; if `PUBLISHED_VERSION_EXISTS`, offer the published DOI.
   - `MULTIPLE_MATCHES`: several works share the title (co-publications); add the DOI or PMID.
5. **Edit only with permission**, change only the reference text, and re-run `verify` afterwards. A
   reference is not fixed until the re-run says `VERIFIED`.
6. **Never soften the verdict.** If `submission_safe` is false, say the manuscript is not safe to
   submit. If rows are `UNVERIFIABLE`, say they were not checked, not that they are fine.
7. **Hand off**: when existence is clean, suggest `check-claims` to confirm each cited sentence is
   supported by its source.

## Reading the statuses

| Status | Meaning | Action |
|---|---|---|
| `VERIFIED` | Identifiers resolve; title, first author, year and journal agree with the sources | None |
| `VERIFIED_WITH_WARNINGS` | Exists; minor field differs (year off by one, journal spelling, pages) | Fix the field |
| `MISMATCH` | Exists, but title or first author differs, year differs by more than one, or DOI and PMID point to different papers | Treat as a wrong citation until corrected |
| `NOT_FOUND` | DOI/PMID does not exist, or no database holds a paper with this title | Treat as fabricated until the user produces the source |
| `RETRACTED` | Exists but is retracted or withdrawn | Do not cite as evidence |
| `UNVERIFIABLE` | Could not be checked (network failure, offline, nothing parseable) | Check by hand |

`confidence: high` means two or more independent databases confirmed the record. `found_by: title`
(flag `TITLE_MATCHED`) means the citation carried no DOI/PMID and was matched on title plus
author/year; ask the user to add the identifier the audit found.

## Do not

- Do not mark anything verified without a `VERIFIED` row from the script in this session.
- Do not recall DOIs, PMIDs, page numbers or author lists from memory, even to "help".
- Do not rewrite references in a style the manuscript does not use.
- Do not call a manuscript clean while any blocking row or `UNVERIFIABLE` row remains.
- Do not treat `PREPRINT` or `CORRECTION_PUBLISHED` as errors; report them as facts the author should know.

## Self-test

If the user doubts the tool, run `python citecheck.py selftest`. It verifies thirteen live fixtures:
real DOI, real PMID, fabricated DOI, fabricated PMID, two Frankenstein citations, a wrong PMID that
points to another real paper, a DOI/PMID conflict, a retracted paper, a title-only real paper, a
title-only fabricated paper, a year-off-by-one citation and a preprint. Each line prints PASS or FAIL
with the expected and observed status.

## Related skills

- `check-claims`: does each cited sentence say what the source says? Run after this skill.
- `find-reference`: find and verify a real source for a sentence that has none, or whose reference was `NOT_FOUND`.
- `cite-safely`: the writing-time discipline (`[VERIFY]` markers, provenance labels) that makes this audit short.
