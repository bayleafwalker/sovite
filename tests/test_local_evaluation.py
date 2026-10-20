import httpx
import pytest
from sovite.local_evaluation import evaluation_bundle, local_model


def test_evaluation_bundle_only_exposes_public_fixture_without_tools():
    model = {
        "id": "handover-model",
        "name": "Handover",
        "adapter": "ollama",
        "endpoint": "http://127.0.0.1:11531",
        "model": "explicit-instruct",
        "destination": "private-runtime",
    }
    bundle = evaluation_bundle(model)
    assert bundle["tools"] == []
    assert (
        bundle["grants"][0]["input_class"] == "C0"
        and bundle["grants"][0]["output_class"] == "C0"
    )
    assert bundle["grants"][0]["max_attempts"] == 1


def test_known_thinking_only_model_is_refused_before_source_is_sent(monkeypatch):
    original = httpx.Client
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "thinking-model", "digest": "a" * 64}]}
            )
        return httpx.Response(
            200, json={"model_info": {"general.finetune": "Thinking"}}
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(ValueError, match="non-thinking"):
        local_model("http://127.0.0.1:11531", "thinking-model", "a" * 64)
    assert requests == ["/api/tags", "/api/show"]


def test_model_digest_mismatch_is_refused_without_chat(monkeypatch):
    original = httpx.Client
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(
            200, json={"models": [{"name": "explicit-model", "digest": "b" * 64}]}
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(ValueError, match="not installed"):
        local_model("http://127.0.0.1:11531", "explicit-model", "a" * 64)
    assert requests == ["/api/tags"]


def test_initialization_failure_closes_owned_listener_and_engine(monkeypatch, tmp_path):
    import sovite.local_evaluation as evaluation

    sockets = []
    disposed = []
    original = evaluation.socket.socket

    def listener(*args, **kwargs):
        value = original(*args, **kwargs)
        sockets.append(value)
        return value

    def fail(platform):
        original_dispose = platform.db.engine.dispose

        def dispose():
            disposed.append(True)
            original_dispose()

        monkeypatch.setattr(platform.db.engine, "dispose", dispose)
        raise RuntimeError("initialization failed")

    monkeypatch.setattr(evaluation.socket, "socket", listener)
    monkeypatch.setattr(evaluation.Platform, "initialize", fail)
    model = {
        "id": "handover-model",
        "name": "Handover",
        "adapter": "ollama",
        "endpoint": "http://127.0.0.1:11531",
        "model": "explicit-instruct",
        "destination": "private-runtime",
    }
    with pytest.raises(RuntimeError, match="initialization failed"):
        with evaluation.local_evaluation(tmp_path / "owned", model):
            pass
    assert len(sockets) == 1 and sockets[0].fileno() == -1
    assert disposed == [True]
