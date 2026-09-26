"""Group chat must hand peer replies over as attributed assistant turns, before the question.

Regression coverage for the verbatim-parroting failure in group chat. group.js used
to inject a peer's complete answer into the next participant's session as a ``user``
message and post the user's question *after* it, so the prompt ended
``[user: peer answer][user: question]`` - and the next speaker, frequently the same
model, reproduced that answer word for word.

The framing helpers are pure and the round loops only need fetch/DOM stubs, so both
turn modes are exercised for real under node (same harness style as
``tests/test_agent_round_model_provenance_ui.py``).
"""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


_SOURCE = (
    Path(__file__).resolve().parents[1] / "static" / "js" / "group.js"
).read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None


def _function_source(name):
    """Return a function's source from group.js, without the `export` keyword."""
    match = re.search(
        rf"^(?:export )?(?:async )?function {name}\(.*?^\}}",
        _SOURCE,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"{name} not found in group.js"
    return re.sub(r"^export ", "", match.group(0), count=1, flags=re.MULTILINE)


def _const_source(name):
    """Return a module constant's source from group.js, without the `export` keyword."""
    match = re.search(rf"^export const {name} =.*?;$", _SOURCE, re.MULTILINE | re.DOTALL)
    assert match, f"{name} not found in group.js"
    return re.sub(r"^export ", "", match.group(0), count=1, flags=re.MULTILINE)


def _run_node(source):
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=source,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_peer_replies_are_delivered_as_attributed_assistant_turns():
    posts = _run_node("\n".join([
        "let _participantSessions = ['s0', 's1', 's2'];",
        "const API_BASE = '';",
        "const posts = [];",
        "async function fetch(url, opts) {",
        "  posts.push({url, body: JSON.parse(opts.body)});",
        "  return {ok: true, status: 200};",
        "}",
        _function_source("buildPeerTurnBlock"),
        _function_source("_injectPeerBlocks"),
        "await _injectPeerBlocks(1, [",
        "  {idx: 0, name: 'A', text: 'A thinks the dragon is asleep.'},",
        "  {idx: 1, name: 'B', text: 'B private draft, must not come back to B.'},",
        "]);",
        "console.log(JSON.stringify(posts));",
    ]))

    assert len(posts) == 1, "one peer turn per participant, not one request per peer"
    assert posts[0]["url"] == "/api/session/s1/inject_messages"
    messages = posts[0]["body"]["messages"]
    assert len(messages) == 1, "peers are bundled into a single turn"
    message = messages[0]
    assert message["role"] == "assistant", "a peer reply must not be posted as a user turn"
    assert message["metadata"]["group_peer"] is True
    assert message["metadata"]["group_speakers"] == ["A"]
    assert "[A]: A thinks the dragon is asleep." in message["content"]
    assert "must not come back to B" not in message["content"], "no self-echo"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_nothing_is_injected_when_a_participant_is_the_only_speaker():
    posts = _run_node("\n".join([
        "let _participantSessions = ['s0', 's1'];",
        "const API_BASE = '';",
        "const posts = [];",
        "async function fetch(url) { posts.push(url); return {ok: true, status: 200}; }",
        _function_source("buildPeerTurnBlock"),
        _function_source("_injectPeerBlocks"),
        "await _injectPeerBlocks(0, [{idx: 0, name: 'A', text: 'only me so far'}]);",
        "console.log(JSON.stringify(posts));",
    ]))
    assert posts == []


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_peer_injection_failure_is_reported_not_thrown():
    result = _run_node("\n".join([
        "let _participantSessions = ['s0', 's1'];",
        "const API_BASE = '';",
        "async function fetch() { return {ok: false, status: 500}; }",
        _function_source("buildPeerTurnBlock"),
        _function_source("_injectPeerBlocks"),
        "const delivered = await _injectPeerBlocks(0, [{idx: 1, name: 'B', text: 'hello'}]);",
        "console.log(JSON.stringify({delivered}));",
    ]))
    assert result == {"delivered": False}


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_round_robin_delivers_peers_before_the_question_never_with_it():
    log = _run_node("\n".join([
        # Deterministic Fisher-Yates: Math.random() === 0 yields the order 1, 2, 0.
        "Math.random = () => 0;",
        "let _models = [",
        "  {mid: 'm-a', display: 'Model A', _groupName: 'A'},",
        "  {mid: 'm-b', display: 'Model B', _groupName: 'B'},",
        "  {mid: 'm-c', display: 'Model C', _groupName: 'C'},",
        "];",
        "let _participantSessions = ['s0', 's1', 's2'];",
        "let _abortControllers = [];",
        "const API_BASE = '';",
        "const log = [];",
        "const uiModule = {scrollHistory() {}};",
        "function _createGroupBubble() { return {dataset: {}}; }",
        "async function fetch(url, opts) {",
        "  log.push({kind: 'peer', url, messages: JSON.parse(opts.body).messages});",
        "  return {ok: true, status: 200};",
        "}",
        "async function _streamToHolder(idx, sessionId, msg, holder) {",
        "  log.push({kind: 'question', idx, sessionId, msg});",
        "  holder.dataset.raw = 'reply from ' + _models[idx]._groupName;",
        "}",
        "function _saveState() {}",
        _function_source("buildPeerTurnBlock"),
        _function_source("_injectPeerBlocks"),
        _function_source("_sendRoundRobin"),
        "await _sendRoundRobin('who wins the duel?', {});",
        "console.log(JSON.stringify(log));",
    ]))

    sequence = [
        (entry["kind"], entry["sessionId"] if entry["kind"] == "question" else entry["url"])
        for entry in log
    ]
    assert sequence == [
        ("question", "s1"),
        ("peer", "/api/session/s2/inject_messages"),
        ("question", "s2"),
        ("peer", "/api/session/s0/inject_messages"),
        ("question", "s0"),
    ], "each peer turn lands before the question it precedes, and only then"

    questions = [entry for entry in log if entry["kind"] == "question"]
    assert [entry["msg"] for entry in questions] == ["who wins the duel?"] * 3

    peer_turns = [entry["messages"] for entry in log if entry["kind"] == "peer"]
    for messages in peer_turns:
        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"
        assert "who wins the duel?" not in messages[0]["content"], (
            "the question must never be bundled into the peer turn above it"
        )
    assert "[B]: reply from B" in peer_turns[0][0]["content"]
    assert "[C]" not in peer_turns[0][0]["content"]
    assert "[B]: reply from B" in peer_turns[1][0]["content"]
    assert "[C]: reply from C" in peer_turns[1][0]["content"]
    assert "[A]" not in peer_turns[1][0]["content"], "a participant never receives its own reply"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_parallel_round_shares_each_reply_with_the_other_participants():
    posts = _run_node("\n".join([
        "let _models = [{_groupName: 'A'}, {_groupName: 'B'}, {_groupName: 'C'}];",
        "let _participantSessions = ['s0', 's1', 's2'];",
        "const API_BASE = '';",
        "const posts = [];",
        "async function fetch(url, opts) {",
        "  posts.push({url, messages: JSON.parse(opts.body).messages});",
        "  return {ok: true, status: 200};",
        "}",
        "const holders = [",
        "  {dataset: {raw: 'A answer'}},",
        "  {dataset: {raw: 'B answer'}},",
        "  {dataset: {raw: ''}},",
        "];",
        _function_source("buildPeerTurnBlock"),
        _function_source("_injectPeerBlocks"),
        _function_source("_syncAllResponses"),
        "await _syncAllResponses(holders);",
        "console.log(JSON.stringify(posts));",
    ]))

    assert [post["url"] for post in posts] == [
        "/api/session/s0/inject_messages",
        "/api/session/s1/inject_messages",
        "/api/session/s2/inject_messages",
    ], "one request per participant, not one per (speaker, listener) pair"
    by_url = {post["url"]: post["messages"][0] for post in posts}
    assert by_url["/api/session/s0/inject_messages"]["role"] == "assistant"
    assert "[A]" not in by_url["/api/session/s0/inject_messages"]["content"], "no self-echo"
    assert "[B]: B answer" in by_url["/api/session/s0/inject_messages"]["content"]
    assert "[A]: A answer" in by_url["/api/session/s1/inject_messages"]["content"]
    assert "[A]: A answer" in by_url["/api/session/s2/inject_messages"]["content"]
    assert "[B]: B answer" in by_url["/api/session/s2/inject_messages"]["content"]


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_peers_sharing_a_model_still_see_each_other_and_are_told_not_to_parrot():
    result = _run_node("\n".join([
        _const_source("GROUP_ETIQUETTE"),
        _function_source("groupPeerNames"),
        _function_source("buildGroupSystemPrompt"),
        "const picked = [",
        "  {mid: 'shared', display: 'Shared Model A', character: {characterName: 'A'}},",
        "  {mid: 'shared', display: 'Shared Model B'},",
        "  {mid: 'other', display: 'Other', character: {characterName: 'Cass'}},",
        "];",
        "const prompt = buildGroupSystemPrompt({",
        "  displayName: 'A',",
        "  otherNames: groupPeerNames(picked, 0).join(', '),",
        "});",
        "const persona = buildGroupSystemPrompt({",
        "  displayName: 'A', otherNames: 'B', characterPrompt: 'You are a dragon.',",
        "});",
        "console.log(JSON.stringify({",
        "  peers0: groupPeerNames(picked, 0),",
        "  peers1: groupPeerNames(picked, 1),",
        "  prompt, persona,",
        "}));",
    ]))

    assert result["peers0"] == ["Shared Model B", "Cass"], "character name, else the model display"
    assert result["peers1"] == ["A", "Cass"], "same-model peers are not hidden from each other"
    assert result["prompt"].startswith(
        "You are A in a group chat with Shared Model B, Cass and the user."
    )
    assert 'begins with "[Name]:"' in result["prompt"]
    assert "never echo, paraphrase, or continue them verbatim" in result["prompt"]
    assert "is always the final message" in result["prompt"]
    assert result["persona"].startswith("You are a dragon.")
    assert "Stay in character." in result["persona"]


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_participants_sharing_one_model_are_detected_and_can_be_diversified():
    result = _run_node("\n".join([
        _function_source("duplicateParticipantModels"),
        _function_source("diversifyParticipants"),
        "const picked = [",
        "  {mid: 'm1', display: 'M1', url: 'http://local:1234/v1', character: {characterName: 'A'}},",
        "  {mid: 'm1', display: 'M1', url: 'http://local:1234/v1', character: {characterName: 'B'}},",
        "  {mid: 'm1', display: 'M1', url: 'http://local:1234/v1', character: {characterName: 'C'}},",
        "];",
        "const available = [",
        "  {mid: 'm1', display: 'M1', url: 'http://local:1234/v1'},",
        "  {mid: 'm2', display: 'M2', url: 'http://local:1234/v1'},",
        "  {mid: 'm3', display: 'M3', url: 'http://remote:9999/v1'},",
        "];",
        "const duplicateGroups = duplicateParticipantModels(picked)",
        "  .map(g => ({mid: g.mid, count: g.indices.length}));",
        "const fixed = diversifyParticipants(picked, available);",
        "const stuck = diversifyParticipants(picked.slice(0, 2), [available[0]]);",
        "console.log(JSON.stringify({",
        "  duplicateGroups,",
        "  mids: fixed.picked.map(p => p.mid),",
        "  chars: fixed.picked.map(p => p.character.characterName),",
        "  changes: fixed.changes.map(c => c.to),",
        "  unresolved: fixed.unresolved,",
        "  stuckMids: stuck.picked.map(p => p.mid),",
        "  stuckUnresolved: stuck.unresolved,",
        "}));",
    ]))

    assert result["duplicateGroups"] == [{"mid": "m1", "count": 3}]
    assert result["mids"] == ["m1", "m2", "m3"], "same-endpoint models are preferred, then any"
    assert result["chars"] == ["A", "B", "C"], "assigned characters survive the swap"
    assert result["changes"] == ["M2", "M3"]
    assert result["unresolved"] == []
    assert result["stuckMids"] == ["m1", "m1"], "with no free model the line-up is left alone"
    assert result["stuckUnresolved"] == ["M1"]


def test_group_turns_request_plain_chat_mode():
    """Source-pinned by design: `_streamToHolder` builds its own browser `FormData`,
    so there is no node seam to drive it. The field is the invariant that keeps group
    turns on the plain-chat branch - chat_routes.py reads form `mode` and falls through
    to the agent loop (whole tool catalogue, 10k-14k prompt tokens, "No active plan
    found." in the bubble) whenever it is empty."""
    assert "fd.append('mode', 'chat');" in _SOURCE
    assert "if (!res.ok || !res.body) {" in _SOURCE, "a failed round must surface"


def test_peer_replies_are_never_injected_as_user_turns():
    """Source-pinned by design: an absence check over the framing that caused the
    collapse. The user-role peer injection is gone, while the parent-session question
    is still stored as a user turn."""
    assert "content: `[${m._groupName || m.display}]: ${response}`" not in _SOURCE
    assert "content: `[${model._groupName || model.display}]: ${response}`" not in _SOURCE
    assert "{ role: 'user', content: msg }" in _SOURCE


def test_group_preset_load_does_not_coerce_a_missing_model():
    """Source-pinned by design: the preset chip handler is built inside `_initGroupTab`
    against live DOM nodes, so it cannot be driven standalone. The invariant is that a
    stale `modelId` no longer silently becomes `models[0]`."""
    assert "models.find(m => m.mid === p.modelId) || models[0]" not in _SOURCE
    assert "models.find(m => m.mid === p.modelId) || null" in _SOURCE
