# Citation failure modes and how each is caught

Language models do not fail at citations in one way. Each row is a distinct mechanism, with the
check that catches it and the live fixture in `citecheck.py selftest` that proves the check works.

| # | Failure mode | How it happens | What catches it | Status / flag | Self-test fixture |
|---|---|---|---|---|---|
| 1 | **Fabricated DOI** | A DOI with the right prefix pattern (`10.1056/NEJMoa…`) that was never issued | CrossRef 404, doi.org 404, OpenAlex 404 | `NOT_FOUND` | fabricated DOI |
| 2 | **Fabricated PMID** | An eight-digit number that is not a PubMed record | PubMed efetch returns no article | `NOT_FOUND` | fabricated PMID |
| 3 | **Frankenstein citation: real DOI, invented authors** | The model remembers a real paper's DOI but produces a different author list (often from a related paper) | First-author surname compared with CrossRef / PubMed / OpenAlex author lists | `MISMATCH` | Frankenstein: real DOI, invented first author |
| 4 | **Frankenstein citation: real DOI, wrong title** | A DOI from one trial attached to the title of another in the same field | Title similarity against the record behind the DOI | `MISMATCH` | Frankenstein: real DOI, wrong title |
| 5 | **Wrong PMID that is somebody else's paper** | A real PMID, but for an unrelated article | PMID record title/author compared with the cited ones | `MISMATCH` | wrong PMID that points to an unrelated real paper |
| 6 | **DOI and PMID from two different papers** | Identifiers copied from two sources | DOI record and PMID record compared (shared PMID/DOI or title similarity) | `MISMATCH` + `IDENTIFIER_CONFLICT` | DOI and PMID from two different papers |
| 7 | **Retracted paper cited as evidence** | Famous papers stay famous after retraction | CrossRef `update-to` (incl. Retraction Watch), OpenAlex `is_retracted`, PubMed `Retracted Publication` / `RetractionIn`, Europe PMC pubType | `RETRACTED` | retracted paper (Mehra, Lancet 2020) |
| 8 | **Preprint cited as peer-reviewed** | medRxiv/bioRxiv/SSRN/arXiv DOIs look like any other | CrossRef type `posted-content`, DOI prefix list, OpenAlex/PubMed/Europe PMC preprint markers; `relation.is-preprint-of` gives the published DOI | `PREPRINT`, `PUBLISHED_VERSION_EXISTS` | preprint cited |
| 9 | **Title-only citation to a paper that does not exist** | Plausible title, plausible authors, no identifiers | Four title searches (PubMed, OpenAlex, Europe PMC, CrossRef) return nothing close | `NOT_FOUND` | title only, fabricated paper |
| 10 | **Real title, hallucinated author or year** | The paper exists; the author list or year is invented | Title candidates found, but author/year disagree, so the candidate is refused | `NOT_FOUND` + `TITLE_EXISTS_AUTHOR_YEAR_DIFFER` | (unit test) |
| 11 | **Co-publication ambiguity** | Statements like PRISMA 2020 appear in several journals with identical titles and authors | Candidates merged; cited journal breaks the tie; otherwise flagged | `MULTIPLE_MATCHES` | (unit test; live example: `resolve "The PRISMA 2020 statement…"`) |
| 12 | **Year drift (online-first vs print)** | 2020 online, 2021 in print; the model picks one | Year compared with each source; ±1 is a warning, more is a mismatch | `VERIFIED_WITH_WARNINGS` | year off by one |
| 13 | **Journal name / abbreviation errors** | Wrong journal attached to a real paper | Journal compared token-wise with full and ISO-abbreviated names | `VERIFIED_WITH_WARNINGS` | (unit test) |
| 14 | **Expression of concern or erratum ignored** | Author unaware of post-publication notices | CrossRef `updated-by`, PubMed `ExpressionOfConcernIn` / `ErratumIn` | `EXPRESSION_OF_CONCERN`, `CORRECTION_PUBLISHED` | retracted paper fixture also carries these |
| 15 | **Real paper, wrong claim** | "Colchicine reduced all-cause mortality [LoDoCo2]" — the paper is real, the claim is not in it | Not a metadata problem. `check-claims`: the `evidence` command fetches the abstract / open-access full text and the verdict must quote the source | `UNSUPPORTED` / `CONTRADICTED` / `PARTIALLY-SUPPORTED` | worked example in `skills/check-claims/SKILL.md` |
| 16 | **Misattribution** | The claim is true, but the cited paper is not where it comes from | `check-claims` finds the source silent; `find-reference` locates the real source | `MISATTRIBUTED` | - |
| 17 | **Abstract-level over-claiming** | A subgroup or secondary endpoint quoted "from" a paper when only the abstract was read | Evidence packs state the tier; a body-level detail checked at abstract tier can only be `PARTIALLY-SUPPORTED (abstract-only)` | - | - |

## Why multi-source

Each database has blind spots: CrossRef has no PMIDs and thin author data for some publishers; PubMed
omits most non-biomedical journals and many conference papers; OpenAlex occasionally merges or
duplicates works; Europe PMC lags for the newest articles. Requiring a record from at least one source
and comparing fields across all that answered makes a single stale or incomplete record far less likely
to produce a wrong verdict, and it is why `confidence: high` means two or more databases agreed.

## Known limitations

- A reference that exists only as a book, chapter, guideline PDF, thesis or web page will be
  `UNVERIFIABLE` or `NOT_FOUND`. The tool is biased toward journal literature by design.
- Very short titles (fewer than three words) are not searched; add an identifier.
- Author-list checks compare the **first** author only (plus an "is this person an author at all"
  tolerance). A fabricated middle author passes.
- Page-range and volume checks are warnings, because publishers and databases disagree about them often.
- If a database has a corrupt record (it happens), the audit shows which source said what, so you can
  judge; the `lookups` block carries every URL.
