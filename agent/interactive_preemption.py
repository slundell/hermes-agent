"""Interactive-priority preemption.

Kill in-flight LOW-priority (background-review) LLM calls the instant an
INTERACTIVE (foreground) call starts, so a live turn never queues behind a long
review on the single-slot std GPU. Stops the contention storms where a review
monopolizes the slot and the foreground turn is starved until the 1800s
stale-watchdog kills it.

Reuses existing machinery only:
  * agent._interrupt_requested      — the stream/non-stream loops poll this and
                                      abort; also stops the review's own loop.
  * agent._active_request_canceller — the call's _close_request_client_once
                                      closure, published per call. It already
                                      has the stranger-thread-safe socket-abort
                                      path (#29507), so a foreground thread can
                                      kill the review's in-flight socket directly
                                      — needed because the streaming interrupt
                                      check is per-chunk and won't fire during a
                                      chunk-less cold prefill.
  * cherry-pick dd0d1222a           — interrupt-induced transport errors are not
                                      retried, so a killed review fails clean.

Only the background-review fork is low-priority. Foreground turns, subagents,
and delegations are interactive and are never preempted.

Tradeoff (intended): during rapid back-and-forth each new turn kills the pending
review, so reviews complete during conversation PAUSES. Best-effort skill/memory
updates lag slightly; live turns never stall.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_inflight_low_prio: Dict[int, Any] = {}   # id(agent) -> review agent, for its whole run


def is_low_priority(agent: Any) -> bool:
    """The background-review fork is the only low-priority caller."""
    return (
        str(getattr(agent, "_memory_write_origin", "") or "") == "background_review"
        or bool(getattr(agent, "_is_low_priority_call", False))
    )


def register_low_priority(agent: Any) -> None:
    """Register for the review's WHOLE run (not just one call) so an interactive
    request can preempt it whether it is mid-call or between tool turns."""
    with _lock:
        _inflight_low_prio[id(agent)] = agent


def deregister_low_priority(agent: Any) -> None:
    with _lock:
        _inflight_low_prio.pop(id(agent), None)


def preempt_low_priority(*, reason: str = "preempted_by_interactive") -> int:
    """Kill every in-flight low-priority call. Returns the count preempted.

    Cheap no-op when nothing is registered (the common case). Sets
    _interrupt_requested (stops the review loop + aborts on the next poll) AND
    invokes the published canceller for an immediate cross-thread socket abort.
    The canceller is idempotent and stranger-thread-safe, so calling a stale one
    (call already finished) is a harmless no-op.
    """
    with _lock:
        agents = list(_inflight_low_prio.values())
    if not agents:
        return 0
    n = 0
    for a in agents:
        # Primary signal: the interrupt flag stops the review loop and aborts on
        # the next poll. Count it here so a best-effort canceller failure below
        # never un-counts an already-flagged fork.
        try:
            a._interrupt_requested = True
            n += 1
        except Exception as exc:   # never let preemption break the interactive call
            logger.debug("preempt_low_priority: could not flag one fork: %s", exc)
            continue
        # Best-effort immediate socket abort — needed for a chunk-less cold
        # prefill where the per-chunk interrupt poll can't fire. Non-fatal.
        try:
            canceller = getattr(a, "_active_request_canceller", None)
            if canceller is not None:
                canceller(reason)
        except Exception as exc:
            logger.debug("preempt_low_priority: canceller failed (flag set): %s", exc)
    if n:
        logger.info("Preempted %d background-review call(s) for an interactive request", n)
    return n
