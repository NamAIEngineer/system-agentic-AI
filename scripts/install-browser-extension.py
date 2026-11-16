#!/usr/bin/env python3
"""Opt-in pinned extension checkout + reviewed implementation patch, never global."""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / 'integrations/browser-extension'
DESTINATION = ROOT / '.system-agentic-local/browser-extension-implementation'


def run(*argv: str, cwd: Path | None = None) -> None:
    subprocess.run(argv, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--allow-network', action='store_true')
    parser.add_argument('--allow-dependency-execution', action='store_true')
    args = parser.parse_args()
    manifest = json.loads((BUNDLE / 'manifest.json').read_text())
    patch = BUNDLE / 'implementation.patch'
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest['patch_sha256']:
        raise SystemExit('Patch integrity mismatch')
    plan = {'destination': str(DESTINATION), 'upstream': manifest['upstream'],
            'revision': manifest['revision'], 'license': manifest['license'],
            'steps': ['clone pinned source', 'check and apply implementation patch',
                      'npm ci --ignore-scripts', 'node scripts/build.mjs'],
            'starts_services': False, 'changes_personal_browser': False,
            'installed': (DESTINATION / '.system-agentic-extension.json').is_file()}
    print(json.dumps(plan, indent=2))
    if not args.install:
        return 0
    if not args.allow_network or not args.allow_dependency_execution:
        raise SystemExit('Install requires --allow-network --allow-dependency-execution after reviewing the plan')
    if DESTINATION.exists():
        raise SystemExit('Destination already exists; preserve it. Inspect or choose a fresh checkout; never reset automatically.')
    for executable in ('git', 'node', 'npm'):
        if not shutil.which(executable):
            raise SystemExit(f'{executable} is required')
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    # A failed installation remains for inspection; never delete/reset user files.
    run('git', 'clone', '--no-checkout', manifest['upstream'], str(DESTINATION))
    run('git', 'checkout', '--detach', manifest['revision'], cwd=DESTINATION)
    run('git', 'apply', '--check', str(patch), cwd=DESTINATION)
    run('git', 'apply', str(patch), cwd=DESTINATION)
    run('npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund', cwd=DESTINATION)
    run('node', 'scripts/build.mjs', cwd=DESTINATION)
    (DESTINATION / '.system-agentic-extension.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print('INSTALLED; browser pairing and live verification are separate explicit steps')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
