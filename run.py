#!/usr/bin/env python3
"""Standalone entry point for the unchanged probes; Python standard library only."""
import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent


def check_package():
    manifest_path = ROOT / 'PACKAGE.json'
    manifest = json.loads(manifest_path.read_text())
    for entry in manifest['files']:
        name = PurePosixPath(entry['path'])
        if name.is_absolute() or '..' in name.parts:
            raise ValueError('Invalid package path')
        path = ROOT / str(name)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Package file changed or missing: ' + str(name))
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--simulator', choices=['fresh', 'preinstalled'], default='fresh')
    parser.add_argument('--expect-xcode', default='26.6')
    parser.add_argument('--simulator-os', default='26.5', help='Installed runtime identifier version, e.g. 26.4 (even when its patch version is 26.4.1).')
    parser.add_argument('--check-only', action='store_true', help='Verify package files without using Xcode or starting a simulator.')
    args = parser.parse_args(argv)
    for value in (args.expect_xcode, args.simulator_os):
        if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', value):
            parser.error('Versions must use major.minor or major.minor.patch format')
    package_digest = check_package()
    if args.check_only:
        print(json.dumps({'package_integrity_passed': True, 'package_manifest_sha256': package_digest,
                          'native_execution_performed': False}, indent=2))
        return 0
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise ValueError('Use an Apple silicon Mac with full Xcode and the requested iOS simulator runtime.')

    build = ROOT / 'build'
    if build.exists():
        raise ValueError('Preserve or rename the existing build directory before another run.')
    build.mkdir()
    started = time.monotonic()
    env = dict(os.environ, STALL_SIMULATOR_OS=args.simulator_os, STALL_SIMULATOR_SOURCE=args.simulator,
               STALL_PREFLIGHT_ONLY='true', STALL_VALIDATION_WORKFLOW='playback-runtime-preflight')
    outcome = {'passed': False, 'stage': 'check-toolchain', 'simulator_source': args.simulator,
               'expected_xcode': args.expect_xcode, 'requested_simulator_runtime': args.simulator_os, 'package_manifest_sha256': package_digest}

    def save():
        (build / 'support-run.json').write_text(json.dumps(outcome, indent=2) + '\n')

    def run(command, timeout, capture=False):
        remaining = 295 - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, 0)
        return subprocess.run(command, cwd=ROOT, env=env, text=True, check=True,
                              capture_output=capture, timeout=min(timeout, remaining))

    try:
        save()
        xcode = run(['xcodebuild', '-version'], 25, capture=True).stdout.strip()
        (build / 'xcode-version.txt').write_text(xcode + '\n')
        version = re.search(r'^Xcode (\d+\.\d+(?:\.\d+)?)(?:\D|$)', xcode, re.M)
        if not version or not (version.group(1) == args.expect_xcode or version.group(1).startswith(args.expect_xcode + '.')):
            raise ValueError('Selected Xcode does not match --expect-xcode: ' + xcode)
        macos = run(['sw_vers', '-productVersion'], 10, capture=True).stdout.strip()
        macos_build = run(['sw_vers', '-buildVersion'], 10, capture=True).stdout.strip()
        identity = {
            'source_commit': 'standalone-support-package',
            'source_digest': package_digest,
            'source_digest_scope': 'SHA256 of PACKAGE.json, after verifying every listed file',
            'original_build_source_commit': '3f4e2f8f55b97f6878b340d62c8247f613241141',
            'original_repository_source_digest': 'b45f98007e4e59210ba1f0dfba37493bacdb95c763a829282df7e6edd4a032ed',
            'workflow_id': 'standalone-support-reproduction',
            'codemagic_build_id': os.environ.get('CM_BUILD_ID'),
            'host_macos_version': macos, 'host_macos_build': macos_build,
            'xcode': xcode, 'architecture': platform.machine(),
            'simulator_source_requested': args.simulator, 'simulator_runtime': args.simulator_os,
            'preflight_started_monotonic': started,
            'release_hold': True, 'signed': False, 'apple_upload_performed': False,
        }
        (build / 'MAC-SOURCE-IDENTITY.json').write_text(json.dumps(identity, indent=2) + '\n')
        outcome['stage'] = 'host-media-and-output'
        save()
        run([sys.executable, 'Tools/run_media_fixture_preflight.py'], 100)
        outcome['stage'] = 'select-simulator-template'
        save()
        inventory = run(['xcrun', 'simctl', 'list', 'devices', 'available'], 25, capture=True).stdout
        (build / 'simulators.txt').write_text(inventory)
        selected = run([sys.executable, 'Tools/select_simulator.py', '--os', args.simulator_os], 25, capture=True).stdout
        (build / 'native-simulator.txt').write_text(selected.strip() + '\n')
        outcome['stage'] = 'simulator-playback'
        save()
        run([sys.executable, 'Tools/run_simulator_playback_preflight.py'], 290)
        outcome.update(passed=True, stage='completed')
    except Exception as error:
        outcome.update(error=str(error), error_type=type(error).__name__)
    finally:
        outcome['elapsed_seconds'] = round(time.monotonic() - started, 3)
        save()
        print(json.dumps(outcome, indent=2))
    return 0 if outcome['passed'] else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError) as error:
        print('Support reproduction: ' + str(error), file=sys.stderr)
        sys.exit(1)
