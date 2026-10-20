# How verification works

`citecheck.py` is one standard-library Python file. Nothing is decided from text patterns alone; every
status traces to a public database record, and the audit JSON keeps the URL of each lookup.

## Pipeline

```
input file ──parse──> Ref{title, first_author, year, journal, volume, pages, doi, pmid}
                              │
            ┌─────────────────┼──────────────────────┐
         has DOI           has PMID              neither
            │                 │                      │
   CrossRef works/{doi}   PubMed efetch        PubMed [Title] search
   doi.org HEAD           Europe PMC EXT_ID     OpenAlex title.search
   OpenAlex works/doi     OpenAlex works/pmid   Europe PMC TITLE:
   Europe PMC DOI:                              CrossRef query.bibliographic
   (Semantic Scholar, optional)                        │
            │                 │             candidates scored on title similarity;
            └────────┬────────┘             accepted only if author AND year also agree
                     │                      (journal breaks ties between co-publications)
         DOI record and PMID record must be the same work (IDENTIFIER_CONFLICT otherwise)
                     │
         field cross-check against every record found:
           title similarity, first-author surname, year, journal, volume, first page
                     │
         flags: RETRACTED / EXPRESSION_OF_CONCERN / CORRECTION_PUBLISHED / PREPRINT /
                PUBLISHED_VERSION_EXISTS / TITLE_MATCHED / MULTIPLE_MATCHES / IDENTIFIER_CONFLICT
                     │
         status + confidence + consensus record  ──>  qc/reference_audit.json, qc/reference_audit.md
```

## Sources and what each contributes

| Source | Endpoint | Used for |
|---|---|---|
| CrossRef | `api.crossref.org/works/{doi}`; `works?query.bibliographic=` | DOI registry record: title, authors, year, journal, type (`posted-content` = preprint), `update-to` / `updated-by` (retractions, expressions of concern, corrections, including Retraction Watch entries), `relation.is-preprint-of` |
| doi.org | `HEAD https://doi.org/{doi}` | Does the DOI resolve at all |
| PubMed | E-utilities `efetch` (XML) and `esearch` | Authoritative biomedical record: title, authors, journal, year, `PublicationType` (`Retracted Publication`, `Preprint`), `CommentsCorrections` (`RetractionIn`, `ExpressionOfConcernIn`, `ErratumIn`), PMCID, abstract |
| OpenAlex | `api.openalex.org/works/https://doi.org/{doi}`, `works/pmid:{pmid}`, `works?filter=title.search:` | Second independent record, `is_retracted`, PMID for DOI-only citations, abstract (inverted index), good title search |
| Europe PMC | `ebi.ac.uk/europepmc/webservices/rest/search`, `/{PMCID}/fullTextXML` | Third record, pubType `Retracted Publication`, preprints (source `PPR`), abstracts, open-access full text |
| Semantic Scholar | `api.semanticscholar.org/graph/v1/paper/DOI:{doi}` | Optional fourth record (`--sources ...,semanticscholar`); public rate limit is shared and tight, so it is off by default |

A DOI-only citation is enriched with its PubMed record via the PMID that OpenAlex or Europe PMC report,
so retraction flags from PubMed apply even when the author cited only a DOI.

## Decision rules

- **NOT_FOUND** needs at least one source to have answered "no such record" (HTTP 404 or empty result)
  with no source saying yes. If every source failed (timeouts, 5xx), the status is **UNVERIFIABLE**,
  never NOT_FOUND.
- **Title-only** citations: candidates from all four title searches are merged by DOI/PMID and scored
  by title similarity (normalised `difflib` ratio with containment and token-overlap boosts). A candidate
  is accepted only if similarity ≥ 0.92 **and** the cited first author matches (when both are known)
  **and** the year is within one. If several distinct works pass (co-publications, letters that reuse the
  paper's title), the cited journal decides; otherwise `MULTIPLE_MATCHES` is flagged. A strong title match
  whose author/year disagree yields `TITLE_EXISTS_AUTHOR_YEAR_DIFFER` and NOT_FOUND, so a hallucinated
  author on a real title is not quietly accepted.
- **Field checks** only fail when the cited value disagrees with *every* record found (sources differ in
  punctuation, subtitles and journal abbreviations). Title similarity < 0.80, first-author mismatch
  (tolerant to particles, hyphens, diacritics) and year off by more than one are **major** → MISMATCH.
  Title 0.80–0.92, year off by one, journal, volume and first-page differences are **warnings** →
  VERIFIED_WITH_WARNINGS.
- **RETRACTED** overrides everything once any source flags a retraction or withdrawal.
- **Confidence** `high` = two or more databases confirmed the same record (doi.org does not count);
  `medium` = one database, or matched by title; `low` = parsed but barely checkable.

## Rate limits, caching, privacy

- Per-host minimum intervals (NCBI 0.35 s, or 0.11 s with `NCBI_API_KEY`; CrossRef and OpenAlex 0.12 s;
  Europe PMC 0.25 s; Semantic Scholar 1.2 s), exponential back-off on 429/5xx, `Retry-After` honoured.
- Responses (200/404) are cached for 7 days in `qc/.citecheck-cache.json` (or a per-user cache dir for
  one-off commands), so re-running after fixing one reference costs one lookup, not sixty.
- Only identifiers and titles leave your machine, to public scholarly APIs. A contact email
  (`--email` / `CITECHECK_EMAIL`) is optional and only used for the polite pools CrossRef, OpenAlex and
  NCBI ask for. Emails and API keys are redacted from every URL written to audit or evidence files.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No blocking row (and, with `--strict`, every row VERIFIED) |
| 1 | At least one NOT_FOUND / MISMATCH / RETRACTED (or any non-VERIFIED row under `--strict`) |
| 2 | Usage or input error |
| 3 | No references detected |

## Audit JSON (abridged)

```json
{"tool": "citecheck", "version": "0.1.0", "generated": "2026-10-20T00:00:00Z", "source": "manuscript.md",
 "sources_queried": ["crossref", "pubmed", "openalex", "europepmc"],
 "total_references": 6, "counts": {"VERIFIED": 4, "RETRACTED": 1, "NOT_FOUND": 1},
 "flags": {"RETRACTED": 1, "EXPRESSION_OF_CONCERN": 1, "CORRECTION_PUBLISHED": 1},
 "blocking_ref_ids": ["ref_4", "ref_6"], "needs_review_ref_ids": [], "submission_safe": false,
 "method_statement": "All 6 references were checked on ... against crossref, pubmed, openalex, europepmc (...)",
 "records": [{"ref_id": "ref_4", "status": "RETRACTED", "confidence": "high", "found_by": "doi",
              "flags": ["RETRACTED", "EXPRESSION_OF_CONCERN", "CORRECTION_PUBLISHED"],
              "sources_confirming": ["crossref", "europepmc", "openalex", "pubmed"],
              "issues": ["this work has been RETRACTED; do not cite it as evidence"],
              "notes": ["Retraction (CrossRef update-to, DOI 10.1016/s0140-6736(20)31324-6)", "PubMed PublicationType: Retracted Publication", "..."],
              "cited": {"title": "...", "first_author": "Mehra", "year": "2020", "doi": "10.1016/s0140-6736(20)31180-6", "raw": "..."},
              "resolved": {"title": "RETRACTED: ...", "authors_full": ["Mehra, Mandeep R", "..."], "pmid": "32450107", "pmcid": "PMC7255293", "retracted": true},
              "checks": [{"field": "title", "cited": "...", "source_value": "...", "source": "pubmed", "outcome": "match", "detail": "similarity 1.0"}],
              "lookups": [{"source": "crossref", "query": "doi:...", "url": "https://api.crossref.org/works/...", "ok": true, "found": true, "retrieved": "..."}]}]}
```

## What it cannot do

- Read PDFs. Export the reference list or the manuscript to text first.
- Verify books, book chapters, guidelines without DOIs, grey literature or web pages. These come back
  UNVERIFIABLE or NOT_FOUND and need a human.
- Tell you whether the paper supports your sentence. That is `check-claims`, which needs a reader (you
  or the model) to compare the claim with the abstract or full text the `evidence` command fetches.
- Judge the quality of the evidence. A VERIFIED retracted-free RCT can still be the wrong paper to cite.
- Work offline. `--offline` parses only; every row is UNVERIFIABLE.
