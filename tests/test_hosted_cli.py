"""S3a: hosted compile and activate are authenticated control-plane API clients.

These commands must not need database authority, must not read the hosting
envelope, and must keep the server-enforced author/activator boundary.
"""
from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from sovite import cli, control_client, policy
from sovite.api import create_app
from sovite.db import BundleRevision
from sovite.settings import Settings

from test_hosting import ACTIVATOR, AUTHOR, CALLER, OPERATOR, build_hosted, control_transport


ROOT = Path(__file__).parents[1]
TOKEN = "a" * 32
GEN = "sha256:" + "a" * 64
BASE_ENV = {"SOVITE_CONTROL_URL": "https://control.example", "SOVITE_AUTHOR_TOKEN": TOKEN}
ACTIVATE_ENV = {"SOVITE_CONTROL_URL": "https://control.example", "SOVITE_ACTIVATOR_TOKEN": TOKEN}


@pytest.fixture
def hosted(tmp_path):
    return build_hosted(tmp_path)


def clean_env(monkeypatch, settings):
    for name in list(os.environ):
        if name.startswith("SOVITE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SOVITE_HOSTING_ENVELOPE", settings.hosting_envelope_path)
    monkeypatch.setenv("SOVITE_CONTROL_URL", "https://control.example")
    monkeypatch.setenv("SOVITE_DATABASE_URL", "sqlite:////nonexistent/poisoned-control.db")
    monkeypatch.setenv("SOVITE_BUNDLE_PATH", settings.bundle_path)


def install_transport(monkeypatch, client):
    def factory(*, verify, transport=None):
        assert verify is True
        return httpx.Client(timeout=httpx.Timeout(control_client.CONTROL_TIMEOUT_SECONDS), trust_env=False,
                            follow_redirects=False, verify=False, transport=control_transport(client))
    monkeypatch.setattr(control_client, "_client", factory)


def mock_client_factory(*, handler):
    def factory(*, verify, transport=None):
        return httpx.Client(timeout=httpx.Timeout(control_client.CONTROL_TIMEOUT_SECONDS), trust_env=False,
                            follow_redirects=False, verify=False, transport=httpx.MockTransport(handler))
    return factory


def forbid_local_runtime(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Hosted control commands must not construct local runtime state")

    def unloaded(*args, **kwargs):
        pytest.fail("Hosted control commands must not load settings")

    monkeypatch.setattr(cli, "Platform", forbidden)
    monkeypatch.setattr(cli, "Database", forbidden)
    monkeypatch.setattr(Settings, "from_env", classmethod(unloaded))
    monkeypatch.setattr(Settings, "from_database_env", classmethod(unloaded))


def no_request(**kwargs):
    pytest.fail("No request may be sent before configuration validation succeeds")


def test_hosted_compile_and_activate_use_api_without_database(hosted, monkeypatch, capsys):
    settings, platform, bundle, _ = hosted
    clean_env(monkeypatch, settings)
    # A missing envelope file proves the client branch never reads it; the serving
    # process remains the authority for the envelope it already loaded.
    monkeypatch.setenv("SOVITE_HOSTING_ENVELOPE", str(settings.hosting_envelope_path) + ".missing")
    monkeypatch.setenv("SOVITE_AUTHOR_TOKEN", AUTHOR)
    bundle["metadata"]["version"] = "s3a-draft"
    Path(settings.bundle_path).write_text(yaml.safe_dump(bundle))
    before = platform.active()[1]
    with TestClient(create_app(settings, platform)) as client:
        install_transport(monkeypatch, client)
        forbid_local_runtime(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["sovite", "compile"])
        cli.main()
        compiled = json.loads(capsys.readouterr().out)
        assert compiled["status"] == "compiled" and compiled["generation"] != before
        assert platform.active()[1] == before
        with platform.db.sessions() as session:
            assert session.get(BundleRevision, compiled["generation"]) is not None
        monkeypatch.delenv("SOVITE_AUTHOR_TOKEN")
        monkeypatch.setenv("SOVITE_ACTIVATOR_TOKEN", ACTIVATOR)
        monkeypatch.setattr(sys, "argv", ["sovite", "activate", compiled["generation"]])
        cli.main()
        activated = json.loads(capsys.readouterr().out)
        assert activated == {"status": "activated", "generation": compiled["generation"]}
        assert platform.active()[1] == compiled["generation"]


def test_hosted_cli_role_boundary_is_server_enforced(hosted, monkeypatch, capsys):
    settings, platform, bundle, _ = hosted
    clean_env(monkeypatch, settings)
    bundle["metadata"]["version"] = "role-boundary"
    Path(settings.bundle_path).write_text(yaml.safe_dump(bundle))
    before = platform.active()[1]
    with TestClient(create_app(settings, platform)) as client:
        install_transport(monkeypatch, client)
        monkeypatch.setenv("SOVITE_AUTHOR_TOKEN", AUTHOR)
        monkeypatch.setattr(sys, "argv", ["sovite", "compile"])
        cli.main()
        generation = json.loads(capsys.readouterr().out)["generation"]
        assert platform.active()[1] == before
        for token in (OPERATOR, CALLER, AUTHOR):
            monkeypatch.setenv("SOVITE_ACTIVATOR_TOKEN", token)
            monkeypatch.setattr(sys, "argv", ["sovite", "activate", generation])
            with pytest.raises(SystemExit) as error:
                cli.main()
            assert error.value.code == 1
            assert platform.active()[1] == before
            capsys.readouterr()
        for token in (OPERATOR, CALLER, ACTIVATOR):
            monkeypatch.setenv("SOVITE_AUTHOR_TOKEN", token)
            monkeypatch.setattr(sys, "argv", ["sovite", "compile"])
            with pytest.raises(SystemExit) as error:
                cli.main()
            assert error.value.code == 1
            capsys.readouterr()
        monkeypatch.setenv("SOVITE_ACTIVATOR_TOKEN", ACTIVATOR)
        monkeypatch.setattr(sys, "argv", ["sovite", "activate", generation])
        cli.main()
        assert json.loads(capsys.readouterr().out) == {"status": "activated", "generation": generation}
        assert platform.active()[1] == generation


def test_hosted_cli_reads_only_the_selected_role(hosted, monkeypatch, capsys):
    settings, platform, bundle, _ = hosted
    clean_env(monkeypatch, settings)
    bundle["metadata"]["version"] = "role-scoped"
    Path(settings.bundle_path).write_text(yaml.safe_dump(bundle))
    # Malformed unrelated role, caller, database and qualification settings must not
    # be read by a hosted control command.
    monkeypatch.setenv("SOVITE_CALLER_TOKENS", "{not-json")
    monkeypatch.setenv("SOVITE_QUALIFICATION_ISSUERS", "not-json")
    monkeypatch.setenv("SOVITE_DATABASE_SCHEMA", "Public")
    monkeypatch.setenv("SOVITE_OPERATOR_TOKEN", "short")
    monkeypatch.setenv("SOVITE_AUTHORITY_TOKEN", "short")
    monkeypatch.setenv("SOVITE_AUTHOR_TOKEN", AUTHOR)
    with TestClient(create_app(settings, platform)) as client:
        install_transport(monkeypatch, client)
        monkeypatch.setattr(sys, "argv", ["sovite", "compile"])
        cli.main()
        generation = json.loads(capsys.readouterr().out)["generation"]
        monkeypatch.setenv("SOVITE_AUTHOR_TOKEN", "short")
        monkeypatch.setenv("SOVITE_ACTIVATOR_TOKEN", ACTIVATOR)
        monkeypatch.setattr(sys, "argv", ["sovite", "activate", generation])
        cli.main()
        assert platform.active()[1] == generation


DELIMITED_CONTROL_URLS = [
    "https://control.example?", "https://control.example#",
    "https://control.example/?", "https://control.example/#",
]


@pytest.mark.parametrize("url", [
    None, "", "http://control.example", "https://user:pass@control.example",
    "https://control.example/tenant", "https://control.example?debug=1",
    "https://control.example#fragment", "https://control.example:bad",
    *DELIMITED_CONTROL_URLS,
])
def test_compile_rejects_invalid_control_url_before_request(monkeypatch, url):
    monkeypatch.setattr(control_client, "_client", no_request)
    env = {"SOVITE_AUTHOR_TOKEN": TOKEN}
    if url is not None:
        env["SOVITE_CONTROL_URL"] = url
    with pytest.raises(control_client.ControlError):
        control_client.compile_command(str(ROOT / "config/platform.local.yaml"), env=env)


def test_control_target_normalization_and_ca(tmp_path):
    assert control_client.target_from_env({"SOVITE_CONTROL_URL": "https://control.example"}) == control_client.ControlTarget("https://control.example", True)
    assert control_client.target_from_env({"SOVITE_CONTROL_URL": "https://control.example:8443/"}) == control_client.ControlTarget("https://control.example:8443", True)
    certificate = tmp_path / "ca.pem"
    certificate.write_text("certificate placeholder")
    assert control_client.target_from_env({"SOVITE_CONTROL_URL": "https://control.example", "SOVITE_CONTROL_CA_FILE": str(certificate)}) == control_client.ControlTarget("https://control.example", str(certificate))
    with pytest.raises(control_client.ControlError, match="CA_FILE"):
        control_client.target_from_env({"SOVITE_CONTROL_URL": "https://control.example", "SOVITE_CONTROL_CA_FILE": str(tmp_path / "missing.pem")})
    for url in DELIMITED_CONTROL_URLS:
        with pytest.raises(control_client.ControlError, match="query or fragment"):
            control_client.target_from_env({"SOVITE_CONTROL_URL": url})


@pytest.mark.parametrize("operation", ["compile", "activate"])
@pytest.mark.parametrize("url", DELIMITED_CONTROL_URLS)
def test_delimited_control_url_sends_no_request(operation, url):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(201, content=_encoded({"status": "compiled", "generation": GEN}))

    env = {**BASE_ENV, **ACTIVATE_ENV, "SOVITE_CONTROL_URL": url}
    transport = httpx.MockTransport(handler)
    with pytest.raises(control_client.ControlError, match="query or fragment"):
        if operation == "compile":
            control_client.compile_command(str(ROOT / "config/platform.local.yaml"), env=env, transport=transport)
        else:
            control_client.activate_command(GEN, env=env, transport=transport)
    assert calls == []


@pytest.mark.parametrize("token", ["", "a" * 23, "a" * 30 + "\n", "a" * 30 + "\x01"])
def test_compile_rejects_invalid_author_token_before_request(monkeypatch, token):
    monkeypatch.setattr(control_client, "_client", no_request)
    with pytest.raises(control_client.ControlError):
        control_client.compile_command(str(ROOT / "config/platform.local.yaml"), env={**BASE_ENV, "SOVITE_AUTHOR_TOKEN": token})


@pytest.mark.parametrize("token", ["", "a" * 23, "a" * 30 + "\r", "a" * 30 + "\x7f"])
def test_activate_rejects_invalid_activator_token_before_request(monkeypatch, token):
    monkeypatch.setattr(control_client, "_client", no_request)
    with pytest.raises(control_client.ControlError):
        control_client.activate_command(GEN, env={**ACTIVATE_ENV, "SOVITE_ACTIVATOR_TOKEN": token})


@pytest.mark.parametrize("generation", [
    "", "sha256:", "sha256:" + "a" * 63, "sha256:" + "a" * 65,
    "sha256:" + "A" * 64, "sha256:" + "g" * 64, "sha512:" + "a" * 64,
    "sha256:" + "a" * 64 + "\n", "sha256:" + "a" * 64 + "/",
])
def test_activate_rejects_invalid_generation_before_request(monkeypatch, generation):
    monkeypatch.setattr(control_client, "_client", no_request)
    with pytest.raises(control_client.ControlError):
        control_client.activate_command(generation, env=ACTIVATE_ENV)


def test_control_client_http_configuration(monkeypatch):
    seen = {}
    real = httpx.Client

    def recording(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(control_client.httpx, "Client", recording)
    control_client._client(verify=True).close()
    assert seen["timeout"] == httpx.Timeout(10.0)
    assert seen["trust_env"] is False
    assert seen["follow_redirects"] is False
    assert seen["verify"] is True
    assert seen["transport"] is None


def test_oversized_bundle_is_rejected_before_transport(tmp_path, monkeypatch):
    bundle = policy.load_bundle(ROOT / "config/platform.local.yaml")[0]
    bundle["metadata"]["description"] = "y" * 16000
    for destination in bundle["destinations"]:
        destination["description"] = "x" * 16000
    path = tmp_path / "oversized.yaml"
    path.write_text(yaml.safe_dump(bundle))
    monkeypatch.setattr(control_client, "_client", no_request)
    with pytest.raises(control_client.ControlError, match="64 KiB"):
        control_client.compile_command(str(path), env=BASE_ENV)


def test_bundle_policy_failure_diagnostic_does_not_echo_values(hosted, monkeypatch, capsys):
    settings, platform, bundle, _ = hosted
    clean_env(monkeypatch, settings)
    canary = "PRIVATE-BUNDLE-CANARY-REVIEW"
    long_value = "PRIVATE-BUNDLE-LONG-VALUE-" + "x" * 20000
    bundle["mode"] = canary
    bundle["metadata"]["description"] = long_value
    Path(settings.bundle_path).write_text(yaml.safe_dump(bundle))
    # Standalone policy diagnostics stay detailed; only the hosted client mapping is bounded.
    with pytest.raises(policy.PolicyError) as standalone:
        policy.load_bundle(settings.bundle_path)
    assert long_value in str(standalone.value)
    monkeypatch.setenv("SOVITE_AUTHOR_TOKEN", AUTHOR)
    monkeypatch.setattr(control_client, "_client", no_request)
    monkeypatch.setattr(sys, "argv", ["sovite", "compile"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert canary not in captured.out + captured.err
    assert long_value not in captured.out + captured.err
    assert "policy validation" in captured.err


def _encoded(payload):
    return json.dumps(payload).encode()


@pytest.mark.parametrize("status,body,headers,match", [
    (302, b"", {"location": "https://elsewhere.example/"}, "status 302"),
    (401, b'{"detail":{"code":"unauthorized"}}', {}, "status 401"),
    (403, b'{"detail":{"code":"forbidden"}}', {}, "status 403"),
    (200, _encoded({"status": "compiled", "generation": GEN}), {}, "status 200"),
    (201, b"not-json", {}, "invalid response"),
    (201, _encoded({"status": "compiled"}), {}, "invalid response"),
    (201, _encoded({"status": "compiled", "generation": GEN, "detail": "extra"}), {}, "invalid response"),
    (201, _encoded({"status": "activated", "generation": GEN}), {}, "unexpected state"),
    (201, _encoded({"status": "compiled", "generation": "sha256:" + "A" * 64}), {}, "invalid generation"),
    (201, b"x" * 70000, {}, "64 KiB"),
    (201, gzip.compress(_encoded({"status": "compiled", "generation": GEN})), {"content-encoding": "gzip"}, "encoded response"),
])
def test_compile_rejects_bad_responses_without_retry(monkeypatch, status, body, headers, match):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=body, headers=headers)

    with pytest.raises(control_client.ControlError, match=match):
        control_client.compile_command(str(ROOT / "config/platform.local.yaml"), env=BASE_ENV, transport=httpx.MockTransport(handler))
    assert len(calls) == 1


def test_activation_response_must_match_requested_generation():
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://control.example/api/bundles/" + GEN + "/activate"
        return httpx.Response(200, content=_encoded({"status": "activated", "generation": "sha256:" + "b" * 64}))

    with pytest.raises(control_client.ControlError, match="different generation"):
        control_client.activate_command(GEN, env=ACTIVATE_ENV, transport=httpx.MockTransport(handler))
    assert len(calls) == 1


@pytest.mark.parametrize("operation", ["compile", "activate"])
def test_transport_failures_are_not_retried(operation):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("upstream detail that must not be printed")

    if operation == "compile":
        with pytest.raises(control_client.ControlError, match="did not complete"):
            control_client.compile_command(str(ROOT / "config/platform.local.yaml"), env=BASE_ENV, transport=httpx.MockTransport(handler))
    else:
        with pytest.raises(control_client.ControlError, match="outcome is unknown"):
            control_client.activate_command(GEN, env=ACTIVATE_ENV, transport=httpx.MockTransport(handler))
    assert len(calls) == 1


def test_diagnostics_never_echo_upstream_text(hosted, monkeypatch, capsys):
    settings, platform, bundle, _ = hosted
    clean_env(monkeypatch, settings)
    monkeypatch.setenv("SOVITE_AUTHOR_TOKEN", AUTHOR)
    canary = "CANARY-PRIVATE-UPSTREAM-BODY"

    def denial(request):
        return httpx.Response(403, content=_encoded({"detail": {"code": "forbidden", "message": canary}}))

    monkeypatch.setattr(control_client, "_client", mock_client_factory(handler=denial))
    monkeypatch.setattr(sys, "argv", ["sovite", "compile"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    denied = capsys.readouterr()
    assert "403" in denied.err
    assert canary not in denied.out + denied.err

    def timeout(request):
        raise httpx.ReadTimeout(canary)

    monkeypatch.setenv("SOVITE_ACTIVATOR_TOKEN", ACTIVATOR)
    monkeypatch.setattr(control_client, "_client", mock_client_factory(handler=timeout))
    monkeypatch.setattr(sys, "argv", ["sovite", "activate", GEN])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    lost = capsys.readouterr()
    assert "outcome is unknown" in lost.err and "current server state" in lost.err
    assert canary not in lost.out + lost.err


def test_hosted_reload_refused_before_database(hosted, monkeypatch, capsys):
    settings, platform, bundle, _ = hosted
    clean_env(monkeypatch, settings)
    monkeypatch.setattr(sys, "argv", ["sovite", "reload"])
    forbid_local_runtime(monkeypatch)
    before = platform.active()[1]
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    message = capsys.readouterr().err
    assert "reload" in message and "compile" in message and "activate" in message
    assert platform.active()[1] == before
