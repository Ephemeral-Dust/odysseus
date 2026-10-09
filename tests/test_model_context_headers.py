"""Regression: model_context's context-window probes must forward the endpoint's
headers, so auth-protected local servers (e.g. Strata) report their real window
instead of falling back to DEFAULT_CONTEXT — which collapses the agent input
budget to ~6k and trims a 128K model's history down to a few messages.
"""
from src import endpoint_resolver
from src import model_context


class _Resp:
    def __init__(self, payload=None, ok=True):
        self.is_success = ok
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def test_query_context_length_forwards_headers(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen[url] = kwargs.get("headers")
        return _Resp(ok=False)

    monkeypatch.setattr(model_context.httpx, "get", fake_get)
    monkeypatch.setattr(model_context, "_lookup_known", lambda m: None)
    monkeypatch.setattr(model_context, "_configured_endpoint_kind", lambda url: "local")
    monkeypatch.setattr(
        endpoint_resolver,
        "build_models_url",
        lambda url: "http://192.168.0.43:8080/v1/models",
    )

    headers = {"Authorization": "Bearer sekret"}
    ctx, known = model_context._query_context_length(
        "http://192.168.0.43:8080/v1", "swift-1.5-iq2_xs", headers=headers
    )

    # No endpoint report (fake is not-success) -> bare fallback, but the headers
    # must still have been sent to both probes so a real server can answer.
    assert (ctx, known) == (model_context.DEFAULT_CONTEXT, False)
    assert seen["http://192.168.0.43:8080/slots"] == headers
    assert seen["http://192.168.0.43:8080/v1/models"] == headers
