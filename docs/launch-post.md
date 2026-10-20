# Launch thread for X (draft, not posted)

Format follows `x-growth-plan/x-growth-roadmap.md` §6 "build-in-public update" and the batch-02 rule that
every post fits 280 characters. Character counts are X-weighted (URLs = 23). Image specs follow each post.
Provenance: (b) = this repository's own behaviour, verified by running it on 2026-10-20; (c) = framing.

Replace `LINK` with the GitHub URL once the repo is public. `[DECIDE]` items at the end.

---

**Post 1 (hook + image)**

> I write papers with AI. The most dangerous thing it does is not inventing a reference. It is pairing a real DOI with the wrong authors, or a real paper with a number it never reported.
>
> So I built guardrails and open-sourced them. Thread.

Chars: 238

Image: screenshot of `citecheck.py selftest` output, cropped to the 13 PASS lines. Alt text: "Terminal output listing thirteen citation test cases, each marked PASS: real DOI, fabricated DOI, Frankenstein citation with invented author, retracted paper, preprint, title-only fabricated paper."

**Post 2**

> What it checks, for every reference:
>
> - does the DOI/PMID exist (CrossRef, PubMed, OpenAlex, Europe PMC)
> - do the cited title, first author, year and journal match the record
> - do the DOI and PMID point to the same paper
> - is it retracted
> - is it a preprint

Chars: 257

**Post 3 (the Frankenstein case, image)**

> The case that worries me most: a correct DOI with an invented first author. Looks perfect. Resolves fine.
>
> The tool compares the cited surname with the author list behind the DOI and flags MISMATCH: cited 'Ebrahimi', source says 'Nidorf'.

Chars: 238

Image: cropped `reference_audit.md` rows for ref_7 (MISMATCH) and ref_4 (RETRACTED) from `examples/manuscript-audit/output/`. Alt text: "Two rows of an audit table: a MISMATCH row with the note first author mismatch, cited Ebrahimi, source says Nidorf; and a RETRACTED row for a 2020 Lancet paper."

**Post 4 (claims)**

> Existence is the easy half. The hard half: a real, correctly cited trial quoted for a result it never reported.
>
> So the second skill pulls each paper's abstract or open-access full text and makes the model quote the source before it can call a claim SUPPORTED.

Chars: 260

**Post 5 (markers)**

> The writing discipline that makes audits short:
>
> [VERIFY] on anything not yet confirmed this session
> [NEEDS VERIFICATION] on a reference that cannot be resolved. It never ships
> [DECIDE] on a methodological fork that is the author's call
>
> Nothing unmarked is unverified.

Chars: 269

**Post 6 (honest limits + link)**

> Limits, stated plainly: journal literature only, no PDFs, title-only matches are medium confidence, and it checks the first author, not the whole list.
>
> Standard-library Python, no API keys, MIT. Works as a Claude Code plugin or a plain CLI.
>
> LINK

Chars: 266

**Post 7 (ask)**

> If you have a reference that this tool classifies wrongly, that is the most useful thing you can send me. Open an issue with the citation text and the status you expected.

Chars: 171

---

## Checks before posting

- `[VERIFY]` the repository is public and `LINK` resolves.
- `[VERIFY]` the two screenshots were taken from the committed example and the selftest on the day of posting (API records change; the thread must match what a reader will see).
- `[DECIDE]` whether post 1 says "I write papers with AI" or "I use AI when writing papers"; the first is punchier, the second is more accurate to how you work.
- `[DECIDE]` whether to credit the prior-art repos by handle in a reply (medsci-skills, citation-check-skill, VeriExCite, CiteGuard). Recommended: yes, one reply, names only unless they are on X.
- GMC check (roadmap §4): no patient data, no clinical advice, no misrepresentation of role. The thread describes a research tool. Clear.
- Conflict line: none to declare for this tool.
