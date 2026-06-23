---
name: learning-mindset
description: "Always-on disposition for capturing durable knowledge. Use whenever you learn something worth keeping — a citable fact, a judgment about a source, a notable entity or narrative — in ANY task, not only when reading documents."
when_to_use: |
  A standing disposition, not a workflow you invoke. Active in every session.
  Apply it the moment you encounter durable information, regardless of what you
  are doing — a database query, a forum scrape, an archive read, a casual
  answer. What you persist is decided by what you learned, not by which skill is
  running.
---

# Learning mindset — capture what you learn, by what it is

You are continuously learning things. What gets persisted is decided by the
**kind of information** in front of you, never by which skill or workflow
happens to be active. A citable fact found mid-SQL-query deserves the same
capture as one found while reading a book.

## The three sinks — route by information type

| When you encounter… | Persist it to… | Via |
|---|---|---|
| An **atomic, citable claim** worth recalling and reasoning over — a witness statement, date, decision, identification, measurement — traceable to a source (line, PM-number, post, row) | **urd** — the system of record for case facts/claims | `remember(text, sources)` — ALWAYS with `sources` |
| A **judgment about a source as a source** — coverage, gaps, reliability, proximity, what it corroborates or contradicts | **llm-wiki** — `/obsidian/Aina/wiki/sources/<slug>.md` + index.md + log.md | load `llm-wiki` |
| **Narrative or entity knowledge** a teammate would read — a summary, a person/org/place/event dossier, a growing research note | **Obsidian vault** — `Dokument/`, `Personer/`, `Aina/…` | load `obsidian` |

The boundary that matters: **facts and claims you'd want to recall or reason
over go to urd** (`remember`, always cited); **long-form notes and documents go
to Obsidian**. Never store a recallable fact in Obsidian, and never use Obsidian
for what urd grades and cross-checks.

A single thing you learn can hit more than one sink. Reading one document
typically hits all three — that is what `document-reading` sequences. But the
trigger is the information, not the pipeline.

## Persist by default

Capture is **not optional and never needs permission.** If you learned
something durable, it is already on its way to a sink — the only open question
is *what to read or do next*, never *whether to save*.

Forbidden — these mean you failed to persist:
- ❌ "Vill du att jag sparar fakta?"
- ❌ "Ska jag skriva en sammanfattning?"
- ❌ ending a task with durable facts left only in the chat transcript

## Write as you read — urd is your working memory

urd is your durable working memory; your context window is volatile and will
compact and vanish. Recording a claim to urd IS the thinking step, not a
save-for-later — as you read and understand each fact, record it then. A fact you
understand but leave only in your head is lost at the next compaction. Read as
much as you genuinely need to understand a passage — no limit on that — but don't
read and hold the whole case before recording; build understanding incrementally
in urd and revisit it with `recall`/`coverage`/`cores`. If your context starts
filling, that means you're holding things that belong in urd — record them now.
Full procedure: load **`document-reading`**.

## Extract for recall — record low, extract wide

urd **grades** every claim, so the bar to write something down is LOW. A weak or
uncertain claim is stored at a low grade, not dropped; a claim you never record
is invisible forever. Confidence lives in the *grade*, not in whether you
bothered to capture it — so extract aggressively and let the grade carry the
doubt. The common failure is under-extraction: recording prose narrative instead
of many atomic claims.

- **Atomic — one assertion per claim.** A sentence stating three things becomes
  three claims, never one narrative summary. **Never** write
  `Entity: <paragraph summary>` — split relations, quantities, and qualifiers into
  their own claims so each can be graded and contradicted on its own.
- **Claim text = the source's own language, verbatim-close.** For Swedish sources
  the stored text stays **Swedish** — never translate or summarise into English.
  The text is preserved primary data; English belongs only in your private notes,
  never in the stored claim.
- **Set the type — don't flatten everything to `fact`.** `observation` for
  testimony / what someone said or saw; polarity `neg` for denials and absences
  ("X did *not* …"); `relation` / `identity` where they fit.
- **Capture the classes you skip.** Peripheral details; implicit/entailed facts;
  **negations and absences** ("the record does *not* place X at the scene" is a
  real claim in a closed world); **provenance** ("witness Z said W" — who-said-it
  is its own claim, separate from whether W is true); quantities; relations
  ("X knew Y").
- **Structure every event** with when/where/who (`remember(..., when=, where=,
  who=)`, or `anchor()` an existing claim) so the checker can reason over it.

Treat **"cores()/tensions() found nothing" as "I under-extracted"**, not "the
record is consistent" — go capture more, especially negations and provenance.
Expect, by design, more near-duplicates (more pending-identity items to
adjudicate in the inbox) and more low-grade noise; graded memory absorbs both.
Pull `urd://guide/extraction` for the full version.

## Capture faithfully

Every claim you `remember` carries its source — that is why `sources` is
mandatory on every call (PM-number, file, line, post). No synthesis without a
source. Two entities named in the same material are **not** related unless the
source says so. When unsure, hedge in the claim text (probable / paraphrased)
rather than assert.

## Anchor events so urd can reason over them

A claim becomes *checkable* — contradiction-tested and placed on urd's spatial
prism — only when it carries its anchors. When you `remember` an **event**
(something that happened at a time, in a place, or by someone), it **MUST**
carry the anchors the source provides — **when** (a time phrase or bounds),
**where** (a place), **who** (the actor) — so urd can reason over it, not just
recall it. Then check `reasoning_eligible` in the response: `false` means the
claim was stored as memory only, so supply the anchor that was missing if the
source has it. Per document, also read `coverage().data_quality.anchoring` — it
counts how many of your claims are still unanchored; go back and `anchor()` the
event claims you left flat to drive that count down.

**Plain facts can stay bare.** Don't invent a time, place, or actor the source
doesn't give — a forced anchor is a faithfulness failure, the same as a
sourceless claim. Anchors are read off the source, never manufactured just to
clear the flag.

**Make the time anchor a clock time when you want temporal reasoning.** urd
resolves a clock time ("22:00", "kl 22.00", "2004-01-10 22:00") to a within-day
time bound, so such a claim can be checked for order- and gap-contradictions. A
**bare date** ("2004-01-10", "10 januari 2004") yields *no* time bound — it
still anchors who/where for the prism and entity-linking, but the claim won't
enter temporal reasoning. So give the time anchor as a clock time whenever the
source provides one (explicit numeric bounds also work); fall back to a date
alone only when that's all there is.

**To fix a claim that already exists, `anchor` it — don't re-`remember`.** When a
claim already in urd needs structure (it came back memory-only, or you later
realize it should be checkable), `anchor` adds the when/where/who to that same
claim in place, keeping its id and every connection it already has. Re-saving
would duplicate the claim and strand its links. Reserve `replace` for when the
*assertion itself* is wrong or has changed — it supersedes the claim with a
newly authored one (lineage kept), which moves its identity. Adding structure to
a *correct* claim is always `anchor`, never `replace`.

## Where the mechanics live

- **Reading a primary source into urd** (read forward in digestible reads →
  record each fact as you understand it → all three sinks) → load
  **`document-reading`** for the how-to; urd tool semantics live in
  `urd://guide/ingest`. Always-on reminder: the closed-world case corpus is
  staged locally at **`/data/knutby-corpus/`** — read it with the **`read_file`**
  tool (line-numbered, paginated; follow the returned `hint` to page forward),
  not nextcloud or `document_read`; and urd holds only claims + locators, so the
  READ + extraction step is always yours.
- **Wiki source-pages, index, log, schema** → **`llm-wiki`**.
- **Vault writes** (paths, frontmatter, wikilinks, obsidian-cli) → **`obsidian`**.
- **Growing a multi-session research note** → **`iterative-research-notes`**.

To pull an existing claim back out, use `recall(query)` against urd — search it
before writing conclusions, so you build on what is already known rather than
re-deriving it. Absence of a tool is never a reason to skip capture; if `urd`
is somehow unreachable, hold the cited claim in your working channel and store
it as soon as the tool returns.

## Connect as you record — urd reasons only over what you link
Recording a claim is half the job; the other half is **linking** it. urd proves contradictions only between
claims you have bound — it will not guess that two differently-worded claims are about the same event. As you
record, ask: does this bear on something already in urd — the **same event**, a **denial/contradiction** of an
asserted claim, the **same person/place/time**? If so, bind them (`resolve` / `confirm` the connection) and let
`check` look for a core. Run `check` **after** you've linked, not before — an unlinked store has nothing to prove.

## Test rivals by disconfirmation, not by re-confirming (closed archive)
Everything in a closed archive is already readable, so "predict then observe what you read" only re-confirms —
worthless. For each hypothesis, name what it **requires** and what it **forbids**; a required trace that is
**absent**, or a forbidden trace that is **present**, is what moves you. Rate evidence by whether it
**discriminates** rivals; seek disconfirmation. Bind the co-referent claims a hypothesis turns on so `check`
can prove a contradiction (a `core`).
