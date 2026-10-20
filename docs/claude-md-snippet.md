# CLAUDE.md snippet

Paste this into your global `~/.claude/CLAUDE.md` (or a project `CLAUDE.md`) so the discipline applies
in every session, whether or not a skill is invoked. Edit the wording to taste; keep the markers.

```markdown
## Citation and claim accuracy (non-negotiable)

- Never invent, approximate or "remember" a citation. Every reference must be verified this
  session against a live database (PubMed, CrossRef, OpenAlex, Europe PMC) with a real DOI or
  PMID and matching title, first author, journal and year. If you cannot verify it, say so and
  mark it `[NEEDS VERIFICATION]`. Never ship it.
- Never state a statistic, effect size, endpoint or study finding that cannot be traced to a
  source. Distinguish (a) what a cited paper says, (b) my own data, (c) your inference. Label them.
- If a claim's detail (subgroup, secondary endpoint, exact number) is not in the abstract, say it
  was verified only at abstract level. Do not imply the full text was read if it was not.
- Report uncertainty plainly. "I could not confirm this" beats a confident wrong number.

Working markers, used inline in every draft:
- `[VERIFY]`: a citation, number or claim not yet independently confirmed this session.
- `[NEEDS VERIFICATION]`: a reference that could not be resolved at all. Surface it; never ship it.
- `[DECIDE]`: an open methodological or editorial choice that is mine. Flag it; do not pick silently.

Before anything is submitted, circulated or posted, run in this order:
1. `check-references` (do the references exist and match their sources?)
2. `check-claims` (does each cited sentence say what the source says?)
Do not call a draft clean while any `NOT_FOUND`, `MISMATCH`, `RETRACTED`, `UNVERIFIABLE`,
`CONTRADICTED`, `MISATTRIBUTED` or `UNSUPPORTED` row remains.
```

Optional, for projects where references are managed in a `.bib`:

```markdown
References live in `references/library.bib`. Never edit a reference by hand to satisfy an audit;
re-run `find-reference` and replace the entry with the verified record.
```
