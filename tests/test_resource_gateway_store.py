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


def test_private_store_refuses_public_directory_file_and_symlink(tmp_path):
    import os
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    store = ResourceOperationStore(private / 'state')
    old_umask = os.umask(0o022)
    try:
        store.install()
    finally:
        os.umask(old_umask)
    assert (private / 'state').stat().st_mode & 0o777 == 0o600
    (private / 'state').chmod(0o644)
    with pytest.raises(RuntimeError, match='file_not_private'):
        store.admit('op', {})
    (private / 'state').chmod(0o600)
    private.chmod(0o755)
    with pytest.raises(RuntimeError, match='directory_not_private'):
        store.admit('op', {})
    private.chmod(0o700)
    (private / 'state-wal').symlink_to(private / 'state')
    with pytest.raises(RuntimeError, match='sidecar_not_private'):
        store.admit('op', {})
    (private / 'state-wal').unlink()
    (private / 'state-shm').write_text('synthetic')
    (private / 'state-shm').chmod(0o644)
    with pytest.raises(RuntimeError, match='sidecar_not_private'):
        store.admit('op', {})
    (private / 'state-shm').unlink()
    (private / 'link').symlink_to(private / 'state')
    with pytest.raises(RuntimeError, match='file_not_private'):
        ResourceOperationStore(private / 'link').install()
