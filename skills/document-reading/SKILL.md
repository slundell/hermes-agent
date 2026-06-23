---
name: document-reading
description: How-to for reading a primary source into urd as durable, sourced claims without losing them to context compaction — the pre-staged local corpus read path and the externalize-as-you-understand cadence. Load when reading or ingesting any long document.
when_to_use: |
  Pull this when you are about to read and ingest a primary source (a case
  file, a long report, a transcript) into urd. It is the operational how-to
  behind the always-on "urd is your working memory" principle in
  learning-mindset: read the source directly, in digestible parts, and record
  each fact the moment you understand it, so a context compaction never costs
  you un-saved work. urd tool semantics live in urd://guide.
---

# Document reading — read a primary source into urd as you understand it

urd is durable; your working context is **not**. The whole job is to move atomic,
sourced claims from the document into urd *as you understand them*, so a
compaction never costs you un-saved work. The failure to avoid: read a whole
document into context → it overflows → force-compact → you reach `ingest()` with
nothing (the run that produced 0 claims in ~50 minutes).

## The cadence — externalize as you understand

urd is durable; your working context is not. You think IN urd — recording a claim
is the thinking step, not a save-afterward. Read as much as you need to understand
a passage (no limit), but record each fact as you understand it rather than piling
up understood-but-unrecorded facts to dump later; don't read the whole case before
recording — understanding accumulates in urd, which you revisit with
`recall`/`coverage`/`cores`. Watch `already_present_rate` per ingest: ~1.0 →
document harvested, move on; ~0.0 → keep going. If context fills, ingest what
you're holding now.

## Reading the corpus (pre-staged, local)

The Knutby closed-world corpus is plain text on the local filesystem at
`/data/knutby-corpus/` — one `.txt` per source document. Read it directly with
`read_file`; NO nextcloud, NO OCR, NO fetching. The `FU_Del` files are large —
read a long document in parts (`read_file` with offset/limit if available;
otherwise read a leading span, record, continue) so one read doesn't flood your
context. This is only to keep a single read digestible — there is NO cap on how
much of a document you may read in total.

## What to capture — atomic, verbatim, typed

**Atomic.** One assertion per claim — never a narrative summary, never
`Entity: <paragraph>`. A sentence asserting three things → three claims, so each
can be graded and contradicted on its own. (The failed run averaged ~240-char
multi-part claims; that is the anti-pattern to avoid.)

**Verbatim-close, in the source's own language.** The stored claim text is
**preserved primary data** — keep it in the source's words. For Swedish sources
the text stays **Swedish**; do **not** translate or summarise into English.
English belongs only in your private notes, never in the stored claim.
- ✅ `"Daniel sköts i sovrummet den 10 januari 2004."`
- ❌ `"Daniel was shot in the bedroom on 10 Jan 2004."` (translated)
- ❌ `"Linde shooting — bedroom, multiple events, see PM"` (summarised)

**Type it — don't flatten everything to `fact`.** Pick the ctype/polarity that
matches the assertion:
- `observation` — testimony / what someone said or saw ("Fossmo *uppgav* att …").
- polarity `neg` — denials and absences ("X *förnekar* …"; "protokollet nämner
  *inte* …").
- `relation` / `identity` — a link between entities, or "A is B".
- `fact` — only for plain source-stated facts that fit none of the above.

**Provenance is its own claim.** "Witness Z said W" is separate from whether W is
true — record both. **Record low, extract wide:** urd grades, so a weak claim is
stored low, not dropped. Every claim carries `sources` (PM-number, file, line).
Two entities named in the same material are not related unless the source says so.

**Anchor events** with when/where/who so urd can reason over them, not just recall
them — prefer a clock time ("22:00") over a bare date when the source gives one
(enables temporal contradiction checks). Check `reasoning_eligible`; fix a flat
existing claim with `anchor()`, not a re-`remember()`. urd holds only claims +
locators, never full text, so the READ + extraction step is always yours.

Full rubric + tool semantics: `urd://guide/extraction` and `urd://guide/ingest`;
the always-on one-liner is in `learning-mindset`.

## Test hypotheses by linking + ACH, not by re-confirming what you read
This is a closed archive — everything is already readable, so "predict then observe what you just read" only
re-confirms and is worthless. Reason the sound way:
- For each hypothesis list what it **requires** to be true and what it **forbids**. A required trace that is
  **absent**, or a forbidden trace that is **present**, is what moves you — seek those, not confirmations.
- Judge each piece of evidence by **diagnosticity**: does it discriminate between rivals, or fit them all?
- **Bind the co-referent claims** that bear on a hypothesis (same event / denial-of-asserted / same entity) so
  urd's checker can prove a contradiction (a `core`). Work `inbox()`; `confirm` real connections; `dismiss`
  non-contradictions. Then `check`.
