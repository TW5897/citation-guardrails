# How verification works

`citecheck.py` is one standard-library Python file. Nothing is decided from text patterns alone; every
status traces to a public database record, and the audit JSON keeps the URL of each lookup.

## Pipeline

```
input file ──parse──> Ref{title, first_author, year, journal, volume, pages, doi, pmid, pmcid, url}
                              │
            ┌─────────────────┼──────────────────────┐
         has DOI           has PMID              neither
            │                 │                      │
   CrossRef works/{doi}   PubMed efetch        PubMed [Title] search
   DataCite dois/{doi}    (articles + books)   OpenAlex title.search
   doi.org HEAD (no       Europe PMC EXT_ID    Europe PMC TITLE:
     redirect followed)   OpenAlex works/pmid  CrossRef query.bibliographic
   OpenAlex works/doi                                  │
   Europe PMC DOI:                          candidates merged by DOI/PMID and scored on title similarity;
   (Semantic Scholar, optional)             accepted only if the cited author OR year also agrees, no
            │                 │             field contradicts, and volume/pages do not point elsewhere
            └────────┬────────┘             (journal breaks ties between co-publications)
                     │
         DOI record and PMID record must be the same work (IDENTIFIER_CONFLICT otherwise)
                     │
         field cross-check against every record WITH METADATA (a doi.org redirect compares nothing):
           title similarity, first-author surname, year, journal, volume, first page
                     │
         flags: RETRACTED / RETRACTION_HINT / EXPRESSION_OF_CONCERN / CORRECTION_PUBLISHED / NOTICE_DOI_CITED /
                PREPRINT / PUBLISHED_VERSION_EXISTS / TITLE_MATCHED / MULTIPLE_MATCHES / TITLE_ONLY_UNCONFIRMED /
                IDENTIFIER_CONFLICT / IDENTIFIER_WRONG_BUT_TITLE_EXISTS / TITLE_EXISTS_AUTHOR_YEAR_DIFFER /
                DOI_RESOLVES_NO_METADATA / WEB_RESOURCE / AUTHOR_NOT_CHECKED / FIELDS_NOT_CHECKED /
                SINGLE_SOURCE_CONFLICT / MULTIPLE_DOIS / PMCID_MISMATCH
                     │
         status + confidence + consensus record  ──>  qc/reference_audit.json, qc/reference_audit.md
```

## Sources and what each contributes

| Source | Endpoint | Used for |
|---|---|---|
| CrossRef | `api.crossref.org/works/{doi}`; `works?query.bibliographic=` | DOI registry record: title, authors, year, journal, type (`posted-content` = preprint), `updated-by` (retractions, expressions of concern, corrections, including Retraction Watch entries with `source: retraction-watch`), `update-to` (used with the title to recognise a record that is itself a correction/erratum notice), `relation.is-preprint-of` |
| DataCite | `api.datacite.org/dois/{doi}` | Metadata for DOIs CrossRef does not register: Zenodo, arXiv (`10.48550`), figshare, Dryad, OSF. Queried when CrossRef has no record or the prefix is a known DataCite prefix |
| doi.org | `HEAD https://doi.org/{doi}`, redirects **not** followed | Registry of record for every agency: a 30x means the DOI exists; a 404 means it does not, whatever a secondary index says. The redirect target host is recorded, never visited |
| PubMed | E-utilities `efetch` (XML, `PubmedArticle` and `PubmedBookArticle`) and `esearch` | Authoritative biomedical record: title, authors, journal, year, `PublicationType` (`Retracted Publication`, `Preprint`, `Published Erratum`), `CommentsCorrections` (`RetractionIn`, `ExpressionOfConcernIn`, `ErratumIn`), PMCID, abstract |
| OpenAlex | `api.openalex.org/works/https://doi.org/{doi}`, `works/pmid:{pmid}`, `works?filter=title.search:` | Second independent record, `is_retracted`, PMID for DOI-only citations, abstract (inverted index), good title search. Optional `OPENALEX_API_KEY` |
| Europe PMC | `ebi.ac.uk/europepmc/webservices/rest/search`, `/{PMCID}/fullTextXML` | Third record, pubType `Retracted Publication`, preprints (source `PPR`), abstracts, open-access full text with its `<license>` |
| Semantic Scholar | `api.semanticscholar.org/graph/v1/paper/DOI:{doi}` | Optional fourth record (`--sources ...,semanticscholar`); public rate limit is shared and tight, so off by default |

A DOI-only citation is enriched with its PubMed record via the PMID that OpenAlex or Europe PMC report,
so retraction flags from PubMed apply even when the author cited only a DOI.

## Decision rules

- **Nothing is VERIFIED without a compared record.** `found` records are split into those that carry
  metadata (title/author/year) and the bare doi.org redirect. If only doi.org answered, the status is
  **UNVERIFIABLE** with `DOI_RESOLVES_NO_METADATA`: the DOI exists, but nothing was compared.
- **NOT_FOUND** needs at least one source to have answered "no such record" (HTTP 404 or empty result)
  with no metadata source saying yes. A doi.org 404 together with CrossRef/DataCite 404 is authoritative
  and overrides a stray OpenAlex record. If every source failed (timeouts, 5xx), the status is
  **UNVERIFIABLE**, never NOT_FOUND. On the title-only path, NOT_FOUND needs three sources to have
  answered, or two with none failed.
- **Title-only** citations: candidates from the four title searches are merged by DOI/PMID and scored by
  title similarity (normalised `difflib` ratio, with containment and token-overlap boosts that are
  disabled when the longer title is a notice such as "Correction to: ..."). A candidate is accepted only if
  similarity ≥ 0.92, **the cited first author or the cited year agrees** (both must be present to count),
  neither contradicts, and the record's volume and first page do not both differ from the cited ones (that
  is a different work, typically a letter reusing the paper's title). Journal-level records and letters/
  comments (when the cited volume/pages do not match) are skipped. If several distinct works remain
  (co-publications), the cited journal decides; otherwise `MULTIPLE_MATCHES`. A strong title match whose
  author/year disagree yields `TITLE_EXISTS_AUTHOR_YEAR_DIFFER` and NOT_FOUND. A bare title with no author
  and no year cannot be confirmed: `TITLE_ONLY_UNCONFIRMED`, UNVERIFIABLE, candidates listed.
- **Web resources** (URL, no DOI/PMID, no journal) are UNVERIFIABLE with `WEB_RESOURCE` and are never
  matched to a paper by title. The URL is not opened.
- **Field checks** only fail when the cited value disagrees with *every* record found. Title similarity
  < 0.80, first-author mismatch (tolerant to particles, hyphens, diacritics, Jr/3rd suffixes), year off by
  more than one and a cited DOI that is a correction/erratum notice (`NOTICE_DOI_CITED`) are **major** →
  MISMATCH. Title 0.80–0.92, year off by one, a year disagreement from a single secondary source about a
  preprint or dataset (`SINGLE_SOURCE_CONFLICT`), journal, volume and first-page differences, and an
  author block the parser could not read (`AUTHOR_NOT_CHECKED`) are **warnings** → VERIFIED_WITH_WARNINGS.
  Journal matching is abbreviation-aware but one-to-one: `N Engl J Med` = `The New England journal of
  medicine`, while `BMJ` ≠ `BMJ Open`.
- **RETRACTED** needs a strong signal: CrossRef `updated-by` retraction, the publisher's `RETRACTED:`
  title label, OpenAlex `is_retracted`, PubMed `Retracted Publication`/`RetractionIn`, or Europe PMC. A
  CrossRef `update-to` retraction link alone is a `RETRACTION_HINT`, not a verdict. A paper whose title
  merely begins with the word "Retracted" is not retracted.
- **Confidence** `high` = two or more databases returned the record (doi.org never counts); `medium` = one
  (title-only matches are capped at medium); `low` = none. For NOT_FOUND it counts the databases that
  answered definitively.

## Rate limits, caching, privacy

- Per-host minimum intervals: NCBI 0.35 s (0.11 s with `NCBI_API_KEY`); CrossRef 0.25 s without an email,
  0.12 s with one (public pool is 5 requests/s, polite pool needs a `mailto`); OpenAlex 0.12 s (keyless use
  is metered by a daily budget; `OPENALEX_API_KEY` is passed when set); Europe PMC and DataCite 0.25 s;
  Semantic Scholar 1.2 s. Exponential back-off on 429/5xx, `Retry-After` honoured; transport failures
  retried once so a dead network fails fast.
- Responses are cached: 200s for 7 days, 404/410 for 24 hours (a new DOI or a fresh retraction should not
  stay hidden for a week). The cache lives in `qc/.citecheck-cache.json` for project runs and in a per-user
  cache directory for one-off commands. Entries with future timestamps or missing fields are ignored, the
  tool version is stored with each entry, publishers' full text is never cached, and every cached URL is
  redacted. The method statement says how many answers came from the cache; use `--no-cache` for the final
  pre-submission run.
- Only identifiers and the parsed title/author/year leave your machine, to the hosts above. The contact
  email goes to CrossRef (User-Agent) and to NCBI and OpenAlex (query parameter), never to doi.org,
  Europe PMC or publishers; emails and API keys are redacted from every URL written to audit, evidence and
  cache files. Audit files record input paths relative to the working directory, not your home folder.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every reference checked, no blocking row (and, with `--strict`, every row VERIFIED) |
| 1 | At least one NOT_FOUND / MISMATCH / RETRACTED (or any non-VERIFIED row under `--strict`) |
| 2 | Usage or input error (unknown source, PDF or binary input, unreadable .docx/.json) |
| 3 | No references detected |
| 4 | No blocking row, but some references could not be checked (network, offline, web resource, DOI without metadata) |

## Audit JSON (abridged)

```json
{"tool": "citecheck", "version": "0.1.0", "generated": "2026-10-20T00:00:00Z", "source": "examples/manuscript-audit/manuscript.md",
 "sources_queried": ["crossref", "pubmed", "openalex", "europepmc", "datacite"], "offline": false,
 "total_references": 7, "checked": 7, "unchecked": 0,
 "counts": {"VERIFIED": 4, "RETRACTED": 1, "NOT_FOUND": 1, "MISMATCH": 1},
 "flags": {"RETRACTED": 1, "EXPRESSION_OF_CONCERN": 1, "CORRECTION_PUBLISHED": 1},
 "blocking_ref_ids": ["ref_4", "ref_6", "ref_7"], "unchecked_ref_ids": [], "needs_review_ref_ids": [], "submission_safe": false,
 "method_statement": "7 of 7 references were checked on ... against crossref, pubmed, openalex, europepmc, datacite (...)",
 "records": [{"ref_id": "ref_4", "status": "RETRACTED", "confidence": "high", "found_by": "doi",
              "flags": ["RETRACTED", "EXPRESSION_OF_CONCERN", "CORRECTION_PUBLISHED"],
              "sources_confirming": ["crossref", "europepmc", "openalex", "pubmed"],
              "issues": ["this work has been RETRACTED; do not cite it as evidence"],
              "notes": ["Retraction (CrossRef updated-by, source retraction-watch, DOI 10.1016/s0140-6736(20)31324-6)", "PubMed PublicationType: Retracted Publication", "..."],
              "cited": {"title": "...", "first_author": "Mehra", "year": "2020", "doi": "10.1016/s0140-6736(20)31180-6", "raw": "..."},
              "resolved": {"title": "RETRACTED: ...", "authors_full": ["Mehra, Mandeep R", "..."], "pmid": "32450107", "pmcid": "PMC7255293", "retracted": true},
              "checks": [{"field": "title", "cited": "...", "source_value": "...", "source": "pubmed", "outcome": "match", "detail": "similarity 1.0"}],
              "lookups": [{"source": "crossref", "query": "doi:...", "url": "https://api.crossref.org/works/...", "ok": true, "found": true, "retrieved": "..."}]}]}
```

## What it cannot do

- Read PDFs. Export the reference list or the manuscript to DOCX or text first.
- Verify books, book chapters, guidelines without DOIs, grey literature or web pages. These come back
  UNVERIFIABLE or NOT_FOUND and need a human; cited URLs are not opened.
- Check every author: the first author is compared, plus a tolerance for the cited name being elsewhere
  in the author list. A fabricated middle author passes.
- Tell you whether the paper supports your sentence. That is `check-claims`, which needs a reader (you
  or the model) to compare the claim with the abstract or full text the `evidence` command fetches.
- Judge the quality of the evidence. A VERIFIED, retraction-free RCT can still be the wrong paper to cite.
- Work offline. `--offline` parses only; every row is UNVERIFIABLE and the report says so.
