# Citation failure modes and how each is caught

Language models do not fail at citations in one way. Each row is a distinct mechanism, with the
check that catches it and the live fixture in `citecheck.py selftest` (or the offline unit test) that
proves the check works.

| # | Failure mode | How it happens | What catches it | Status / flag | Proof |
|---|---|---|---|---|---|
| 1 | **Fabricated DOI** | A DOI with the right prefix pattern (`10.1056/NEJMoa…`) that was never issued | CrossRef 404, DataCite 404, doi.org 404 | `NOT_FOUND` | selftest: fabricated DOI |
| 2 | **Fabricated PMID** | An eight-digit number that is not a PubMed record | PubMed efetch returns no article or book | `NOT_FOUND` | selftest: fabricated PMID |
| 3 | **Frankenstein citation: real DOI, invented authors** | The model remembers a real paper's DOI but produces a different author list | First-author surname compared with CrossRef / PubMed / OpenAlex / DataCite author lists; Unicode and Jr/3rd suffixes handled | `MISMATCH` | selftest: invented first author; Özdemir; Smith AB Jr |
| 4 | **Frankenstein citation: real DOI, wrong title** | A DOI from one trial attached to the title of another | Title similarity against the record behind the DOI | `MISMATCH` | selftest: COLCOT title on LoDoCo2 DOI |
| 5 | **Fabricated metadata on a DOI that is real but not in CrossRef** | Zenodo, arXiv, figshare DOIs resolve, so they look fine | DataCite supplies the record; fields are compared. If no database has metadata, the DOI is never VERIFIED | `MISMATCH` / `UNVERIFIABLE` + `DOI_RESOLVES_NO_METADATA` | selftest: Zenodo dataset DOI; unit test doi.org-only |
| 6 | **Wrong PMID that is somebody else's paper** | A real PMID, but for an unrelated article (including one hidden in a PubMed URL) | PMID record title/author compared with the cited ones; PMIDs are read from `pubmed.ncbi.nlm.nih.gov/…` URLs too | `MISMATCH` | selftest: wrong PMID |
| 7 | **DOI and PMID from two different papers** | Identifiers copied from two sources | DOI record and PMID record compared (shared PMID/DOI or title similarity) | `MISMATCH` + `IDENTIFIER_CONFLICT` | selftest: DOI and PMID conflict |
| 8 | **Erratum or correction DOI cited as the article** | The notice's DOI is adjacent to the paper's and shares its title | CrossRef `update-to` plus a notice-shaped title ("Correction to: …"); the subtitle-containment boost is disabled for notices | `MISMATCH` + `NOTICE_DOI_CITED` | selftest: erratum DOI |
| 9 | **Retracted paper cited as evidence** | Famous papers stay famous after retraction | CrossRef `updated-by` (incl. Retraction Watch), OpenAlex `is_retracted`, PubMed `Retracted Publication` / `RetractionIn`, Europe PMC pubType, publisher `RETRACTED:` label | `RETRACTED` | selftest: Mehra, Lancet 2020 |
| 10 | **A paper about retractions mistaken for a retracted one** | Its title starts with the word "Retracted" | Only the publisher's uppercase label form (`RETRACTED:`) counts as a title signal; everything else needs a structured signal | `VERIFIED` | selftest: Wang 2019, Sci Eng Ethics |
| 11 | **Preprint cited as peer-reviewed** | medRxiv/bioRxiv/SSRN/arXiv DOIs look like any other | CrossRef type `posted-content`, DOI prefix list, OpenAlex/PubMed/Europe PMC/DataCite preprint markers; `relation.is-preprint-of` gives the published DOI | `PREPRINT`, `PUBLISHED_VERSION_EXISTS` | selftest: preprint |
| 12 | **Title-only citation to a paper that does not exist** | Plausible title, plausible authors, no identifiers | Four title searches (PubMed, OpenAlex, Europe PMC, CrossRef) return nothing close | `NOT_FOUND` | selftest: TURMERIC-SHOCK |
| 13 | **Real title, hallucinated author or year** | The paper exists; the author list or year is invented | Title candidates found, but author/year disagree, so no candidate is accepted | `NOT_FOUND` + `TITLE_EXISTS_AUTHOR_YEAR_DIFFER` | unit test |
| 14 | **Bare title with nothing to confirm it** | A title and nothing else | Candidates found but the citation gives no author or year to corroborate: not accepted | `UNVERIFIABLE` + `TITLE_ONLY_UNCONFIRMED` | unit test |
| 15 | **Letter or comment that reuses a trial's title** | Correspondence in NEJM carries the article's title and a nearby DOI | Candidate rejected when its volume AND first page both differ from the cited ones; letter types deprioritised | the trial is matched, not the letter | selftest: 'et al.'-only list |
| 16 | **Co-publication ambiguity** | PRISMA 2020 appears in several journals with identical titles and authors | Candidates merged; cited journal breaks the tie; otherwise flagged | `MULTIPLE_MATCHES` | unit test; live: `resolve "The PRISMA 2020 statement…"` |
| 17 | **Web page or guideline landing page cited like a paper** | "WHO. Cardiovascular diseases fact sheet. https://…" | Recognised as a web resource (URL, no identifiers, no journal) and never matched to a paper by title | `UNVERIFIABLE` + `WEB_RESOURCE` | selftest: WHO fact sheet |
| 18 | **Year drift (online-first vs print)** | 2020 online, 2021 in print; the model picks one | Year compared with each source; ±1 is a warning, more is a mismatch (unless only one secondary source disagrees about a preprint/dataset) | `VERIFIED_WITH_WARNINGS` | selftest: year off by one |
| 19 | **Sister-journal errors** | BMJ Open cited as BMJ, Lancet Digital Health as Lancet | One-to-one abbreviation matching; a single-token name does not match a multi-token one | `VERIFIED_WITH_WARNINGS` | selftest: BMJ Open as BMJ |
| 20 | **Expression of concern or erratum ignored** | Author unaware of post-publication notices | CrossRef `updated-by`, PubMed `ExpressionOfConcernIn` / `ErratumIn` | `EXPRESSION_OF_CONCERN`, `CORRECTION_PUBLISHED` | selftest: Mehra fixture carries both |
| 21 | **Identifier mangled by copy-paste** | `?query=` or `#fragment` kept from a publisher URL; a SICI DOI cut at `<` | DOI cleaning strips URL noise; SICI DOIs are kept whole | `VERIFIED` | selftest: URL-embedded DOI; Newcombe 1998 |
| 22 | **Reference list shapes that hide references** | One reference per line, Word automatic numbering, one-line BibTeX entries, CSL-JSON keys | Per-line splitting, Word `w:numPr` labels, brace-balanced BibTeX, CSL-JSON aliases | each reference checked on its own | unit tests (`tests/test_audit_regressions.py`) |
| 23 | **Real paper, wrong claim** | "Colchicine reduced all-cause mortality [LoDoCo2]" — the paper is real, the claim is not in it | Not a metadata problem. `check-claims`: the `evidence` command fetches the abstract / open-access full text and the verdict must quote the source | `UNSUPPORTED` / `CONTRADICTED` / `PARTIALLY-SUPPORTED` | worked example in `skills/check-claims/SKILL.md` |
| 24 | **Misattribution** | The claim is true, but the cited paper is not where it comes from | `check-claims` finds the source silent; `find-reference` locates the real source | `MISATTRIBUTED` | - |
| 25 | **Abstract-level over-claiming** | A subgroup or secondary endpoint quoted "from" a paper when only the abstract was read | Evidence packs state the tier; a body-level detail checked at abstract tier can only be `PARTIALLY-SUPPORTED (abstract-only)` | - | - |
| 26 | **"Verified" when nothing was checked** | Offline run or network outage | `submission_safe` requires every reference to have been checked; exit code 4; the method statement says how many were checked | `UNVERIFIABLE`, `NOT CHECKED` in the report | unit test; CLI test |

## Why multi-source

Each database has blind spots: CrossRef has no PMIDs and no DataCite DOIs; PubMed omits most
non-biomedical journals; OpenAlex occasionally holds stray or merged records; Europe PMC lags for the
newest articles. Requiring a metadata record from at least one source, treating doi.org as the registry of
record for existence, and comparing fields across all that answered makes a single stale or incomplete
record far less likely to produce a wrong verdict, and it is why `confidence: high` means two or more
databases agreed.

## Known limitations

- Books, chapters, guideline PDFs, theses and web pages are `UNVERIFIABLE` or `NOT_FOUND` by design.
- Very short titles (fewer than three words) are not searched; add an identifier.
- Author checks compare the **first** author (plus an "is this person an author at all" tolerance). A
  fabricated middle author passes.
- A second DOI in the same reference (supplement, erratum) is recorded (`MULTIPLE_DOIS`) but not resolved.
- Page-range and volume checks are warnings, because publishers and databases disagree about them often.
- If a database has a corrupt record (it happens), the audit shows which source said what, so you can
  judge; the `lookups` block carries every URL.
