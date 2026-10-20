# citation-guardrails

**A clinician's guardrails against fabricated citations.** Four Claude Code skills and one
standard-library Python tool that verify references against CrossRef, PubMed, OpenAlex and Europe PMC,
check that each cited paper actually says what your sentence says, and keep `[VERIFY]` markers on
everything that has not been confirmed. No dependencies, no API keys, works as a plain CLI too.

[![tests](https://github.com/TW5897/citation-guardrails/actions/workflows/tests.yml/badge.svg)](https://github.com/TW5897/citation-guardrails/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![deps](https://img.shields.io/badge/dependencies-none-brightgreen)
[![license](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

## Why

Language models do not just invent references. They produce a **real DOI with the wrong authors**, a
**real title with an invented year**, a **PMID that belongs to someone else's paper**, a **retracted
trial cited as evidence**, and, most often, a **real, correctly cited paper attached to a number it never
reported**. Each of these passes a glance. Each has reached submitted manuscripts.

While building this tool I recalled the PMID of the retracted Lancet hydroxychloroquine paper from memory
and got it wrong by a few hundred thousand; the number I "remembered" belongs to an unrelated paediatric
COVID paper. The tool caught it. That is the point: **nothing here is verified from memory.** Every status
traces to a public database record whose URL is kept in the audit.

## What you get

| Skill | Question it answers | Script command |
|---|---|---|
| `check-references` | Does every reference exist, and do the cited title, first author, year and journal match the source record? Is it retracted? A preprint? | `citecheck.py verify` |
| `check-claims` | Does each cited sentence say what the source says? (number, direction, endpoint, population) | `citecheck.py claims` + `citecheck.py evidence` |
| `find-reference` | What is a real, verifiable source for this statement? | `citecheck.py find`, `citecheck.py resolve` |
| `cite-safely` | How do I draft so that nothing unverified ships? `[VERIFY]` / `[NEEDS VERIFICATION]` / `[DECIDE]` markers and provenance labels | - |

The skills are instructions for Claude; the script does the lookups. You can use the script alone.

## Sixty-second proof

```bash
python skills/check-references/scripts/citecheck.py selftest
```

Thirteen live fixtures, each a different way a citation goes wrong, run against the real APIs:

```
PASS  real DOI, cited correctly (LoDoCo2, NEJM 2020)              expected VERIFIED               got VERIFIED
PASS  real PMID, cited correctly (Warsi, Neurologist 2024)        expected VERIFIED               got VERIFIED
PASS  fabricated DOI                                              expected NOT_FOUND              got NOT_FOUND
PASS  fabricated PMID                                             expected NOT_FOUND              got NOT_FOUND
PASS  Frankenstein: real DOI, invented first author               expected MISMATCH               got MISMATCH
      first author mismatch: cited 'Ebrahimi', source says 'Nidorf'
PASS  Frankenstein: real DOI, wrong title (COLCOT title on LoDoCo2 DOI)   expected MISMATCH       got MISMATCH
PASS  wrong PMID that points to an unrelated real paper           expected MISMATCH               got MISMATCH
PASS  DOI and PMID from two different papers                      expected MISMATCH               got MISMATCH
PASS  retracted paper (Mehra, Lancet 2020)                        expected RETRACTED              got RETRACTED
PASS  title only, real paper, no identifiers                      expected VERIFIED               got VERIFIED
PASS  title only, fabricated paper                                expected NOT_FOUND              got NOT_FOUND
PASS  year off by one (online-first vs print)                     expected VERIFIED_WITH_WARNINGS got VERIFIED_WITH_WARNINGS
PASS  preprint cited (medRxiv DOI prefix)                         expected PREPRINT_FLAG          got VERIFIED flags=['PREPRINT']

13/13 fixtures behaved as expected.
```

## Install

**As a Claude Code plugin** (all four skills, kept up to date):

```
/plugin marketplace add TW5897/citation-guardrails
/plugin install citation-guardrails@citation-guardrails
```

**By copying the skills** into your personal skills folder:

```bash
git clone https://github.com/TW5897/citation-guardrails.git
cp -r citation-guardrails/skills/* ~/.claude/skills/        # Windows: copy to %USERPROFILE%\.claude\skills\
```

**Just the script** (no Claude needed):

```bash
curl -O https://raw.githubusercontent.com/TW5897/citation-guardrails/main/skills/check-references/scripts/citecheck.py
python citecheck.py verify my_references.bib --out-dir qc
```

Requirements: Python 3.10 or newer, internet access. Nothing to `pip install`. On Windows, `python3` is
often a Microsoft Store stub that prints "Python was not found"; use `python` or `py -3`.

Optional: `export CITECHECK_EMAIL=you@example.org` puts you in the polite pools of CrossRef, OpenAlex and
NCBI; `export NCBI_API_KEY=...` raises the PubMed rate limit from 3 to 10 requests per second.

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
C=skills/check-references/scripts/citecheck.py

python $C verify manuscript.md --out-dir qc          # .md .qmd .txt .docx .bib .ris .tex .json .csv .tsv
python $C verify refs.bib --strict                    # exit 1 unless every reference is VERIFIED
python $C verify --ids 10.1056/NEJMoa2021372 32865380 "The PRISMA 2020 statement: an updated guideline for reporting systematic reviews"
python $C resolve 10.1016/S0140-6736(20)31180-6       # one identifier -> status, flags, consensus record
python $C find "colchicine chronic coronary disease randomized" --year-from 2019 --bib references/library.bib
python $C claims manuscript.md --out-dir qc           # every sentence that carries a citation, as a checklist
python $C evidence qc/reference_audit.json --out-dir qc   # abstracts / open-access full text for claim checking
```

Running `verify` on the example manuscript in [`examples/manuscript-audit/`](examples/manuscript-audit/)
(real references plus one fabricated, one retracted and one Frankenstein citation planted on purpose):

```
citecheck v0.1.0 - 7 references from manuscript.md
ref_1          VERIFIED               Nidorf 2020 Colchicine in Patients with Chronic Coronary Dis
ref_2          VERIFIED               Tardif 2019 Efficacy and Safety of Low-Dose Colchicine after
ref_3          VERIFIED               Warsi 2024 Risk Factors of Acute Ischemic Stroke and Mortal
ref_4          RETRACTED              Mehra 2020 Hydroxychloroquine or chloroquine with or withou   [RETRACTED,EXPRESSION_OF_CONCERN,CORRECTION_PUBLISHED]
ref_5          VERIFIED               Page 2021 The PRISMA 2020 statement: an updated guideline
ref_6          NOT_FOUND              Smith 2023 Intravenous Turmeric and 30-Day Mortality in Car
ref_7          MISMATCH               Ebrahimi 2020 Colchicine in Patients with Chronic Coronary Dis   first author mismatch: cited 'Ebrahimi', source says 'Nidorf'
counts: MISMATCH=1, NOT_FOUND=1, RETRACTED=1, VERIFIED=4
submission_safe=False  blocking=['ref_4', 'ref_6', 'ref_7']
```

The full audit lands in `qc/reference_audit.json` (every lookup URL, every field comparison, the
consensus record) and `qc/reference_audit.md` (a table you can send to a co-author), with a one-paragraph
method statement you can paste into a cover letter.

## What the statuses mean

| Status | Meaning | What to do |
|---|---|---|
| `VERIFIED` | Identifiers resolve; title, first author, year and journal agree with the sources | Nothing |
| `VERIFIED_WITH_WARNINGS` | The paper exists; a minor field differs (year off by one, journal spelling, page numbers) | Fix the field |
| `MISMATCH` | The paper exists, but the title or first author differs, the year differs by more than one, or the DOI and PMID point to different papers | Treat as a wrong citation until corrected from the source record |
| `NOT_FOUND` | The DOI/PMID does not exist, or no database has a paper with this title | Treat as fabricated until you can produce the source |
| `RETRACTED` | Exists, but retracted or withdrawn | Do not cite as evidence |
| `UNVERIFIABLE` | Could not be checked (network failure, offline, nothing parseable) | Check by hand |

Flags add detail: `PREPRINT`, `PUBLISHED_VERSION_EXISTS`, `EXPRESSION_OF_CONCERN`, `CORRECTION_PUBLISHED`,
`TITLE_MATCHED` (no identifier was cited; matched by title plus author/year), `MULTIPLE_MATCHES` (several
works share the title; add the DOI), `IDENTIFIER_CONFLICT`. `confidence: high` means two or more
independent databases agreed.

## How it checks

1. Parse the reference (BibTeX, RIS, Vancouver/AMA, APA, pandoc, DOCX, LaTeX `\bibitem`, JSON, CSV).
2. Resolve identifiers in parallel sources: CrossRef and doi.org for the DOI; PubMed for the PMID; OpenAlex and
   Europe PMC for both. A DOI-only citation is enriched with its PubMed record via the PMID the other
   databases report.
3. Check that the DOI record and the PMID record are the same work.
4. Compare the cited title (fuzzy, subtitle-tolerant), first-author surname (particle-, hyphen- and
   accent-tolerant), year, journal (abbreviation-aware), volume and first page against every record found.
5. Collect retraction signals from four places (CrossRef `update-to` including Retraction Watch, OpenAlex
   `is_retracted`, PubMed `Retracted Publication` / `RetractionIn`, Europe PMC), and preprint signals
   (CrossRef `posted-content`, DOI prefixes, PubMed `Preprint` type).
6. For citations with no identifier, search all four databases by title and accept a candidate only when
   author and year also agree; use the journal to separate co-publications.

Details, thresholds and the audit schema: [`docs/how-it-works.md`](docs/how-it-works.md). The catalogue of
failure modes, each with the fixture that proves it is caught: [`docs/failure-modes.md`](docs/failure-modes.md).

## The writing workflow

`cite-safely` is the part that makes the audits short. While drafting:

- No citation is written from memory; `find-reference` locates and verifies it first.
- Anything not yet confirmed carries `[VERIFY]`. A reference that cannot be resolved carries
  `[NEEDS VERIFICATION]` and never ships. An open methodological choice carries `[DECIDE]`.
- Statements are labelled (a) what a source says, (b) your own data, (c) inference, when it matters.
- Before anything leaves the draft: `check-references`, then `check-claims`.

`check-claims` is deliberately a two-part design. The script extracts every cited sentence and fetches each
source's abstract or open-access full text (Europe PMC) into an evidence pack that states the **tier**
reached. Claude (or you) then judges each claim against the source's own words and must quote them. A
subgroup result checked against an abstract that does not mention it is `PARTIALLY-SUPPORTED
(abstract-only)`, never `SUPPORTED`. The worked example in
[`skills/check-claims/SKILL.md`](skills/check-claims/SKILL.md) uses the real LoDoCo2 abstract.

## Honest limits

- It verifies journal literature. Books, guidelines without DOIs, theses and web pages come back
  `UNVERIFIABLE` or `NOT_FOUND` and need a human.
- It does not read PDFs. Export the reference list or manuscript to text or DOCX first.
- Title-only matching is `medium` confidence by design. Add DOIs or PMIDs to your references.
- The first author is checked; a fabricated middle author passes. Volume and page differences are warnings.
- Claim checking needs a reader. The script fetches evidence and enforces the tier; it does not decide
  whether a sentence is supported.
- It says nothing about the quality of the evidence. A `VERIFIED` paper can still be the wrong paper to cite.
- It needs the network. Public APIs have rate limits; a 60-reference list takes a minute or two the first
  time and seconds after that thanks to the local cache.

## Privacy

Only reference identifiers and titles are sent, to public scholarly APIs (CrossRef, NCBI, OpenAlex,
Europe PMC). No manuscript text leaves your machine except the sentences you choose to check yourself.
A contact email is optional and is redacted, along with any API key, from every URL recorded in the
audit and evidence files, so `qc/` can be shared with co-authors.

## Prior art and thanks

- [medsci-skills](https://github.com/Aperivue/medsci-skills) (Aperivue, MIT) for the idea of an
  audit-only reference checker inside a research skill set and for documenting the first-author
  hallucination incident that motivated the Frankenstein checks here. This repository shares no code with it.
- [citation-check-skill](https://github.com/serenakeyitan/citation-check-skill),
  [VeriExCite](https://github.com/ykangw/VeriExCiting) and [CiteGuard](https://github.com/larrylot/citeguard)
  approach the same problem for general LLM output, PDFs and URL-cited reports respectively.
- [Retraction Watch](https://retractionwatch.com/), whose database reaches this tool through CrossRef.
- CrossRef, NCBI, OpenAlex and Europe PMC for keeping their APIs open.

## Contributing

Wrongly classified references are the most useful bug reports; see [CONTRIBUTING.md](CONTRIBUTING.md).
Run `python -m unittest discover -s tests` (offline) and `citecheck.py selftest` (live) before a pull request.

## Licence and citing

MIT. If this tool is part of your workflow, cite it via [`CITATION.cff`](CITATION.cff).

Built by [Talal Warsi](https://github.com/TW5897), an internal medicine doctor in the NHS doing cardiology
and clinical-AI research, because the alternative was checking sixty references by hand before every submission.
