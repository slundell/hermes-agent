"""DRAFT — holographic memory auto-pruner.

Capacity management for the HRR fact store. Two facts about the store shape this:

  * SNR = sqrt(dim / n_items) is computed PER CATEGORY (each category is its own
    `memory_banks` superposition; see store._rebuild_bank). So capacity is a
    per-bank property and pruning runs per category.
  * The store never evicts — facts only accumulate (add_fact dedups by EXACT
    content only). trust/feedback/decay merely change *retrieval scores*; a
    low-trust fact still sits in the superposition adding crosstalk. So nothing
    ever brings n_items down. This closes that gap.

It uses the signals the store already records — trust_score, retrieval_count,
helpful_count, recency (updated_at), and near-duplicate token overlap — in two
stages per over-capacity category:

  1. CONSOLIDATE near-duplicates (lossless): collapse each near-dup cluster to
     its highest-value member, transferring retrieval/helpful counts and the max
     trust into the survivor; archive the rest. (Targets the on_memory_write
     mirror dups + paraphrases that exact-dedup misses.)
  2. EVICT lowest-value facts until n_items <= target (target chosen for a
     comfortable SNR), protecting recent facts and configured categories.

Safety by design: archives to `facts_archive` (recoverable) instead of hard
DELETE; supports dry_run; protects categories; batch-rebuilds each bank once.

Status: DRAFT. `analyze()` is read-only and runnable now against the live DB to
preview the plan. `apply()` (the mutate path) is sketched for wiring into the
store/tool once the policy is approved.
"""
from __future__ import annotations

import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Tunables (DRAFT defaults — these are the policy knobs to review) ──────────
TARGET_SNR          = 2.5            # prune category to n <= dim / TARGET_SNR**2  (dim 1024 -> ~163)
DEDUP_JACCARD       = 0.82           # token-overlap >= this = near-duplicate -> consolidate
PROTECT_CATEGORIES  = ("user_pref",) # never EVICT from these (still deduped — losslessly)
MIN_AGE_HOURS       = 24             # grace period: never evict facts younger than this
RECENCY_HALFLIFE_D  = 45.0           # recency value halves every N days
# value-score weights — "what makes a fact worth keeping" (sum ~= 1.0)
W_TRUST, W_RETRIEVAL, W_HELPFUL, W_RECENCY = 0.40, 0.25, 0.15, 0.20

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _parse_ts(ts: str | None, now: float) -> float:
    if not ts:
        return now
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return now


def _target_count(dim: int) -> int:
    return max(1, int(dim / (TARGET_SNR ** 2)))


def _snr(dim: int, n: int) -> float:
    return float("inf") if n <= 0 else math.sqrt(dim / n)


@dataclass
class PruneReport:
    category: str
    dim: int
    before: int
    target: int
    snr_before: float
    deduped: int = 0          # archived as near-dups (lossless)
    evicted: int = 0          # archived as low-value
    after: int = 0
    snr_after: float = 0.0
    protected: bool = False
    dry_run: bool = True
    plan: list = field(default_factory=list)   # (fact_id, reason, value, content[:70])


def _value_score(f: dict, max_retr: int, max_help: int, now: float) -> float:
    """0..1-ish keep-value. High = trusted / often-retrieved / helpful / recent."""
    trust = float(f.get("trust_score") or 0.0)
    retr = math.log1p(f.get("retrieval_count") or 0) / math.log1p(max_retr) if max_retr else 0.0
    helpr = math.log1p(f.get("helpful_count") or 0) / math.log1p(max_help) if max_help else 0.0
    age_d = max(0.0, (now - _parse_ts(f.get("updated_at"), now)) / 86400.0)
    recency = 0.5 ** (age_d / RECENCY_HALFLIFE_D)
    return W_TRUST * trust + W_RETRIEVAL * retr + W_HELPFUL * helpr + W_RECENCY * recency


def _dedup_clusters(facts: list[dict]) -> list[list[int]]:
    """Greedy near-dup clustering by token Jaccard. Returns clusters (>=2) of list indices."""
    toks = [_tokens(f["content"]) for f in facts]
    seen = [False] * len(facts)
    clusters: list[list[int]] = []
    for i in range(len(facts)):
        if seen[i]:
            continue
        group = [i]
        seen[i] = True
        for j in range(i + 1, len(facts)):
            if not seen[j] and _jaccard(toks[i], toks[j]) >= DEDUP_JACCARD:
                group.append(j)
                seen[j] = True
        if len(group) > 1:
            clusters.append(group)
    return clusters


def analyze(db_path: str, category: str | None = None, *, now: float | None = None) -> list[PruneReport]:
    """Read-only: compute the prune plan per category. Mutates nothing."""
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        dim = int(conn.execute(
            "SELECT dim FROM memory_banks ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()["dim"])
    except (TypeError, sqlite3.Error):
        dim = 1024
    cats = ([category] if category else
            [r["category"] for r in conn.execute(
                "SELECT category, COUNT(*) c FROM facts GROUP BY category ORDER BY c DESC")])
    reports: list[PruneReport] = []
    for cat in cats:
        facts = [dict(r) for r in conn.execute(
            """SELECT fact_id, content, category, trust_score, retrieval_count,
                      helpful_count, created_at, updated_at
               FROM facts WHERE category = ?""", (cat,))]
        n0 = len(facts)
        target = _target_count(dim)
        rep = PruneReport(category=cat, dim=dim, before=n0, target=target,
                          snr_before=_snr(dim, n0),
                          protected=(cat in PROTECT_CATEGORIES))
        if n0 <= target:
            rep.after, rep.snr_after = n0, _snr(dim, n0)
            reports.append(rep)
            continue

        survivors = {f["fact_id"]: f for f in facts}
        # Stage 1 — consolidate near-dups (lossless): keep the best per cluster.
        max_retr = max((f["retrieval_count"] or 0) for f in facts) or 1
        max_help = max((f["helpful_count"] or 0) for f in facts) or 1
        for group in _dedup_clusters(facts):
            members = [facts[k] for k in group]
            keep = max(members, key=lambda f: _value_score(f, max_retr, max_help, now))
            for m in members:
                if m["fact_id"] != keep["fact_id"]:
                    rep.deduped += 1
                    rep.plan.append((m["fact_id"], "dup", 0.0, m["content"][:70]))
                    survivors.pop(m["fact_id"], None)

        # Stage 2 — evict lowest-value until under target (skip if protected).
        if not rep.protected and len(survivors) > target:
            scored = sorted(
                ({**f, "_v": _value_score(f, max_retr, max_help, now)} for f in survivors.values()),
                key=lambda f: f["_v"])
            need = len(survivors) - target
            for f in scored:
                if need <= 0:
                    break
                age_h = (now - _parse_ts(f.get("updated_at"), now)) / 3600.0
                if age_h < MIN_AGE_HOURS:        # protect recent
                    continue
                rep.evicted += 1
                rep.plan.append((f["fact_id"], "low-value", round(f["_v"], 3), f["content"][:70]))
                need -= 1

        rep.after = n0 - rep.deduped - rep.evicted
        rep.snr_after = _snr(dim, rep.after)
        reports.append(rep)
    conn.close()
    return reports


# ── Mutate path (SKETCH — wire into HolographicStore once policy is approved) ──
def apply(store, category: str | None = None) -> list[PruneReport]:
    """Execute the plan from analyze(): archive marked facts, rebuild banks once.

    Uses the store's connection + HRR machinery. Archives to facts_archive
    (recoverable) rather than hard-deleting. Rebuilds each touched bank a single
    time after all removals (NOT per-fact, which would be O(n^2)).
    """
    raise NotImplementedError(
        "DRAFT: wire to store after policy review. Steps:\n"
        "  conn.execute('CREATE TABLE IF NOT EXISTS facts_archive (... + archived_at, reason)')\n"
        "  for each planned fact: INSERT INTO facts_archive SELECT *,reason FROM facts WHERE fact_id=?;\n"
        "                          DELETE FROM fact_entities WHERE fact_id=?; DELETE FROM facts WHERE fact_id=?\n"
        "  for each touched category: store._rebuild_bank(category)   # once\n"
        "  return reports"
    )


if __name__ == "__main__":   # python pruner.py <db_path> [category]
    import sys
    dbp = sys.argv[1] if len(sys.argv) > 1 else ""
    cat = sys.argv[2] if len(sys.argv) > 2 else None
    for r in analyze(dbp, cat):
        tag = " [PROTECTED:evict-skipped]" if r.protected else ""
        print(f"\ncategory={r.category!r} dim={r.dim}{tag}")
        print(f"  {r.before} facts (SNR {r.snr_before:.2f}) -> target {r.target} "
              f"(dedup {r.deduped} + evict {r.evicted}) -> {r.after} (SNR {r.snr_after:.2f})")
        for fid, reason, val, snippet in r.plan[:8]:
            print(f"    - #{fid:<5} {reason:<10} v={val:<5} {snippet!r}")
        if len(r.plan) > 8:
            print(f"    … +{len(r.plan) - 8} more")
