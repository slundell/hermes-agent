---
name: document-reading
description: How-to for ingesting a primary source into urd without losing claims to context compaction — the bounded-chunk read→ingest→drop cadence, the nextcloud archive reader (pre-computed OCR), the hard-flush rule, and what to capture. Load when reading/ingesting any long document.
when_to_use: |
  Pull this when you are about to read and ingest a primary source (an archive
  PDF, a long report, a transcript) into urd. It is the operational how-to behind
  the always-on "write as you read" principle in learning-mindset: read in
  bounded chunks, ingest each chunk immediately, keep the unsaved backlog tiny,
  and hard-flush before a compaction. urd tool semantics live in urd://guide.
---

# Document reading — ingest a primary source without losing claims

urd is durable; your working context is **not**. The whole job is to move atomic,
sourced claims from the document into urd *faster than your context fills*, so a
compaction never costs you un-saved work. The failure to avoid: read a whole
document into context → overflow → force-compact → reach `ingest()` with nothing
(the run that produced 0 claims in ~50 minutes).

## The cadence — a HARD, forcing loop (not a guideline)

This has now failed twice: you read whole documents into context and reached
`ingest()` with **zero claims**. The fix is a strict read↔ingest interleave you
do not get to opt out of. The unit is a **window** of text pulled into context.

1. `nextcloud read "<path>"` **once** per document — this writes the *whole*
   document's text to a file under `/tmp/aina-results/`. It does **not** load it
   into your context, and it has no offset/length flags.
2. Pull **ONE window** of that file into context: `sed -n 'START,ENDp' <file>` for
   ~300–500 lines (~2–3k words). Use `grep -n 'TERM' <file>` to find anchors.
3. **MANDATORY — your very next urd action MUST be `ingest([...])`** of that
   window's atomic claims. Not a recall, not another read — `ingest`.
4. Only then pull the next window (back to step 2).

**You may NOT pull the next window (`sed`) — and may NOT `nextcloud read` another
document — until the current window's claims are in urd.** Hard constraints, no
exceptions:

- **Ingest from the FIRST window before reading anything else.** Do not read a
  second window or a second document to "understand the sequence / context" first.
  Understanding accumulates in **urd**, not in your context window.
- **Never hold more than one un-ingested window.** If you have read text you have
  not yet ingested, ingest it before doing anything else.
- **Do not cross-reference across documents in your head before recording.**
  Record each window's claims as they stand; urd links and contradiction-checks
  them later (`cores()`, `tensions()`). Holding facts in context to "connect them"
  is exactly what overflows and loses them.
- **Flush before compaction.** If context is filling or the turn is long, ingest
  everything pending now — an un-`ingest()`ed claim is lost at compaction.
- **Saturation = next document.** `already_present_rate` ≈1.0 → that document is
  harvested, move on; ≈0.0 → keep going.

## Reading the archive (pre-computed OCR)

Case-source documents live in the team Nextcloud archive — not on the local
filesystem (`document_read("/nextcloud/…")` returns *file not found*). Use the
`nextcloud` tool via `terminal` (never `execute_code` — no creds in that sandbox):

- `nextcloud ls "<folder>"` — list the corpus.
- `nextcloud read "<archive path>"` — extract text. Returns the archive's
  **pre-computed OCR** from the search index (~2s; it only re-runs OCR as a
  fallback for the rare unindexed file) and saves it under `/tmp/aina-results/`.
- The extract is large, so **slice it** — never load the whole file into context:
  `grep -n 'TERM' <file>` to find section anchors, `sed -n 'START,ENDp' <file>`
  to pull one chunk at a time. This *is* the bounded-chunk read above.
- If `read` returns a near-empty file, the OCR is corrupt for that doc — move on,
  don't retry the same path.

Reading the corpus through the `nextcloud` tool is the **sanctioned** in-corpus
path; it stays inside a closed-world / walled-garden run and is **not** "shelling
out" to the outside. (`nextcloud-research` is authoritative for the tool's flags.)

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
