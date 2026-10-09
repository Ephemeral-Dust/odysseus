"""Regression tests for ``endpoint_supports_vision``: the llama.cpp / OpenAI-style
probes (``/health``, ``/v1/models`` ``architecture.input_modalities``, ``/props``
``modalities.vision``) that let Strata-style servers (e.g. ``swift-1.5-iq2_xs``)
advertise image input even though the LM Studio native probe 404s and the name
heuristic misses the id. LM Studio precedence and header forwarding are pinned.
"""
import pytest

from src import chat_helpers


class _Resp:
    def __init__(self, payload=None, ok=True):
        self.is_success = ok
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_cache():
    chat_helpers._lmstudio_models_cache.clear()
    yield
    chat_helpers._lmstudio_models_cache.clear()


def _patch(monkeypatch, routes):
    """Route path-suffix -> _Resp; records every GET's headers. Forces the LM
    Studio probe to return None so the llama.cpp/OpenAI probes run."""
    seen = {}

    def fake_get(url, **kwargs):
        seen[url] = kwargs.get("headers")
        for suffix, resp in routes.items():
            if url.endswith(suffix):
                return resp
        return _Resp(ok=False)

    monkeypatch.setattr(chat_helpers.httpx, "get", fake_get)
    monkeypatch.setattr(chat_helpers, "lmstudio_supports_vision", lambda url, m: None)
    return seen


def test_health_images_true(monkeypatch):
    seen = _patch(monkeypatch, {"/health": _Resp({"images": True})})
    assert chat_helpers.endpoint_supports_vision(
        "http://192.168.0.43:8080/v1/chat/completions", "swift-1.5-iq2_xs"
    ) is True
    assert any(url.endswith("/health") for url in seen)


def test_v1_models_input_modalities_with_headers(monkeypatch):
    seen = _patch(monkeypatch, {
        "/v1/models": _Resp({"data": [{"id": "swift-1.5-iq2_xs",
                                       "architecture": {"input_modalities": ["text", "image"]}}]}),
    })
    headers = {"Authorization": "Bearer sekret"}
    assert chat_helpers.endpoint_supports_vision(
        "http://192.168.0.43:8080/v1/chat/completions", "swift-1.5-iq2_xs", headers=headers
    ) is True
    models_url = next(url for url in seen if url.endswith("/v1/models"))
    assert seen[models_url] == headers


def test_v1_models_text_only(monkeypatch):
    _patch(monkeypatch, {
        "/v1/models": _Resp({"data": [{"id": "swift-1.5-iq2_xs",
                                       "architecture": {"input_modalities": ["text"]}}]}),
    })
    assert chat_helpers.endpoint_supports_vision(
        "http://192.168.0.43:8080", "swift-1.5-iq2_xs"
    ) is False


def test_props_modalities_vision(monkeypatch):
    _patch(monkeypatch, {"/props": _Resp({"model_alias": "swift-1.5-iq2_xs",
                                           "modalities": {"vision": True}})})
    assert chat_helpers.endpoint_supports_vision(
        "http://192.168.0.43:8080", "swift-1.5-iq2_xs"
    ) is True


def test_lmstudio_still_wins(monkeypatch):
    monkeypatch.setattr(chat_helpers, "lmstudio_supports_vision", lambda url, m: True)

    def boom(*a, **k):
        raise AssertionError("httpx must not be probed when LM Studio decides")

    monkeypatch.setattr(chat_helpers.httpx, "get", boom)
    assert chat_helpers.endpoint_supports_vision("http://192.168.0.43:8080", "m") is True


def test_bare_hostname_not_probed(monkeypatch):
    monkeypatch.setattr(chat_helpers, "lmstudio_supports_vision", lambda url, m: None)
    calls = []
    monkeypatch.setattr(chat_helpers.httpx, "get", lambda *a, **k: calls.append(a))
    assert chat_helpers.endpoint_supports_vision("http://host/v1", "qwen2-vl-7b") is None
    assert calls == []


def test_model_supports_vision_forwards_headers(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        chat_helpers,
        "endpoint_supports_vision",
        lambda url, model, headers=None: captured.__setitem__("headers", headers) or True,
    )
    monkeypatch.setattr(chat_helpers, "is_vision_model", lambda n: False)
    assert chat_helpers.model_supports_vision(
        "swift-1.5-iq2_xs", "http://192.168.0.43:8080", headers={"a": "b"}
    ) is True
    assert captured["headers"] == {"a": "b"}
