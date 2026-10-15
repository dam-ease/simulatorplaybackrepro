#!/usr/bin/env python3
"""Bound Apple decoding and actual host audio output before simulator/app work."""
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def collect_host_output_failure(process_id, build):
    try:
        subprocess.run(['/usr/bin/sample', str(process_id), '1', '1', '-file',
                        str(build / 'host-audio-output-stack.txt')],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4, check=False)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with (build / 'host-audio-output-system.log').open('w') as output:
            subprocess.run(['/usr/bin/log', 'show', '--last', '1m', '--style', 'compact',
                            '--predicate', 'process == "afplay" OR process == "coreaudiod"'],
                           stdout=output, stderr=subprocess.STDOUT, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def run_host_output(command, log, build, timeout=8):
    # Inventory/decoding do not initialize the output device. Opening actual
    # fixture PCM here also primes the host before Simulator creates its route.
    with log.open('w') as output:
        output.write('COMMAND ' + ' '.join(command) + '\n')
        output.flush()
        started = time.monotonic()
        process = subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
        try:
            try:
                status = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                output.write(f'TIMEOUT after {timeout:.1f} seconds\n')
                output.flush()
                # Sample the owned process while it is still stalled, not after
                # subprocess.run has already killed it. Never restart a daemon.
                collect_host_output_failure(process.pid, build)
                raise
            elapsed = time.monotonic() - started
            if status:
                raise subprocess.CalledProcessError(status, command)
            if elapsed < .25:
                raise ValueError('Host output exited before the fixture audio could advance')
            return elapsed
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)


def read_progress(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get('platform') == 'macOS-host' else {}


def main():
    if platform.system() != 'Darwin':
        raise SystemExit('Apple fixture decoding requires the selected Mac host.')
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    result = {'passed': False, 'platform': 'macOS-host', 'ios_readiness_verified': False,
              'playback_verified': False, 'physical_sound_verified': False,
              'host_output_io_passed': False, 'stage': 'compile-media-probe'}
    status = 1
    progress = build / 'media-fixture-progress.json'
    preview = build / 'media-fixture-audio-preview.caf'
    # Both are produced by this invocation; an earlier preview/report is unusable.
    progress.unlink(missing_ok=True)
    preview.unlink(missing_ok=True)
    result_path = build / 'media-fixture-preflight.json'

    def save():
        result_path.write_text(json.dumps(result, indent=2) + '\n')

    try:
        save()
        executable = build / 'media-fixture-preflight'
        with (build / 'media-fixture-compile.log').open('w') as output:
            subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-swift-version', '5',
                            '-target', platform.machine() + '-apple-macosx13.0',
                            str(ROOT / 'Tools/media_fixture_preflight.swift'), '-o', str(executable)],
                           cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, timeout=40, check=True)
        result['stage'] = 'decode-media-fixture'
        fixture = ROOT / 'Stall/Resources/fullscreen-check.mp4'
        result['fixture_sha256'] = hashlib.sha256(fixture.read_bytes()).hexdigest()
        save()
        with (build / 'media-fixture-decode.log').open('w') as output:
            completed = subprocess.run([str(executable), str(fixture), str(preview), str(progress)], cwd=ROOT,
                                       stdout=output, stderr=subprocess.STDOUT, timeout=30)
        text = (build / 'media-fixture-decode.log').read_text()
        # System diagnostics may surround the probe's JSON; locate its object.
        decoded = {}
        decoder = json.JSONDecoder()
        for index, character in enumerate(text):
            if character != '{':
                continue
            try:
                value, _ = decoder.raw_decode(text, index)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get('platform') == 'macOS-host' and isinstance(value.get('passed'), bool):
                decoded = value
                break
        result.update(decoded, passed=False)
        if completed.returncode != 0 or decoded.get('passed') is not True:
            raise ValueError('Apple fixture decoder did not pass; see media-fixture-decode.log')
        if (decoded.get('audio_preview_frames') != 24000 or decoded.get('audio_preview_nonzero') is not True
                or not preview.is_file() or preview.stat().st_size <= 0):
            raise ValueError('Fresh half-second fixture PCM preview is missing or silent')
        result['stage'] = 'start-host-audio-output'
        save()
        result['host_output_elapsed_seconds'] = round(run_host_output(
            ['/usr/bin/afplay', '-t', '0.5', str(preview)], build / 'host-audio-output.log', build), 3)
        result.update(host_output_io_passed=True, passed=True, stage='completed')
        status = 0
    except Exception as error:
        if result['stage'] == 'decode-media-fixture':
            result.update(read_progress(progress))
        result.update(passed=False, error=str(error), error_type=type(error).__name__)
    finally:
        save()
        print(json.dumps(result, indent=2))
    return status


if __name__ == '__main__':
    sys.exit(main())
