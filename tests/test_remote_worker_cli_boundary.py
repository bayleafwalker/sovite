from sovite.remote_worker_cli import _CONTROL_OR_STORE_ENV, main


def test_remote_worker_cli_refuses_ambient_database_and_artifact_credentials(monkeypatch, capsys):
    for name in _CONTROL_OR_STORE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SOVITE_DATABASE_URL", "postgresql+psycopg://secret-value")
    assert main([]) == 2
    output = capsys.readouterr().err
    assert "SOVITE_DATABASE_URL" in output
    assert "secret-value" not in output

    monkeypatch.delenv("SOVITE_DATABASE_URL")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "storage-secret-value")
    assert main([]) == 2
    output = capsys.readouterr().err
    assert "AWS_SECRET_ACCESS_KEY" in output
    assert "storage-secret-value" not in output


def test_remote_worker_cli_refuses_control_execution_token_map(monkeypatch, capsys):
    for name in _CONTROL_OR_STORE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SOVITE_EXECUTION_TOKENS", '{"workload":"server-token"}')
    assert main([]) == 2
    output = capsys.readouterr().err
    assert "SOVITE_EXECUTION_TOKENS" in output
    assert "server-token" not in output
