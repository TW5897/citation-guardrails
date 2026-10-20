# Changelog

## 0.1.0 - 2026-10-20

First public release.

- `check-references` skill and `citecheck.py` engine: multi-source verification (CrossRef, doi.org,
  PubMed, OpenAlex, Europe PMC; Semantic Scholar optional) with title / first-author / year / journal
  cross-checks, DOI-PMID consistency, retraction and preprint flags, title-only matching with
  co-publication disambiguation, JSON + Markdown audits, local response cache, live `selftest`.
- `check-claims` skill: `claims` extractor and `evidence` packs (abstract or open-access full text
  via Europe PMC) with a tiered verdict protocol.
- `find-reference` skill: `find` (PubMed + OpenAlex, every hit verified) and `resolve`.
- `cite-safely` skill: `[VERIFY]` / `[NEEDS VERIFICATION]` / `[DECIDE]` markers and provenance labels.
- Offline unit tests, GitHub Actions matrix (Linux/Windows, Python 3.10-3.14), plugin manifests.
