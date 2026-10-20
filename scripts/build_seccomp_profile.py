#!/usr/bin/env python3
"""Build deploy/seccomp/sovite-codex-sandbox.json from Moby's default seccomp profile.

The native harness pod runs with no capabilities, as non-root, on amd64. This
resolves Moby's capability- and architecture-conditional rules for exactly that
process, then adds the namespace syscalls Codex's bubblewrap sandbox needs.
Use it only together with a user-namespaced pod (`hostUsers: false`), where the
kernel confines those syscalls to namespaces the pod's user namespace owns.

Input used for the checked-in profile: moby/moby v28.3.3
profiles/seccomp/default.json, sha256
01536f1d1df938ae611eba20d6349e0de7a99b6ecdee1549427a0b01b8301e28.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

NAMESPACE_SYSCALLS = ["unshare", "clone", "setns", "mount", "umount2", "pivot_root"]


def build(moby: dict) -> dict:
    rules = []
    for rule in moby["syscalls"]:
        includes, excludes = rule.get("includes", {}), rule.get("excludes", {})
        if set(includes) - {"caps", "arches", "minKernel"}:
            raise ValueError(f"unhandled include condition: {includes}")
        if includes.get("caps"):
            continue  # the pod drops every capability
        if includes.get("arches") and "amd64" not in includes["arches"]:
            continue
        if excludes.get("arches") and "amd64" in excludes["arches"]:
            continue
        rules.append({key: value for key, value in rule.items() if key not in ("includes", "excludes", "comment")})
    rules.append({"names": NAMESPACE_SYSCALLS, "action": "SCMP_ACT_ALLOW"})
    return {"defaultAction": moby["defaultAction"], "defaultErrnoRet": moby.get("defaultErrnoRet", 1),
            "archMap": moby["archMap"], "syscalls": rules}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("moby_default", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "deploy/seccomp/sovite-codex-sandbox.json")
    args = parser.parse_args()
    args.output.write_text(json.dumps(build(json.loads(args.moby_default.read_text())), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
