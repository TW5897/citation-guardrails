---
name: check-claims
description: >
  Claim-to-source audit: for every sentence that cites a reference, check whether the cited paper
  actually says what the sentence says (number, direction of effect, endpoint, population,
  recommendation), using the paper's abstract or open-access full text fetched by script, and record
  a verdict with a quoted snippet and the evidence tier reached. Catches the error check-references
  cannot: a real, correctly cited paper paired with a claim it never made. Use this before any
  manuscript, review, protocol, editorial, grant or thesis is submitted or circulated, after
  AI-assisted drafting or revision, when a reviewer or co-author questions a statement, or whenever
  the user asks "does the source support this", "fact-check my draft", "are these claims right",
  or mentions misquoted, misattributed or over-stated citations.
allowed-tools: Bash(python "${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py" *), Bash(python3 "${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py" *), Bash(py -3 "${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py" *), Read, Glob, Grep
---

# check-claims

`check-references` tells you a paper exists and is cited correctly. This skill asks the harder
question: **does it say that?** A metadata-perfect citation can still be attached to a number the
paper never reported, the wrong endpoint, the wrong direction, or a conclusion the authors did not
draw. You check each cited sentence against the source's own words and report, one claim at a time.

Audit only. You do not rewrite the manuscript, invent sources, or guess what a paper "probably" says.

## Run order

1. `check-references` first. A `NOT_FOUND` or `MISMATCH` reference has no source to check a claim
   against; a `RETRACTED` one fails every claim automatically. If it has not been run, run it now.
2. This skill.

## The scripts

Both live in the sibling skill: `${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py`.
If that file does not exist, stop and tell the user to install `check-references` as well. On Windows
use `python` or `py -3` for `python3`.

```bash
# 1. list every sentence that carries a citation, with its cite keys and numbers
python "${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py" claims manuscript.md --out-dir qc
#    -> qc/claims_to_check.json, qc/claims_to_check.md     (--style numeric|pandoc|apa if auto-detection misfires)
#    handles [1], [1-3], ^1^, Word superscripts, pandoc [@key], LaTeX \cite{}, (Author, 2020) and Author et al. (2020)

# 2. fetch abstracts and, where open access, full text for every verified reference
python "${CLAUDE_SKILL_DIR}/../check-references/scripts/citecheck.py" evidence qc/reference_audit.json --out-dir qc
#    -> qc/evidence/<ref_id>.md (one pack per reference) and qc/evidence/index.json (tier per reference)
#    --only ref_3 ref_7 to limit; --ids "10.1056/NEJMoa2021372" for a single paper; --abstract-only to skip full text
```

The evidence pack header states the **tier** reached: `full_text` (Europe PMC open-access XML, with
its licence), `abstract` (PubMed, Europe PMC, OpenAlex or CrossRef), or `none`. For paywalled papers
you may use the user's own PDF or notes; record that as the tier. Packs hold publishers' text under
their own licences: they are for this audit, not for committing to a repository or redistributing.

## Completeness first

`claims_to_check.json` is a starting list, not proof of completeness. Before any verdict:

- If the `claims` command printed `0 cited sentences extracted` or exited 3, **stop**: the manuscript's
  citation style was not recognised. Look at the text, tell the user which markers you see, and check
  the sentences by hand or ask for a different export. Zero extracted claims is never "claim-safe".
- Compare the set of cite keys/numbers in the claims file with the `ref_id`s in `reference_audit.json`.
  List references that are never cited and cite markers that map to no reference.
- Skim the manuscript for citation markers the extractor may have missed (unusual superscripts, footnote
  cites, author-year forms with "e.g.,") and add them to your checklist.

## Two kinds of claim: separate them first

- **External-attributed**: cites another work ("colchicine reduced the primary endpoint by 31% [1]").
  In scope. Check against the cited source.
- **Own-result**: the paper's own finding ("in our cohort, readmission was 4.0 points lower"). Not
  checkable against literature. Trace to the user's own tables if supplied; otherwise mark
  `OWN-DATA` and move on. Never verify an own result against someone else's paper.

Uncited background statements are out of scope unless the user asks you to flag sentences that need a
citation.

## Per-claim procedure

For each row in `claims_to_check.json` (plus anything you added in the completeness step):

1. Map the cite keys to references: numeric `[1]` → `ref_1`; pandoc `@Key` or LaTeX `\cite{Key}` → the
   BibTeX key; author-year → the matching entry. If a key maps to nothing, record
   `UNVERIFIABLE (cite key unresolved)`.
2. Open the evidence pack(s). Note the tier.
3. State the **checkable assertion** in one line: the number, direction, endpoint, population or
   recommendation the sentence attributes to the source.
4. Find the source's words that bear on it. Quote them (at most 40 words).
5. Give a verdict and severity:

| Verdict | Meaning | Severity |
|---|---|---|
| `SUPPORTED` | Source states the claim, including number and direction, at the tier reached | - |
| `PARTIALLY-SUPPORTED` | Gist right, detail off: rounding, CI omitted, "reduced" vs "non-significantly reduced", composite endpoint described as one component, or abstract-only for a detail that lives in the body | medium (low for rounding / missing CI) |
| `UNSUPPORTED` | Source is silent on this claim | high |
| `CONTRADICTED` | Source says the opposite or a materially different number | high |
| `MISATTRIBUTED` | Claim is true somewhere, but not in this source (wrong paper cited) | high |
| `OWN-DATA` | The manuscript's own result; not checked externally | - |
| `UNVERIFIABLE` | No abstract or text could be obtained at any tier | flag |

6. A claim about a subgroup, secondary endpoint, adverse event rate or methods detail that is **not
   in the abstract** cannot be `SUPPORTED` at `abstract` tier. It is `PARTIALLY-SUPPORTED
   (abstract-only)` until full text is read. Say so.

## Output

Write `qc/claim_audit.json`:

```json
{"manuscript": "...", "generated": "...", "verify_refs_run": true, "claims_extracted": 14, "claims_added_by_hand": 1, "claim_safe": false,
 "counts": {"supported": 0, "partial": 0, "unsupported": 0, "contradicted": 0, "misattributed": 0, "own_data": 0, "unverifiable": 0},
 "records": [{"id": 2, "sentence": "...", "cites": ["1"], "ref_ids": ["ref_1"], "assertion": "...",
              "verdict": "CONTRADICTED", "severity": "high", "tier": "abstract",
              "source_evidence": "quoted words from the pack", "fix": "..."}]}
```

and `qc/claim_audit.md` with high-severity rows first. In chat, lead with the high-severity table,
then medium/low, then `OWN-DATA` / `UNVERIFIABLE` rows that need the user, then the completeness
notes (uncited references, unmapped markers). Finish with the verdict: **claim-safe** only if the
extraction was complete and no `CONTRADICTED`, `MISATTRIBUTED`, `UNSUPPORTED` or external
`UNVERIFIABLE` row remains.

## Worked example (real abstract, fetched by the script)

Draft sentence: "Colchicine also reduced all-cause mortality in this population [1]", where [1] is
LoDoCo2 (Nidorf et al., N Engl J Med 2020; DOI 10.1056/NEJMoa2021372). `check-references`: VERIFIED.

Evidence pack, tier `abstract`, PubMed: the primary end point (a composite of cardiovascular death,
MI, ischemic stroke or ischemia-driven revascularization) had "hazard ratio, 0.69; 95% confidence
interval [CI], 0.57 to 0.83", and "The incidence of death from noncardiovascular causes was higher in
the colchicine group than in the placebo group (incidence, 0.7 vs. 0.5 events per 100 person-years;
hazard ratio, 1.51; 95% CI, 0.99 to 2.31)". The abstract reports no all-cause mortality result.

Verdict: **UNSUPPORTED (high)** at abstract tier, leaning CONTRADICTED: the source does not report an
all-cause mortality reduction and reports numerically more non-cardiovascular deaths with colchicine.
Fix: restate the composite primary endpoint with its HR and CI, or remove the mortality claim.

Same manuscript: "reduced the composite ... by 31% [1]". Source: HR 0.69. Verdict: **SUPPORTED**
(1 − 0.69 = 31%), tier abstract; suggest adding the CI.

## Do not

- Do not give any verdict without a quoted snippet from the evidence pack (or the user's PDF) at a stated tier.
- Do not upgrade an abstract-only check to `SUPPORTED` for a detail that is not in the abstract.
- Do not verify own-result claims against external papers, or "correct" the user's own numbers.
- Do not mark a claim `SUPPORTED` because the paper is about the right topic; the specific assertion must be there.
- Do not edit the manuscript or references. Report; the user decides.
- Do not call a draft claim-safe while a high-severity or external `UNVERIFIABLE` row stands, or when extraction was incomplete.
- Do not re-audit existence or metadata; that was `check-references`.
- Do not send manuscript sentences to web search engines; the evidence packs and the user's own sources are the material.

## Related skills

- `check-references`: run first.
- `find-reference`: when a claim is `MISATTRIBUTED` or `UNSUPPORTED`, find the source that does support it, or advise removal.
- `cite-safely`: the drafting rules that keep this audit short.
