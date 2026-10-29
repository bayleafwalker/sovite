import pytest

from sovite.resource_gateway_store import OperationConflict, ResourceOperationStore


def test_install_admit_restart_conflict_and_immutable_result(tmp_path):
    path = tmp_path / 'operations.sqlite'
    store = ResourceOperationStore(path)
    with pytest.raises(RuntimeError, match='uninstalled'):
        store.admit('op', {'request': 'one'})
    store.install()
    assert store.admit('op', {'request': 'one'})['state'] == 'admitted'
    restarted = ResourceOperationStore(path)
    assert restarted.admit('op', {'request': 'one'})['state'] == 'unknown'
    with pytest.raises(OperationConflict):
        restarted.admit('op', {'request': 'changed'})
    restarted.observe('op', b'{"phase":"Running"}', 'allowed')
    replay = ResourceOperationStore(path).admit('op', {'request': 'one'})
    assert replay['state'] == 'observed' and replay['observation'] == b'{"phase":"Running"}'
    with pytest.raises(OperationConflict):
        restarted.observe('op', b'different', 'allowed')
    assert restarted.admit('op', {'request': 'one'}) == replay
