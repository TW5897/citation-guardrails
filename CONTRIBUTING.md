# Contributing

Bug reports with a reference that was classified wrongly are the most useful contribution. Please include:

- the citation text exactly as it was in your file (or the DOI/PMID),
- the status the tool gave and the status you expected,
- the `lookups` block for that reference from `qc/reference_audit.json` (it contains the API URLs and what each source returned; contact emails and API keys are redacted automatically).

## Ground rules

- Standard library only. No new dependencies; clinicians install this on locked-down machines.
- Every status change must be reproducible from a public database record. The tool never decides a reference is real from text patterns alone.
- Add a fixture to `tests/` for any parser change, and a live fixture to `SELFTEST_FIXTURES` for any new failure mode you catch.
- Run `python -m unittest discover -s tests -v` (offline) and `python skills/check-references/scripts/citecheck.py selftest` (network) before opening a pull request.
- Keep SKILL.md files under 500 lines and written as instructions, not essays.

## Adding a source

Implement `source_by_doi` / `source_by_pmid` / `source_title_search` functions returning `Hit`, register them in `Verifier.lookup_*`, add the host's polite interval to `HOST_INTERVAL`, and document the endpoint in `docs/how-it-works.md`.

## Releasing

Users who installed the plugin only receive changes when `version` in `.claude-plugin/plugin.json` changes
(the marketplace entry deliberately carries no version). For every release, bump together: `plugin.json`,
`__version__` in `skills/check-references/scripts/citecheck.py`, `CITATION.cff` and `CHANGELOG.md`; then run
the offline tests and the live `selftest`, regenerate `examples/manuscript-audit/output/`, and tag the commit.
