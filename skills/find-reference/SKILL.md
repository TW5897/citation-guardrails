---
name: find-reference
description: >
  Find a real, verifiable source for a statement instead of citing from memory. Searches PubMed and
  OpenAlex for the user's query, verifies each candidate against CrossRef, PubMed, OpenAlex, Europe
  PMC and DataCite, shows only candidates confirmed by two or more databases, and returns
  ready-to-paste Vancouver strings and BibTeX entries stamped with the databases that confirmed them.
  Also resolves a DOI, PMID or title the user supplies. Use this whenever a sentence needs a citation,
  the user asks "what's the reference for", "find me a paper showing", "add a citation", "is there
  evidence that", or a reference came back NOT_FOUND from check-references. Use it before writing any
  citation into a draft, even when you think you remember the paper.
allowed-tools: Bash(python "${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py" *), Bash(python3 "${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py" *), Bash(py -3 "${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py" *), Read, Glob, Grep
---

# find-reference

Rule one: **a citation is never written from memory.** It is found in a database, confirmed by at
least one second database, checked against the sentence it will support, and only then pasted. If
this skill cannot find a source, the honest output is "no verifiable source found", and the sentence
gets a `[NEEDS VERIFICATION]` marker, not a plausible-sounding reference.

## The script

This skill uses the `check-references` script in the sibling skill directory:
`${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py`. If it is missing, stop and tell the
user to install `check-references` as well. On Windows use `python` or `py -3` instead of `python3`.
Quote any DOI that contains parentheses.

```bash
python "${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py" find "colchicine chronic coronary disease randomized" --max 5 --year-from 2019
python "${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py" find "..." --bib references/library.bib --pick 2      # write ONLY the chosen candidate as verified BibTeX
python "${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py" resolve "10.1056/NEJMoa2021372"                        # check an identifier the user supplied
python "${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py" resolve "The PRISMA 2020 statement: an updated guideline for reporting systematic reviews"
python "${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py" evidence --ids "10.1056/NEJMoa2021372" --out-dir qc    # abstract / open-access text for the chosen paper
```

`find` runs a PubMed search (relevance-ranked) (full PubMed syntax works: `"heart failure"[MeSH] AND colchicine[tiab]`)
and an OpenAlex search, de-duplicates, then puts every candidate through the same multi-source
verification as `check-references`. The output has three lists: **verified** candidates (confirmed
by two or more databases, not retracted), **single-source** candidates (seen in one database only;
not verified), and **retracted** papers that matched the query (excluded). Only verified candidates
can be written to `--bib`, and an entry whose DOI/PMID is already in the file is not duplicated.
`evidence --ids` writes its one-off audit to `qc/evidence/ids_audit.json` and merges into the evidence
index; it never touches the manuscript's `qc/reference_audit.json`.

## Procedure

1. **Pin down the claim.** Restate, in one line, exactly what the sentence asserts (population,
   exposure or intervention, outcome, direction, magnitude if stated). A reference must support that,
   not merely be on the topic.
2. **Build the query.** Start specific (trial acronym, drug + outcome, MeSH terms). Add `--year-from`
   or `--year-to` when the claim is about a period. Run `find`. If nothing comes back, broaden once;
   if still nothing, stop and report that.
3. **Show candidates to the user** as the script prints them (Vancouver string, confirming databases,
   flags). Do not rank by what you "know" about the papers; rank by fit to the claim as stated in the
   title and, if needed, the abstract. Never promote a single-source candidate to "verified".
4. **Confirm support before citing.** For the chosen candidate, run `evidence --ids <DOI or PMID>` and
   quote the sentence that supports the claim. If the abstract does not contain it, say the support is
   unconfirmed and keep a `[VERIFY]` marker on the sentence. A paper that is merely about the topic is
   not a source for a specific number.
5. **Emit the citation** in the manuscript's existing style, from the verified record only, and add
   the DOI or PMID so later audits are unambiguous. With `--bib --pick N`, the BibTeX entry carries
   `verified = {true}`, `verified_by = {...}` and `verified_on = {date}`; write only the paper you
   checked, not the whole candidate list.
6. **If the user supplies an identifier**, run `resolve` and show the status before using it. A
   supplied DOI can be wrong too.

## Do not

- Do not present a candidate from the single-source or retracted lists as a usable reference.
- Do not invent a DOI, PMID, page range or author list to complete an entry the search did not return.
- Do not cite a preprint as if peer reviewed; keep the `PREPRINT` label visible.
- Do not cite a retracted paper as evidence.
- Do not keep broadening a search until something tangential appears. "No verifiable source found" is a valid, useful answer.
- Do not drop the `[VERIFY]` marker from a sentence until the source's own words have been checked.
- Do not write candidates to the user's `.bib` before support has been confirmed; use `--pick`.

## Related skills

- `check-references`: audit a whole reference list.
- `check-claims`: confirm the chosen paper actually supports the sentence.
- `cite-safely`: the drafting rules this skill serves.
