"""The local qualification explicitly migrates its own disposable database."""
import json
import importlib.util
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'qualify_agentic_setup', Path(__file__).resolve().parents[1] / 'scripts/qualify_agentic_setup.py')
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)


def test_qualification_initializes_explicitly_migrated_disposable_db(tmp_path, monkeypatch):
    fixture = tmp_path / 'source.json'
    fixture.write_text(json.dumps({'classification': 'C0', 'synthetic': True}))
    monkeypatch.setattr(sys, 'argv', ['qualify_agentic_setup', '--model-endpoint', 'http://127.0.0.1:12345',
        '--expected-model-digest', 'synthetic-digest', '--fixture', str(fixture),
        '--output', str(tmp_path / 'output'), '--private-artifact', str(tmp_path / 'artifact')])
    class Response:
        def __init__(self, value): self.value = value
        def raise_for_status(self): return self
        def json(self): return self.value
    monkeypatch.setattr(qualification.httpx, 'get', lambda url, **_: Response(
        {'models': [{'digest': 'synthetic-digest'}]} if url.endswith('/tags') else {'version': 'synthetic'}))
    class ReachedKeys(Exception): pass
    monkeypatch.setattr(qualification, 'initialize_keys', lambda _: (_ for _ in ()).throw(ReachedKeys()))
    with pytest.raises(ReachedKeys):
        qualification.main()
