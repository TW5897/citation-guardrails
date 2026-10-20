---
name: cite-safely
description: >
  Writing-time discipline for any medical or scientific text that cites literature: never write a
  citation from memory, mark every unconfirmed reference or number with [VERIFY], label provenance
  ((a) what a source says, (b) the user's own data, (c) inference), flag open methodological choices
  with [DECIDE], and finish with the check-references then check-claims gate. Use this whenever
  drafting, expanding, revising or translating a manuscript, abstract, protocol, review, editorial,
  grant, thesis chapter, teaching slide or social post that states a study finding, statistic,
  effect size, guideline recommendation or cites a paper. Apply it even when the user only asks to
  "write the introduction" or "add some references".
---

# cite-safely

The failure this prevents is not the obviously fake reference. It is the confident paragraph with a
real-looking citation, a real DOI attached to the wrong authors, a correct paper quoted for a number
it never reported, or a "well-established" statistic nobody can trace. Readers cannot tell these
from the truth; neither can you, from memory. So the rule is procedural: **everything unverified is
visibly marked until a database and the source's own words have been checked.**

## Markers (use them inline, in every draft)

| Marker | Meaning | Who clears it |
|---|---|---|
| `[VERIFY]` | A citation, number or factual claim not yet confirmed against a source this session | `check-references` (existence) + `check-claims` (content) |
| `[NEEDS VERIFICATION]` | A reference that could not be resolved at all. Never ship it; surface it | The user, by supplying the source, or deletion |
| `[DECIDE]` | An open methodological or editorial fork (a cutoff, an inclusion rule, an endpoint definition, which of two framings) | The user |

Provenance labels, used when the distinction matters (methods, results, discussion of other trials):

- **(a)** what a cited source says, at the tier checked (abstract or full text),
- **(b)** the user's own data or results,
- **(c)** inference or framing.

Never blur these. "(a, abstract only)" is a legitimate and common label.

## Rules while drafting

1. **No citation from memory.** When a sentence needs a source, call `find-reference` (or ask the user
   for the paper). Until the source is found and verified, write the sentence with `[VERIFY]` and no
   fabricated placeholder reference. A bare `[VERIFY]` is better than a plausible wrong citation.
2. **No untraceable numbers.** Every effect size, prevalence, sensitivity, hazard ratio or percentage
   is either (a) quoted from a checked source with its CI, (b) the user's own result, or (c) explicitly
   your estimate, labelled. If you cannot trace it, do not state it.
3. **Abstract-level honesty.** If a detail (subgroup, secondary endpoint, exact number) is not in the
   abstract and the full text was not read, say "verified at abstract level" or keep `[VERIFY]`.
4. **Keep identifiers.** Every reference you insert carries its DOI or PMID so audits are unambiguous.
   Prefer the citation style the manuscript already uses; do not switch styles silently.
5. **Retractions and preprints are facts to report**, not to hide: cite a preprint as a preprint; never
   cite a retracted paper as evidence.
6. **Do not silently correct the user's own numbers.** If their figure disagrees with a source or with
   their own table, flag the discrepancy and ask.
7. **Separate the kinds of claim.** The user's own results are (b) and are not checked against other
   papers. Background statements with no citation are either common knowledge or need one; when in
   doubt, add `[VERIFY]`.

## The gate, before anything leaves the draft

Run, in this order, and report the results to the user:

1. `check-references` on the bibliography or manuscript: do the references exist and match?
2. `check-claims` on the manuscript: does each cited sentence say what its source says?
3. Only after both: remove the `[VERIFY]` markers that were cleared, and list every `[VERIFY]`,
   `[NEEDS VERIFICATION]` and `[DECIDE]` that remains, at the end of your reply.

Append a short verification footer to the draft while it is in progress:

```
Verification status (remove before submission)
- References checked: 14/16 VERIFIED, 1 VERIFIED_WITH_WARNINGS (ref 9 year), 1 NOT_FOUND (ref 12)  [check-references, 2026-10-20]
- Claims checked: 11 SUPPORTED, 2 PARTIALLY-SUPPORTED (abstract only), 1 UNSUPPORTED (claim 7)   [check-claims]
- Open: [VERIFY] x3, [NEEDS VERIFICATION] x1 (ref 12), [DECIDE] x1 (primary endpoint definition)
```

## Phrases that should trigger a marker

"Studies have shown", "it is well established", "approximately X% of patients", "a recent
meta-analysis found", "guidelines recommend" (which guideline, which class and level?), any named
trial result, any number with a decimal point. Each either gets a verified citation or a `[VERIFY]`.

## For users who want this always on

Copy `docs/claude-md-snippet.md` from this repository into your `CLAUDE.md` so the rules apply in
every session without invoking the skill.

## Related skills

- `find-reference`: locate and verify a source for a sentence.
- `check-references`: audit existence and metadata of all references.
- `check-claims`: audit claim-to-source fidelity.
