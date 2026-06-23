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

## The cadence: bounded chunk → ingest → drop → continue

- **Read a bounded chunk, not the document.** Take in just enough to understand
  the claims in context — a section, a few pages — then stop. Never read most or
  all of a long document before you write, and never read more than a few minutes
  before an `ingest()`.
- **Ingest that chunk immediately, then drop it.** Extract the chunk's atomic
  claims and `ingest([...])` them right away; clear that text from working context
  before reading the next chunk. Read → ingest → drop → read next.
- **Keep the unsaved backlog tiny** — a handful of claims in flight, never dozens.
  If claims are piling up unsaved, stop reading and ingest what you have.
- **HARD FLUSH on approaching compaction.** If a compaction feels near (LCM /
  fresh-tail trimming, context filling, a long turn), ingest everything you have
  extracted *before* it triggers — don't finish the page first. A flushed claim is
  durable; an un-flushed one is gone.
- **Saturation = next document.** Watch `already_present_rate` per `ingest()`
  batch (≈1.0 = already harvested, move on; ≈0.0 = finding new things, keep
  going); when it saturates the document is harvested — move to the next one.

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
