# Changelog

## 0.1.0 - 2026-10-20

First public release. Before release the tool went through a seven-lens adversarial audit (code
correctness, live misclassification hunt with 31 crafted citations, documentation accuracy, skill
quality, a clinician's first run on Windows, privacy, test coverage) and a second, independently
verified round on the fixed code (adversarial re-hunt with 30+ new cases, docs-vs-behaviour, a
clinician's first run in PowerShell); every confirmed finding is fixed here and covered by a
regression test or a live `selftest` fixture.

Second-round fixes: one-item and Lancet-style (`1 Author`, `1<TAB>Author`) lists; Word line breaks,
tabs and endnotes; curly apostrophes and dotted initials in surnames; non-English titles (PubMed
vernacular titles); book chapters (`In:`), Harvard/MLA quoted titles, NLM group authors after a
semicolon; sibling-paper Frankensteins (`DIFFERENT_WORK`); cited PMCIDs and second PMIDs resolved and
cross-checked; DOIs wrapped across lines; `evidence --ids` no longer overwrites the manuscript audit;
manuscript prose can no longer leak via a bare `Sources` heading or numbered body lists; `find`
ranks by relevance, shows every verified hit and never duplicates `.bib` entries; shell-split DOIs
are re-joined; unchecked rows are reported by cause; skill `allowed-tools` rules match the commands.

- `check-references` skill and `citecheck.py` engine: multi-source verification (CrossRef, doi.org
  registry check without following redirects, PubMed incl. Bookshelf records, OpenAlex, Europe PMC,
  DataCite for Zenodo/arXiv/dataset DOIs; Semantic Scholar optional) with title / first-author / year /
  journal cross-checks, DOI-PMID consistency, erratum-DOI detection, retraction and preprint flags,
  title-only matching that requires author or year corroboration and separates co-publications and
  letters, JSON + Markdown audits, redacted local response cache, 24-fixture live `selftest`.
- Never VERIFIED without a metadata record compared (a bare doi.org redirect is `UNVERIFIABLE`);
  `submission_safe` requires every reference to have been checked; exit code 4 when some were not;
  fail-fast when the network is unreachable.
- Parsers: brace-balanced BibTeX (one-line entries, @string), RIS, CSL-JSON, CSV/TSV with BOM,
  DOCX with Word auto-numbering and superscript cites, one-reference-per-line lists, BibTeX-generated
  `\bibitem`, UTF-16/cp1252 input, SICI DOIs, DOIs inside publisher URLs, PubMed URLs, group authors,
  non-ASCII and suffixed surnames; PDFs refused with instructions.
- `check-claims` skill: `claims` extractor and `evidence` packs (abstract or open-access full text
  via Europe PMC) with a tiered verdict protocol.
- `find-reference` skill: `find` (PubMed + OpenAlex, every hit verified) and `resolve`.
- `cite-safely` skill: `[VERIFY]` / `[NEEDS VERIFICATION]` / `[DECIDE]` markers and provenance labels.
- Offline unit tests, GitHub Actions matrix (Linux/Windows, Python 3.10-3.14), plugin manifests.
