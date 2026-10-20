# citation-guardrails

**A clinician's guardrails against fabricated citations.** Four Claude Code skills and one
standard-library Python tool that verify references against CrossRef, PubMed, OpenAlex, Europe PMC and
DataCite, check that each cited paper actually says what your sentence says, and keep `[VERIFY]` markers
on everything that has not been confirmed. No dependencies, no API keys, works as a plain CLI too.

[![tests](https://github.com/TW5897/citation-guardrails/actions/workflows/tests.yml/badge.svg)](https://github.com/TW5897/citation-guardrails/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![deps](https://img.shields.io/badge/dependencies-none-brightgreen)
[![license](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

## Why

Language models do not just invent references. They produce a **real DOI with the wrong authors**, a
**real title with an invented year**, a **PMID that belongs to someone else's paper**, an **erratum DOI
cited as the article**, a **retracted trial cited as evidence**, and, most often, a **real, correctly
cited paper attached to a number it never reported**. Each of these passes a glance. Each has reached
submitted manuscripts.

While building this tool I recalled the PMID of the retracted Lancet hydroxychloroquine paper from memory
and got it wrong; the number I "remembered" belongs to an unrelated paediatric COVID paper. The tool
caught it. That is the point: **nothing here is verified from memory.** Every status traces to a public
database record whose URL is kept in the audit.

## What you get

| Skill | Question it answers | Script command |
|---|---|---|
| `check-references` | Does every reference exist, and do the cited title, first author, year and journal match the source record? Is it retracted? A preprint? An erratum? | `citecheck.py verify` |
| `check-claims` | Does each cited sentence say what the source says? (number, direction, endpoint, population) | `citecheck.py claims` + `citecheck.py evidence` |
| `find-reference` | What is a real, verifiable source for this statement? | `citecheck.py find`, `citecheck.py resolve` |
| `cite-safely` | How do I draft so that nothing unverified ships? `[VERIFY]` / `[NEEDS VERIFICATION]` / `[DECIDE]` markers and provenance labels | - |

The skills are instructions for Claude; the script does the lookups. You can use the script alone.

## Install

**As a Claude Code plugin** (all four skills):

```
/plugin marketplace add TW5897/citation-guardrails
/plugin install citation-guardrails@citation-guardrails
```

Updates arrive when you run `/plugin marketplace update citation-guardrails` (or enable auto-update for
this marketplace under `/plugin` > Marketplaces).

**By copying the skills** into your personal skills folder:

```bash
git clone https://github.com/TW5897/citation-guardrails.git
cp -r citation-guardrails/skills/* ~/.claude/skills/
```

On Windows copy the four folders under `skills\` into `%USERPROFILE%\.claude\skills\`. Keep them
together: `check-claims` and `find-reference` call the script that lives in `check-references`.

**Just the script** (no Claude needed):

```bash
curl -O https://raw.githubusercontent.com/TW5897/citation-guardrails/main/skills/check-references/scripts/citecheck.py
python citecheck.py verify my_references.bib --out-dir qc
```

Requirements: Python 3.10 or newer and internet access. Nothing to `pip install`. On Windows, `python3`
is often a Microsoft Store stub that prints "Python was not found"; use `python` or `py -3`, and in
PowerShell use `curl.exe` rather than `curl`.

Optional environment variables: `CITECHECK_EMAIL=you@example.org` puts you in CrossRef's polite pool
(and is passed to NCBI and OpenAlex as the contact they ask for); `NCBI_API_KEY` raises the PubMed rate
limit from 3 to 10 requests per second; `OPENALEX_API_KEY` lifts OpenAlex's keyless daily budget.

## Sixty-second proof

From a clone of this repository:

```bash
cd citation-guardrails
python skills/check-references/scripts/citecheck.py selftest
```

Twenty-four live fixtures, each a different way a citation goes wrong, run against the real APIs
(output abridged to one line per fixture):

```
PASS  real DOI, cited correctly (LoDoCo2, NEJM 2020)                                  VERIFIED
PASS  real PMID, cited correctly (Warsi, Neurologist 2024)                            VERIFIED
PASS  fabricated DOI                                                                  NOT_FOUND
PASS  fabricated PMID                                                                 NOT_FOUND
PASS  Frankenstein: real DOI, invented first author                                   MISMATCH   first author mismatch: cited 'Ebrahimi', source says 'Nidorf'
PASS  Frankenstein: real DOI, wrong title (COLCOT title on LoDoCo2 DOI)               MISMATCH
PASS  wrong PMID that points to an unrelated real paper                               MISMATCH
PASS  DOI and PMID from two different papers                                          MISMATCH
PASS  retracted paper (Mehra, Lancet 2020)                                            RETRACTED
PASS  title only, real paper, no identifiers                                          VERIFIED
PASS  title only, fabricated paper                                                    NOT_FOUND
PASS  year off by one (online-first vs print)                                         VERIFIED_WITH_WARNINGS
PASS  preprint cited (medRxiv DOI prefix)                                             VERIFIED flags=['PREPRINT']
PASS  fabricated title/authors on a real Zenodo dataset DOI (DataCite)                MISMATCH
PASS  paper ABOUT retractions whose title starts with 'Retracted' (not retracted)     VERIFIED
PASS  erratum DOI cited as if it were the article                                     MISMATCH flags=['NOTICE_DOI_CITED']
PASS  Wiley SICI DOI with angle brackets (Newcombe, Stat Med 1998)                    VERIFIED
PASS  PubMed Bookshelf record (StatPearls) cited by PMID                              VERIFIED
PASS  web page (WHO fact sheet) with a URL: not a journal article                     UNVERIFIABLE flags=['WEB_RESOURCE']
PASS  Frankenstein with a non-ASCII first author (Özdemir on the LoDoCo2 DOI)         MISMATCH
PASS  Frankenstein with a Jr suffix (Smith AB Jr on the SPRINT DOI)                   MISMATCH
PASS  DOI embedded in a publisher URL with a query string                             VERIFIED
PASS  'et al.'-only author list resolves to the trial, not the letter sharing its title  VERIFIED
PASS  BMJ Open paper cited as BMJ (sister journal)                                    VERIFIED_WITH_WARNINGS

24/24 fixtures behaved as expected.
```

## Use it in Claude Code

Once installed, the skills trigger on their own when references, citations or submission come up. You
can also ask directly:

- "Check the references in `manuscript.docx` before I send it to my co-authors."
- "Run check-claims on the introduction; I want to know if each cited sentence is actually supported."
- "Find me a verified reference for 'colchicine reduces cardiovascular events after MI', with the DOI."
- "Draft the discussion with cite-safely rules: mark anything you can't verify."

Paste `docs/claude-md-snippet.md` into your `CLAUDE.md` to make the rules standing policy.

## Use it from the terminal

```bash
C=skills/check-references/scripts/citecheck.py                 # PowerShell: $C = "skills\check-references\scripts\citecheck.py"

python $C verify examples/manuscript-audit/manuscript.md --out-dir qc      # .md .qmd .txt .docx .bib .ris .tex .json .csv .tsv
python $C verify refs.bib --strict                                         # exit 1 unless every reference is VERIFIED
python $C verify --ids "10.1056/NEJMoa2021372" 32865380 "The PRISMA 2020 statement: an updated guideline for reporting systematic reviews"
python $C resolve "10.1016/S0140-6736(20)31180-6"                          # one identifier -> status, flags, consensus record (quote DOIs with parentheses)
python $C find "colchicine chronic coronary disease randomized" --year-from 2019 --bib references/library.bib --pick 1
python $C claims manuscript.md --out-dir qc                                # every sentence that carries a citation, as a checklist
python $C evidence qc/reference_audit.json --out-dir qc                    # abstracts / open-access full text for claim checking
```

Running `verify` on the example manuscript in [`examples/manuscript-audit/`](examples/manuscript-audit/)
(real references plus one fabricated, one retracted and one Frankenstein citation planted on purpose):

```
citecheck v0.1.0 - 7 references from manuscript.md
ref_1          VERIFIED               Nidorf 2020 Colchicine in Patients with Chronic Coronary Dis
ref_2          VERIFIED               Tardif 2019 Efficacy and Safety of Low-Dose Colchicine after
ref_3          VERIFIED               Warsi 2024 Risk Factors of Acute Ischemic Stroke and Mortal
ref_4          RETRACTED              Mehra 2020 Hydroxychloroquine or chloroquine with or withou   [RETRACTED,EXPRESSION_OF_CONCERN,CORRECTION_PUBLISHED] this work has been RETRACTED; do not cite it as evidence
ref_5          VERIFIED               Page 2021 The PRISMA 2020 statement: an updated guideline
ref_6          NOT_FOUND              Smith 2023 Intravenous Turmeric and 30-Day Mortality in Car
ref_7          MISMATCH               Ebrahimi 2020 Colchicine in Patients with Chronic Coronary Dis   first author mismatch: cited 'Ebrahimi', source says 'Nidorf'
counts: MISMATCH=1, NOT_FOUND=1, RETRACTED=1, VERIFIED=4 | flags: CORRECTION_PUBLISHED=1, EXPRESSION_OF_CONCERN=1, RETRACTED=1
submission_safe=False  blocking=['ref_4', 'ref_6', 'ref_7']  unchecked=[]  needs_review=[]
```

The full audit lands in `qc/reference_audit.json` (every lookup URL, every field comparison, the
consensus record) and `qc/reference_audit.md` (a table you can send to a co-author), with a method
statement built from what was actually checked.

Exit codes: `0` clean, `1` a blocking problem (or any non-VERIFIED row with `--strict`), `2` input or
usage error, `3` no references detected, `4` nothing blocking but some references could not be checked.

## What the statuses mean

| Status | Meaning | What to do |
|---|---|---|
| `VERIFIED` | Identifiers resolve; title, first author, year and journal agree with the sources | Nothing |
| `VERIFIED_WITH_WARNINGS` | The paper exists; a minor field differs (year off by one, sister journal, pages), or a field could not be compared (`AUTHOR_NOT_CHECKED`) | Fix the field, or compare by hand |
| `MISMATCH` | The paper exists, but the title or first author differs, the year differs by more than one, the DOI is an erratum/correction notice (`NOTICE_DOI_CITED`), or the DOI and PMID point to different papers | Treat as a wrong citation until corrected from the source record |
| `NOT_FOUND` | The DOI/PMID is not registered, or no database has a paper with this title | Treat as fabricated until you can produce the source. With `IDENTIFIER_WRONG_BUT_TITLE_EXISTS` the paper is real and only the identifier is wrong |
| `RETRACTED` | Exists, but retracted or withdrawn | Do not cite as evidence |
| `UNVERIFIABLE` | Could not be checked: a web page (`WEB_RESOURCE`), a DOI with no bibliographic record (`DOI_RESOLVES_NO_METADATA`), a bare title with no author or year to confirm it, a network failure, or `--offline` | Check by hand. Unchecked is never verified |

`submission_safe` is true only when every reference was checked and none is blocking. Other flags:
`PREPRINT`, `PUBLISHED_VERSION_EXISTS`, `EXPRESSION_OF_CONCERN`, `CORRECTION_PUBLISHED`, `TITLE_MATCHED`
(matched by title plus the cited author or year), `MULTIPLE_MATCHES` (co-publications share the title;
add the DOI), `MULTIPLE_DOIS`, `PMCID_MISMATCH`, `RETRACTION_HINT`, `SINGLE_SOURCE_CONFLICT`.
`confidence: high` means two or more independent databases returned the record.

## How it checks

1. Parse the reference (BibTeX, RIS, Vancouver/AMA, APA, pandoc, DOCX including Word auto-numbering,
   LaTeX `\bibitem`, JSON/CSL-JSON, CSV). UTF-8, UTF-16 and Windows-1252 files are read; PDFs are refused
   with instructions.
2. Resolve identifiers: CrossRef, DataCite (Zenodo, arXiv, datasets) and the doi.org registry for the DOI;
   PubMed (including Bookshelf) for the PMID; OpenAlex and Europe PMC for both. A DOI-only citation is
   enriched with its PubMed record via the PMID the other databases report. A DOI that doi.org, CrossRef
   and DataCite do not know is NOT_FOUND even if a secondary index has a stray record for it.
3. Check that the DOI record and the PMID record are the same work, and that a cited PMCID belongs to it.
4. Compare the cited title (fuzzy, subtitle-tolerant), first-author surname (particle-, hyphen-, suffix- and
   accent-tolerant), year, journal (abbreviation-aware; sister journals do not match), volume and first
   page against every record found. **Nothing is VERIFIED unless at least one record with metadata was
   compared**; a bare doi.org redirect is UNVERIFIABLE.
5. Collect retraction signals from CrossRef `updated-by` (including Retraction Watch entries), OpenAlex
   `is_retracted`, PubMed `Retracted Publication` / `RetractionIn` and Europe PMC; detect erratum and
   correction notices cited as the article; flag preprints (CrossRef `posted-content`, DOI prefixes,
   PubMed `Preprint`).
6. For citations with no identifier, search all four databases by title and accept a candidate only when
   the cited author or year also agrees; use the journal to separate co-publications and volume/pages to
   separate letters that reuse a paper's title.

Details, thresholds and the audit schema: [`docs/how-it-works.md`](docs/how-it-works.md). The catalogue of
failure modes, each with the fixture that proves it is caught: [`docs/failure-modes.md`](docs/failure-modes.md).

## The writing workflow

`cite-safely` is the part that makes the audits short. While drafting:

- No citation is written from memory; `find-reference` locates and verifies it first (two databases
  must agree, retracted papers are excluded, and only the paper you pick is written to your `.bib`).
- Anything not yet confirmed carries `[VERIFY]`. A reference that cannot be resolved carries
  `[NEEDS VERIFICATION]` and never ships. An open methodological choice carries `[DECIDE]`.
- Statements are labelled (a) what a source says, (b) your own data, (c) inference, when it matters.
- Before anything leaves the draft: `check-references`, then `check-claims`.

`check-claims` is deliberately a two-part design. The script extracts every cited sentence (numeric,
superscript, pandoc, LaTeX `\cite`, author-year) and fetches each source's abstract or open-access full
text (Europe PMC, with its licence) into an evidence pack that states the **tier** reached. Claude (or
you) then judges each claim against the source's own words and must quote them. A subgroup result checked
against an abstract that does not mention it is `PARTIALLY-SUPPORTED (abstract-only)`, never `SUPPORTED`.
The worked example in [`skills/check-claims/SKILL.md`](skills/check-claims/SKILL.md) uses the real LoDoCo2
abstract.

## Honest limits

- It verifies literature with identifiers or a searchable title. Books, guidelines without DOIs, theses
  and web pages come back `UNVERIFIABLE` or `NOT_FOUND` and need a human; a cited URL is never opened.
- It does not read PDFs. Export the reference list or manuscript to DOCX or text first.
- Title-only matching is `medium` confidence by design and needs the cited author or year. Add DOIs or
  PMIDs to your references.
- The first author is checked; a fabricated middle author passes. Volume and page differences are warnings.
- Claim checking needs a reader. The script fetches evidence and enforces the tier; it does not decide
  whether a sentence is supported.
- It says nothing about the quality of the evidence. A `VERIFIED` paper can still be the wrong paper to cite.
- It needs the network. A 60-reference list takes a minute or two the first time and seconds after that
  thanks to the local cache (positives kept 7 days, negatives 24 hours; use `--no-cache` for the final run).

## Privacy

The script sends each reference's identifiers and its parsed title, first author and year to CrossRef,
NCBI (PubMed), OpenAlex, Europe PMC, DataCite and doi.org. doi.org redirects are not followed, so no
publisher site is contacted and your IP never reaches one through this tool. Manuscript prose is not
sent by the script; the `claims` and `evidence` commands work locally except for fetching abstracts and
open-access text by identifier. Your contact email, if you set one, goes to CrossRef (User-Agent), NCBI
and OpenAlex (query parameter) only, and it and any API key are redacted from every URL written to the
audit, evidence and cache files. Evidence packs contain publishers' text under their own licences: keep
them for your own checking and do not commit them to a public repository.

When you use the skills inside Claude Code, Claude itself reads your manuscript, the extracted cited
sentences and the evidence packs, and that content is processed by Anthropic under your plan's data terms.
Use the CLI alone for drafts you cannot share that way.

## Prior art and thanks

- [medsci-skills](https://github.com/Aperivue/medsci-skills) (Aperivue, MIT) for the idea of an
  audit-only reference checker inside a research skill set and for documenting the first-author
  hallucination incident that motivated the Frankenstein checks here. This repository shares no code with it.
- [citation-check-skill](https://github.com/serenakeyitan/citation-check-skill) (MIT),
  [VeriExCite](https://github.com/ykangw/VeriExCiting) (AGPL-3.0) and
  [CiteGuard](https://github.com/larrylot/citeguard) (MIT) approach the same problem for general LLM
  output, PDFs and URL-cited reports respectively.
- [Retraction Watch](https://retractionwatch.com/), whose database reaches this tool through CrossRef.
- CrossRef, NCBI, OpenAlex, Europe PMC and DataCite for keeping their APIs open.

## Contributing

Wrongly classified references are the most useful bug reports; see [CONTRIBUTING.md](CONTRIBUTING.md).
Run `python -m unittest discover -s tests` (offline) and `citecheck.py selftest` (live) before a pull request.

## Licence and citing

MIT. If this tool is part of your workflow, cite it via [`CITATION.cff`](CITATION.cff).

Built by [Talal Warsi](https://github.com/TW5897), an internal medicine doctor in the NHS doing cardiology
and clinical-AI research, because the alternative was checking sixty references by hand before every submission.
