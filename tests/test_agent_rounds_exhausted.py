"""Regression: stream_agent_loop emits `rounds_exhausted` only when the round
cap is hit while still working, and NOT on a normal finish.

The decision is a `for/else` in the loop: the `else` runs only if no `break`
fired (break = done / budget / error). A refactor that adds a stray break or
return, or moves the done-break, could silently flip this. See PR #1999 / #1997.
"""

import asyncio
import json

import src.agent_loop as al


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _types(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch_common(monkeypatch):
    # Skip RAG/tool-index, MCP, and settings lookups; keep the real loop body,
    # _resolve_tool_blocks, and parse_tool_blocks.
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _run_loop(monkeypatch, round_text, max_rounds=2):
    async def _fake_stream(_candidates, messages, **kwargs):
        yield f'data: {json.dumps({"delta": round_text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do a long multi-step task"}],
        max_rounds=max_rounds,
        relevant_tools={"bash"},
    )
    return _types(_collect(gen))


def test_emits_rounds_exhausted_when_cap_hit_mid_task(monkeypatch):
    _patch_common(monkeypatch)
    # Use a system-owned interaction result so this remains a loop-control test:
    # Bash output is workspace-derived and now correctly pauses for exact user
    # approval before a later Bash call.
    events = _run_loop(
        monkeypatch,
        '```update_plan\n{"plan":"- [ ] keep going"}\n```',
        max_rounds=2,
    )
    assert any(e.get("type") == "rounds_exhausted" for e in events), events


def test_no_rounds_exhausted_on_normal_finish(monkeypatch):
    _patch_common(monkeypatch)
    # A plain answer (no tool block) -> done-break on round 1 -> no event.
    events = _run_loop(monkeypatch, "All done, here is your answer.", max_rounds=2)
    assert not any(e.get("type") == "rounds_exhausted" for e in events), events


def test_emits_intent_nudge_exhausted_when_cap_is_exhausted(monkeypatch):
    _patch_common(monkeypatch)

    events = _run_loop(monkeypatch, "Let me check the logs", max_rounds=5)

    guard = next((e for e in events if e.get("type") == "intent_nudge_exhausted"), None)
    assert guard is not None, events
    assert guard["reason"] == "intent_without_action_nudge_cap"
    assert guard["nudges"] == 2


def test_emits_loop_breaker_triggered_when_loop_breaker_trips(monkeypatch):
    _patch_common(monkeypatch)

    events = _run_loop(
        monkeypatch,
        '```update_plan\n{"plan":"- [ ] keep going"}\n```',
        max_rounds=6,
    )

    guard = next((e for e in events if e.get("type") == "loop_breaker_triggered"), None)
    assert guard is not None, events
    assert guard["reason"] == "loop_breaker_stall"


# ---------------------------------------------------------------------------
# Intent-without-action supervisor: length-cap + anchor regressions.
#
# Live session 960ef2a3 (model qwen3.5-9b-uncensored-hauhaucs-aggressive) had
# four turns end with NO streamed event at all. The model announced an action,
# emitted no tool call, and control fell through to the bare
# `break  # no tools — done` at the bottom of the supervisor because the
# predicate above it never fired:
#
#   17:49:31   420 chars  regex matched, len >= 400         -> missed
#   17:49:54   443 chars  regex matched, len >= 400         -> missed
#   17:50:03   475 chars  regex matched, len >= 400         -> missed
#   17:52:50   194 chars  no match ("...handled:Let me...")  -> missed
#
# The first three were long only because the model emitted *untagged* inline
# reasoning ahead of a short, 51-char visible answer. _strip_think_blocks drops
# <think> tags only, so the whole-text length cap was measuring reasoning
# instead of the promise. The predicate now scores the closing paragraph and
# accepts clause boundaries (":", ";", ".", "?", "!") as an anchor.
# ---------------------------------------------------------------------------


def test_emits_intent_nudge_exhausted_when_reasoning_inflates_round_text(monkeypatch):
    _patch_common(monkeypatch)

    reasoning = (
        "Now I have the full context about the available tools and how the "
        "agent loop dispatches them. The issue the user described is clear: "
        "when they call web_search or a file tool, the arguments are not "
        "being resolved against the workspace root, they are being routed at "
        "a remote repository path instead. Before changing any code I want to "
        "confirm whether that resolver is shared between the web and file "
        "code paths, and whether the routing table is built at import time."
    )
    answer = "Let me look at the issue tracker for related bugs:"
    round_text = f"{reasoning}\n\n{answer}"

    # Precondition: this shape only stalls because the whole round busts the
    # cap while the visible answer does not.
    assert len(round_text) > 400, len(round_text)
    assert len(answer) < 400

    events = _run_loop(monkeypatch, round_text, max_rounds=5)

    guard = next((e for e in events if e.get("type") == "intent_nudge_exhausted"), None)
    assert guard is not None, events
    assert guard["reason"] == "intent_without_action_nudge_cap"
    assert guard["nudges"] == 2
    assert "issue tracker" in guard["matched"]


def test_emits_intent_nudge_exhausted_for_clause_anchored_promise(monkeypatch):
    _patch_common(monkeypatch)

    # The only lead-in here sits immediately after ":", with no space and no
    # preceding newline — the old "(?:^|\n)" anchor could not see it.
    round_text = (
        "I have the repository structure now and the tool dispatch module is "
        "where path mapping happens:Let me continue investigating the source "
        "code to find where the routing decision is made:"
    )

    events = _run_loop(monkeypatch, round_text, max_rounds=5)

    guard = next((e for e in events if e.get("type") == "intent_nudge_exhausted"), None)
    assert guard is not None, events
    assert guard["reason"] == "intent_without_action_nudge_cap"
    # "continue" only matches via the clause-boundary anchor.
    assert "continue" in guard["matched"]


def test_no_intent_nudge_for_let_me_know(monkeypatch):
    _patch_common(monkeypatch)

    events = _run_loop(
        monkeypatch,
        "Sounds reasonable. Let me know what you think about this approach.",
        max_rounds=5,
    )

    assert not any(e.get("type") == "intent_nudge_exhausted" for e in events), events
    assert not any(e.get("type") == "agent_step" for e in events), events


def test_no_intent_nudge_when_round_ends_on_a_real_answer(monkeypatch):
    _patch_common(monkeypatch)

    # A mid-round promise followed by an actual answer: the turn did not end on
    # the promise, so the closing paragraph decides — no nudge, no guard.
    events = _run_loop(
        monkeypatch,
        "Let me check the earlier results first.\n\n"
        "All three hosts responded. The endpoint returns 200 and the "
        "certificate chain is valid, so nothing is wrong server side.",
        max_rounds=5,
    )

    assert not any(e.get("type") == "intent_nudge_exhausted" for e in events), events
