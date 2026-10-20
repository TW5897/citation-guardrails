---
name: find-reference
description: >
  Find a real, verifiable source for a statement instead of citing from memory. Searches PubMed and
  OpenAlex for the user's query, verifies each candidate against CrossRef, PubMed, OpenAlex and
  Europe PMC, and returns ready-to-paste Vancouver strings and BibTeX entries stamped with the
  databases that confirmed them. Also resolves a DOI, PMID or title the user supplies. Use this
  whenever a sentence needs a citation, the user asks "what's the reference for", "find me a paper
  showing", "add a citation", "is there evidence that", or a reference came back NOT_FOUND from
  check-references. Use it before writing any citation into a draft, even when you think you remember
  the paper.
allowed-tools: Bash, Read, Write, Glob, Grep, WebFetch, WebSearch
---

# find-reference

Rule one: **a citation is never written from memory.** It is found in a database, verified by at
least one second database, and only then pasted. If this skill cannot find a source, the honest
output is "no verifiable source found", and the sentence gets a `[NEEDS VERIFICATION]` marker, not a
plausible-sounding reference.

## The script

This skill uses the `check-references` script, which lives in the sibling skill directory:
`../check-references/scripts/citecheck.py` relative to this file. Install both skills together.

```bash
python3 <skills-dir>/check-references/scripts/citecheck.py find "colchicine chronic coronary disease randomized" --max 5 --year-from 2019
python3 <skills-dir>/check-references/scripts/citecheck.py find "..." --bib references/library.bib     # append verified BibTeX
python3 <skills-dir>/check-references/scripts/citecheck.py resolve 10.1056/NEJMoa2021372               # check an identifier
python3 <skills-dir>/check-references/scripts/citecheck.py resolve "The PRISMA 2020 statement: an updated guideline for reporting systematic reviews"
```

On Windows use `python` or `py -3` instead of `python3`.

`find` runs a PubMed search (full PubMed syntax works: `"heart failure"[MeSH] AND colchicine[tiab]`),
an OpenAlex search, de-duplicates, then puts every candidate through the same multi-source
verification as `check-references`. Only candidates that come back `VERIFIED` are shown, each with
the databases that confirmed it and any `PREPRINT` / `RETRACTED` / `CORRECTION_PUBLISHED` flag.

## Procedure

1. **Pin down the claim.** Restate, in one line, exactly what the sentence asserts (population,
   exposure or intervention, outcome, direction, magnitude if stated). A reference must support that,
   not merely be on the topic.
2. **Build the query.** Start specific (trial acronym, drug + outcome, MeSH terms). Add `--year-from`
   or `--year-to` when the claim is about a period. Run `find`. If nothing comes back, broaden once;
   if still nothing, stop and report that.
3. **Show candidates to the user** as the script prints them (Vancouver string, confirming databases,
   flags). Do not rank by what you "know" about the papers; rank by fit to the claim as stated in the
   title and, if needed, the abstract.
4. **Confirm support before citing.** For the chosen candidate, fetch the abstract
   (`citecheck.py evidence` or `check-claims`) and quote the sentence that supports the claim. If the
   abstract does not contain it, say the support is unconfirmed and keep a `[VERIFY]` marker on the
   sentence. A paper that is merely about the topic is not a source for a specific number.
5. **Emit the citation** in the manuscript's existing style, from the verified record only, and add
   the DOI or PMID so later audits are unambiguous. With `--bib`, the BibTeX entry carries
   `verified = {true}`, `verified_by = {...}` and `verified_on = {date}`.
6. **If the user supplies an identifier**, run `resolve` and show the status before using it. A
   supplied DOI can be wrong too.

## Do not

- Do not present a candidate that the script did not mark `VERIFIED` (or `VERIFIED_WITH_WARNINGS`).
- Do not invent a DOI, PMID, page range or author list to complete an entry the search did not return.
- Do not cite a preprint as if peer reviewed; keep the `PREPRINT` label visible.
- Do not cite a retracted paper as evidence.
- Do not keep broadening a search until something tangential appears. "No verifiable source found" is a valid, useful answer.
- Do not drop the `[VERIFY]` marker from a sentence until the source's own words have been checked.

## Related skills

- `check-references`: audit a whole reference list.
- `check-claims`: confirm the chosen paper actually supports the sentence.
- `cite-safely`: the drafting rules this skill serves.
