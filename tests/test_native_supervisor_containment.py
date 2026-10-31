"""Actual native OS controls in an explicitly installed immutable test fixture.

Each removed guard must fail the same portable synthetic privilege probe. The
fixture never carries owner credentials or advertises runtime qualification.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('removed',['none','mount','proc','network'])
def test_actual_native_parent_boundary_and_guard_removal(removed):
    executable=os.environ.get('SOVITE_CODEX_COMPAT_EXECUTABLE')
    profile=os.environ.get('SOVITE_NATIVE_TEST_PROFILE')
    child=os.environ.get('SOVITE_NATIVE_TEST_CHILD')
    python=os.environ.get('SOVITE_NATIVE_TEST_PYTHON')
    if not all((executable,profile,child,python)):
        pytest.skip('Requires the declared immutable disposable native supervisor fixture')
    root=Path(__file__).parents[1]
    probe=root/'tests/probes/native_supervisor_containment.py'
    script='''import asyncio,os,runpy,sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1]).parent))
original=asyncio.create_subprocess_exec
async def start(*args,**kwargs):
    args=list(args)
    if "--unshare-all" in args:
        index=args.index("-I")-1
        removed=os.environ["SOVITE_REMOVED_GUARD"]
        home=Path(args[args.index("--chdir")+1])
        if removed=="network":args[index:index]=["--share-net"]
        elif removed=="proc":args[index:index]=["--ro-bind","/proc","/proc"]
        elif removed=="mount":args[index:index]=["--bind",str(home.parent),str(home.parent)]
    return await original(*args,**kwargs)
asyncio.create_subprocess_exec=start
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0],run_name="__main__")
'''
    env={'PATH':'/run/current-system/sw/bin','LANG':'C.UTF-8',
         'SOVITE_SYNTHETIC_API_ENV':'public-synthetic-parent-db-s3-marker',
         'SOVITE_REMOVED_GUARD':removed}
    result=subprocess.run([python,'-c',script,str(probe),'--runtime-module',
        str(root/'sovite/repository_native_runtime.py'),'--codex',executable,
        '--fixture-profile',profile,'--fixture-child',child],env=env,capture_output=True,text=True,timeout=35)
    import json
    assert result.stdout,result.stderr
    proof=json.loads(result.stdout)
    assert proof['qualified']is False and proof['paid_model_calls']==0
    if removed=='none':
        assert result.returncode==0,result.stderr
        assert not any(proof['parent_marker_visibility'].values())
        assert proof['direct_network_effects']==0 and proof['detached_child_stopped']
    else:
        assert result.returncode!=0,'Removed OS guard unexpectedly passed'
        if removed=='network':assert proof['direct_network_effects']==1
        elif removed=='mount':assert proof['parent_marker_visibility']['parent_config']
        else:assert proof['parent_marker_visibility']['parent_proc_status']
