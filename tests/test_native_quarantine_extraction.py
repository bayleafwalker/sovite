"""Local Work quarantine fixtures; no live owner or VM is qualified here."""
import asyncio
import time
from pathlib import Path

import pytest

from sovite.adapters import RuntimeFailure
from sovite.repository_native_git import NativeGit
from sovite.repository_native_vm import _prepare_quarantine
from sovite.repository_sessions import _git, RepositorySessionError
from test_repository_native_vm import (callback, candidate_bundle, commit, repository,
                                       shared_workstation_uid_fixture)


def test_quarantine_prepares_only_and_preserves_worktree(repository, tmp_path):
    worktree, base = repository
    bound = callback(tmp_path, base)
    tip, bundle = candidate_bundle(tmp_path, worktree, base)
    calls = []
    git = NativeGit(time.time() + 20, lambda: calls.append('check'), expected_tools=bound.driver.git_facts)
    async def trace(*args):
        calls.append(args)
        return await git(*args)
    result = asyncio.run(_prepare_quarantine(git=trace, root=bound.driver.state_root, worktree=worktree,
                                             base=base, seed_tip=base, header={'tip_sha': tip},
                                             bundle=bundle, areas=['src']))
    assert result.tip == tip and result.quarantine == bound.driver.state_root / 'quarantine.git'
    assert _git('-C', str(worktree), 'rev-parse', 'HEAD').decode().strip() == base
    assert not (worktree / 'src/result.txt').exists()
    commands = [entry for entry in calls if isinstance(entry, tuple)]
    assert commands[0][:2] == ('init', '--bare')
    assert [entry[1] for entry in commands if entry and entry[0].startswith('--git-dir=')][:4] == ['fetch', 'bundle', 'fetch', 'merge-base']
    assert any(entry[:2] == ('bundle', 'list-heads') for entry in commands)
    assert calls.count('check') >= len(commands) * 2


def test_quarantine_refuses_mismatched_head_before_materialization(repository, tmp_path):
    worktree, base = repository
    bound = callback(tmp_path, base)
    _, bundle = candidate_bundle(tmp_path, worktree, base)
    with pytest.raises(RuntimeFailure, match='native_export_scope_mismatch'):
        asyncio.run(bound._import(bound.driver.state_root, worktree, base, base,
                                  {'tip_sha': 'a' * 40}, bundle, ['src'], lambda: True))
    assert _git('-C', str(worktree), 'rev-parse', 'HEAD').decode().strip() == base
    assert not (worktree / 'src/result.txt').exists()


def test_quarantine_refuses_candidate_not_descended_from_seed(repository, tmp_path):
    worktree, base = repository
    bound = callback(tmp_path, base)
    tip, bundle = candidate_bundle(tmp_path, worktree, base)
    seed = commit(worktree, 'src/seed.txt', 'other branch')
    with pytest.raises(RuntimeFailure, match='native_git_refused'):
        asyncio.run(bound._import(bound.driver.state_root, worktree, base, seed,
                                  {'tip_sha': tip}, bundle, ['src'], lambda: True))
    assert _git('-C', str(worktree), 'rev-parse', 'HEAD').decode().strip() == seed
    assert not (worktree / 'src/result.txt').exists()


def test_quarantine_refuses_existing_bundle_file(repository, tmp_path):
    worktree, base = repository
    bound = callback(tmp_path, base)
    tip, bundle = candidate_bundle(tmp_path, worktree, base)
    (bound.driver.state_root / 'candidate.bundle').write_bytes(b'existing')
    with pytest.raises(RepositorySessionError, match="immutable candidate receipt"):
        asyncio.run(bound._import(bound.driver.state_root, worktree, base, base,
                                  {'tip_sha': tip}, bundle, ['src'], lambda: True))
    assert (bound.driver.state_root / 'candidate.bundle').read_bytes() == b'existing'
    assert _git('-C', str(worktree), 'rev-parse', 'HEAD').decode().strip() == base


def test_quarantine_refuses_merge_history(repository, tmp_path):
    worktree, base = repository
    bound = callback(tmp_path, base)
    guest = tmp_path / 'merge-guest'
    _git('clone', '--no-hardlinks', str(worktree), str(guest))
    _git('-C', str(guest), 'checkout', '-b', 'side', base)
    commit(guest, 'src/side.txt', 'side')
    _git('-C', str(guest), 'checkout', '-b', 'main-candidate', base)
    commit(guest, 'src/main.txt', 'main')
    _git('-C', str(guest), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
         'merge', '--no-ff', '-m', 'merge', 'side')
    tip = _git('-C', str(guest), 'rev-parse', 'HEAD').decode().strip()
    _git('-C', str(guest), 'update-ref', 'refs/heads/candidate', tip)
    bundle_path = tmp_path / 'merge.bundle'
    _git('-C', str(guest), 'bundle', 'create', str(bundle_path), 'refs/heads/candidate', '^' + base)
    with pytest.raises(RuntimeFailure, match='native_candidate_history_refused'):
        asyncio.run(bound._import(bound.driver.state_root, worktree, base, base,
                                  {'tip_sha': tip}, bundle_path.read_bytes(), ['src'], lambda: True))
    assert _git('-C', str(worktree), 'rev-parse', 'HEAD').decode().strip() == base
    assert not (worktree / 'src/main.txt').exists()


def test_authority_loss_after_quarantine_prevents_materialization(repository, tmp_path, monkeypatch):
    import sovite.repository_native_vm as vm
    worktree, base = repository
    bound = callback(tmp_path, base)
    tip, bundle = candidate_bundle(tmp_path, worktree, base)
    current = True
    original = vm._prepare_quarantine
    real_git = vm.NativeGit
    calls_after_revoke = []
    class TracedGit:
        def __init__(self, *args, **kwargs):
            self.inner = real_git(*args, **kwargs)
        async def __call__(self, *args):
            if not current:
                calls_after_revoke.append(args)
            return await self.inner(*args)
    monkeypatch.setattr(vm, 'NativeGit', TracedGit)
    async def prepare(**kwargs):
        nonlocal current
        result = await original(**kwargs)
        current = False
        return result
    def authorize():
        if not current:
            raise RuntimeFailure('authorization_revoked')
    monkeypatch.setattr(vm, '_prepare_quarantine', prepare)
    with pytest.raises(RuntimeFailure, match='authorization_revoked'):
        asyncio.run(bound._import(bound.driver.state_root, worktree, base, base,
                                  {'tip_sha': tip}, bundle, ['src'], authorize))
    assert _git('-C', str(worktree), 'rev-parse', 'HEAD').decode().strip() == base
    assert not (worktree / 'src/result.txt').exists()
    assert not calls_after_revoke


def test_driver_fingerprint_covers_extracted_helper(monkeypatch):
    import sovite.repository_native_vm as vm
    actual = vm._source_digest()
    original = Path.read_bytes
    def changed(path):
        value = original(path)
        return value + b'\n# synthetic source change' if path.name == 'repository_native_vm.py' else value
    monkeypatch.setattr(Path, 'read_bytes', changed)
    assert vm._source_digest() != actual


@pytest.mark.parametrize('filename,old,new,test_name', [
    ('repository_native_vm.py', "heads == tip+' refs/heads/candidate'", 'True',
     'test_quarantine_refuses_mismatched_head_before_materialization'),
    ('repository_native_vm.py', "await git('--git-dir='+str(quarantine),'merge-base','--is-ancestor',seed_tip,tip)", 'pass',
     'test_quarantine_refuses_candidate_not_descended_from_seed'),
    ('repository_native_vm.py', 'all(len(line.split()) == 2 for line in lines)', 'True',
     'test_quarantine_refuses_merge_history'),
    ('repository_native_vm.py', 'RepositorySessionManager._within_area(name,tuple(areas))', 'True',
     'test_untrusted_candidate_cannot_import_outside_area_or_symlink'),
    ('repository_native_vm.py', "entry.split(b' ',1)[0] in {b'100644',b'100755'}", 'True',
     'test_untrusted_candidate_cannot_import_outside_area_or_symlink'),
    ('repository_native_vm.py', 'expanded_bytes <= EXPANDED_LIMIT', 'True',
     'test_compressed_bundle_expansion_refuses_before_materializing'),
    ('repository_native_git.py', 'expected_tools is None or tool_facts() == expected_tools', 'True',
     'test_parser_refuses_a_different_qualified_tool_digest'),
    ('repository_native_vm.py', 'quarantine, tip = candidate.quarantine, candidate.tip\n        authorization_check()',
     'quarantine, tip = candidate.quarantine, candidate.tip',
     'test_authority_loss_after_quarantine_prevents_materialization'),
    ('repository_native_vm.py', "placement = ['--git-dir='+str(quarantine),'--work-tree='+str(worktree),'-c','core.bare=false']",
     "placement = ['-C',str(worktree)]", 'test_candidate_attributes_cannot_invoke_host_filter_and_no_environment_inheritance'),
])
def test_quarantine_guard_mutations_are_detected(tmp_path, filename, old, new, test_name):
    """Each local mutant must fail its named adversarial regression test."""
    import os
    import shutil
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / 'sovite', tmp_path / 'sovite', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (tmp_path / 'tests').mkdir()
    for name in ('test_native_quarantine_extraction.py', 'test_repository_native_vm.py'):
        shutil.copyfile(root / 'tests' / name, tmp_path / 'tests' / name)
    target = tmp_path / 'sovite' / filename
    source = target.read_text()
    assert source.count(old) == 1
    target.write_text(source.replace(old, new, 1))
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-x', 'tests', '-k', test_name],
        cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(tmp_path), 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 1 and 'failed' in result.stdout, result.stdout + result.stderr
