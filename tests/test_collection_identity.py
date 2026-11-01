import json

import pytest

from sovite.collection_identity import (CollectionCredential, InvalidCollectionInput,
    authenticate_collection, create_request_identity, parse_closed_json, validate_credentials)


def request_body():
    return {"schema_version": "sovite.collection-session/v1", "workspace_id": "workspace",
            "repository_id": "repo", "generation": "sha256:" + "1" * 64,
            "suite_version": "suite-v1", "suite_digest": "2" * 64,
            "deployment_digest": "3" * 64, "permit_digest": "4" * 64,
            "deadline_ms": 100, "retain_until_ms": 200}


def test_canonical_request_identity_and_changed_content():
    body = request_body()
    _, canonical, digest = create_request_identity(json.dumps(body).encode())
    _, canonical2, digest2 = create_request_identity(json.dumps(body, indent=1, sort_keys=True).encode())
    assert (canonical, digest) == (canonical2, digest2)
    body["deadline_ms"] = 101
    assert create_request_identity(json.dumps(body).encode())[2] != digest


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":{"b":1,"b":2}}',
                                 b'{"a":1.0}', b'{"a":NaN}', b'[]', b'\xff', b' ' * 16385])
def test_reject_ambiguous_or_unbounded_json(raw):
    with pytest.raises(InvalidCollectionInput):
        parse_closed_json(raw)


def test_closed_create_and_integer_bounds():
    for change in ({"new_field": "x"}, {"deadline_ms": True}, {"deadline_ms": 2**53},
                   {"workspace_id": "invalid/selector"}, {"retain_until_ms": 99}):
        with pytest.raises(InvalidCollectionInput):
            create_request_identity(json.dumps(request_body() | change).encode())


def credentials():
    return tuple(CollectionCredential(role, role, role * 3) for role in
                 ("collection-operator", "collection-agent", "collection-reconciler", "collection-recovery"))


def test_role_separation_and_safe_representation():
    values = credentials()
    validate_credentials(values, reserved_tokens=("existing-secret",))
    assert authenticate_collection(values[1].token, values) == values[1]
    assert authenticate_collection("existing-secret", values) is None
    assert values[1].token not in repr(values[1])
    with pytest.raises(InvalidCollectionInput):
        validate_credentials(values, reserved_tokens=(values[1].token,))
    with pytest.raises(InvalidCollectionInput):
        validate_credentials(values[:-1] + (CollectionCredential(values[0].principal_id,
            "collection-recovery", "different-credential-value"),), reserved_tokens=())
