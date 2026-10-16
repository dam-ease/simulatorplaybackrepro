#!/usr/bin/env python3
"""Real UIKit/AVPlayer preflight before app builds or pods; no CI dispatch."""
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = 'com.danielchampion.adblockcastbrowser.playback-probe'


def validate_probe(result, expected_run_id=None):
    return (result.get('schema') == 1 and result.get('finished') is True and result.get('passed') is True
            and not result.get('error')
            and (expected_run_id is None or result.get('validation_run_id') == expected_run_id)
            and result.get('paused_ready') is True and result.get('audio_track_count') == 1
            and result.get('item_status') == 1 and result.get('time_control_status') == 2
            and isinstance(result.get('elapsed_seconds'), (int, float)) and result['elapsed_seconds'] >= .25
            and isinstance(result.get('output_channels'), int) and result['output_channels'] > 0
            and isinstance(result.get('sample_rate'), (int, float)) and result['sample_rate'] > 0
            and isinstance(result.get('route'), list) and bool(result['route']))


def read_probe(report, run_id):
    if report is None or run_id is None:
        return {}
    try:
        value = json.loads(report.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get('validation_run_id') == run_id else {}


def retain_probe_report(report, run_id, result, build):
    # simctl can time out waiting for launch after the app has written a report.
    # Preserve this run's observations without overriding a command failure.
    probe = read_probe(report, run_id)
    if report is None:
        return
    result['probe'] = probe
    result['probe_report_retained'] = False
    if probe:
        try:
            (build / 'playback-probe.json').write_text(json.dumps(probe, indent=2) + '\n')
            result['probe_report_retained'] = True
        except OSError as error:
            result['probe_report_copy_error'] = str(error)


def selected_device(devices, selected_id, runtime_version, device_types=()):
    for runtime, entries in devices.items():
        version = runtime.partition('iOS-')[2].replace('-', '.')
        for device in entries:
            if device['udid'] == selected_id:
                if version != runtime_version or not device.get('isAvailable'):
                    raise ValueError('Selected simulator does not match the required available runtime')
                device_type = device.get('deviceTypeIdentifier')
                if not device_type:
                    matches = [entry['identifier'] for entry in device_types if entry['name'] == device['name']]
                    device_type = matches[0] if len(matches) == 1 else None
                if not device_type:
                    raise ValueError('Simulator inventory lacks a device type identifier')
                return runtime, device_type
    raise ValueError('Selected simulator is missing')


def run_command(command, log, timeout=25, check=True):
    # Write the child's output directly to the artifact. PIPE/communicate loses
    # boot progress when a timeout interrupts subprocess.run.
    with log.open('a+') as output:
        output.write('COMMAND ' + ' '.join(command) + '\n')
        output.flush()
        start = output.tell()
        try:
            completed = subprocess.run(command, cwd=ROOT, text=True, stdout=output,
                                       stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            output.write(f'\nTIMEOUT after {timeout:.1f} seconds\n')
            output.flush()
            raise
        output.flush()
        output.seek(start)
        command_output = output.read()
    if check and completed.returncode:
        raise subprocess.CalledProcessError(completed.returncode, command, output=command_output)
    return command_output.strip()


def run_before_deadline(command, log, deadline, timeout=25, check=True):
    remaining = deadline - time.monotonic()
    if remaining <= 0 or timeout <= 0:
        raise subprocess.TimeoutExpired(command, 0)
    return run_command(command, log, timeout=min(timeout, remaining), check=check)


def prepare_probe(build, run):
    sdk = run(['xcrun', '--sdk', 'iphonesimulator', '--show-sdk-path'])
    app = build / 'SimulatorPlaybackProbe.app'
    app.mkdir(exist_ok=True)
    executable = app / 'SimulatorPlaybackProbe'
    run(['xcrun', 'swiftc', '-parse-as-library', '-swift-version', '5', '-target',
         platform.machine() + '-apple-ios18.0-simulator', '-sdk', sdk,
         str(ROOT / 'Tools/simulator_playback_probe.swift'), '-o', str(executable)], timeout=60)
    info = {'CFBundleIdentifier': BUNDLE, 'CFBundleExecutable': executable.name, 'CFBundleName': 'Playback Probe',
            'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1', 'CFBundleShortVersionString': '1.0',
            'CFBundleInfoDictionaryVersion': '6.0', 'CFBundleSupportedPlatforms': ['iPhoneSimulator'],
            'MinimumOSVersion': '18.0', 'UIDeviceFamily': [1, 2], 'UILaunchScreen': {},
            'UISupportedInterfaceOrientations': ['UIInterfaceOrientationPortrait',
                'UIInterfaceOrientationLandscapeLeft', 'UIInterfaceOrientationLandscapeRight'],
            'UISupportedInterfaceOrientations~ipad': ['UIInterfaceOrientationPortrait',
                'UIInterfaceOrientationPortraitUpsideDown', 'UIInterfaceOrientationLandscapeLeft',
                'UIInterfaceOrientationLandscapeRight'],
            'UIBackgroundModes': ['audio'], 'AVInitialRouteSharingPolicy': 'LongFormVideo'}
    (app / 'Info.plist').write_bytes(plistlib.dumps(info))
    (build / 'playback-probe-info.json').write_text(json.dumps(info, indent=2) + '\n')
    fixture = ROOT / 'Stall/Resources/fullscreen-check.mp4'
    shutil.copyfile(fixture, app / fixture.name)
    return app, hashlib.sha256(fixture.read_bytes()).hexdigest()


def prepare_report_path(container):
    if not container.is_absolute() or not container.is_dir():
        raise ValueError('Simulator did not return an existing absolute app data directory')
    report = container / 'Documents/playback-probe.json'
    report.parent.mkdir(parents=True, exist_ok=True)
    # Only test storage in this probe's data container. Do not fabricate a
    # player report; UIKit must still supply the real current-run evidence.
    with tempfile.TemporaryFile(dir=report.parent) as check:
        check.write(b'stall-probe-report-storage-check\n')
        check.flush()
    return report


def collect_failure(simulator, process_id, build):
    if process_id:
        try:
            run_command(['/usr/bin/sample', str(process_id), '1', '1', '-file', str(build / 'playback-probe-stack.txt')],
                        build / 'playback-probe-diagnostics.log', timeout=8, check=False)
        except Exception:
            pass
    try:
        with (build / 'playback-preflight-media.log').open('w') as output:
            subprocess.run(['xcrun', 'simctl', 'spawn', simulator, 'log', 'show', '--last', '2m', '--style', 'compact',
                            '--predicate', 'subsystem CONTAINS[c] "coremedia" OR subsystem CONTAINS[c] "audio" '
                            'OR subsystem CONTAINS[c] "frontboard" OR process == "SpringBoard" '
                            'OR process == "SimulatorPlaybackProbe"'],
                           stdout=output, stderr=subprocess.STDOUT, timeout=15, check=False)
    except Exception:
        pass


def main():
    if platform.system() != 'Darwin':
        raise SystemExit('Simulator playback preflight requires the selected Mac.')
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    log = build / 'simulator-playback-setup.log'
    identity = json.loads((build / 'MAC-SOURCE-IDENTITY.json').read_text())
    plan = json.loads((ROOT / 'Release/codemagic-focused-recheck.json').read_text())
    plan['simulator_runtime'] = os.environ.get('STALL_SIMULATOR_OS', plan['simulator_runtime'])
    result = {'passed': False, 'source_commit': identity['source_commit'], 'source_digest': identity['source_digest'],
              'runtime': plan['simulator_runtime'], 'physical_sound_verified': False,
              'app_tests_passed': False, 'release_hold': True, 'signed': False, 'apple_upload_performed': False}
    simulator, process_id = None, None
    report, run_id = None, None
    status = 1
    started = time.monotonic()

    def stage(name):
        result['stage'] = name
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (build / 'simulator-playback-preflight.json').write_text(json.dumps(result, indent=2) + '\n')

    try:
        # Reserve time for diagnostics/cleanup before Codemagic's five-minute
        # cap. The timestamp is recorded before the host media check, so a slow
        # host compile cannot silently consume the simulator's entire budget.
        deadline = min(started + 210, float(identity.get('preflight_started_monotonic', started)) + 240)

        def run(command, timeout=25, check=True):
            return run_before_deadline(command, log, deadline, timeout, check)

        stage('validate-simulator-mode')
        simulator_mode = os.environ.get('STALL_SIMULATOR_SOURCE', 'preinstalled')
        if simulator_mode not in ['preinstalled', 'fresh']:
            raise ValueError('Unknown simulator source: ' + simulator_mode)
        if simulator_mode == 'fresh' and (os.environ.get('STALL_PREFLIGHT_ONLY') != 'true'
                or os.environ.get('STALL_VALIDATION_WORKFLOW') != 'playback-runtime-preflight'):
            raise ValueError('Fresh simulator comparison is limited to the playback-only workflow')
        result['requested_simulator_source'] = simulator_mode
        stage('require-host-audio-and-decoding')
        media = json.loads((build / 'media-fixture-preflight.json').read_text())
        if media.get('passed') is not True or media.get('host_audio', {}).get('host_output_valid') is not True:
            raise ValueError('Mac decoding and a live host output must pass first')
        if media.get('host_output_io_passed') is not True:
            raise ValueError('Actual Mac audio output must pass before compiling or booting the simulator')
        result['host_output_io_passed'] = True
        result['host_audio'] = media['host_audio']
        # Catch SDK/compiler errors before spending a cold simulator boot.
        stage('compile-small-playback-probe')
        app, result['fixture_sha256'] = prepare_probe(build, run)
        result['probe_compiled'] = True
        selected_id = (build / 'native-simulator.txt').read_text().strip()
        stage('select-required-runtime')
        devices = json.loads(run(['xcrun', 'simctl', 'list', 'devices', 'available', '--json']))['devices']
        types = json.loads(run(['xcrun', 'simctl', 'list', 'devicetypes', '--json']))['devicetypes']
        runtime, device_type = selected_device(devices, selected_id, plan['simulator_runtime'], types)
        selected = next(device for device in devices[runtime] if device['udid'] == selected_id)
        initial_state = selected.get('state')
        if initial_state not in ['Shutdown', 'Booted']:
            raise ValueError('Selected preinstalled simulator has an unexpected state: ' + str(initial_state))
        if simulator_mode == 'fresh':
            # Support requested one empty-profile comparison. Use the selected
            # device only to identify the same model/runtime, never as fallback.
            stage('create-fresh-simulator')
            result['template_preinstalled_simulator_udid'] = selected_id
            result['simulator_name'] = 'StallPlaybackProbe-' + uuid.uuid4().hex[:12]
            created_id = run(['xcrun', 'simctl', 'create', result['simulator_name'], device_type, runtime])
            if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', created_id):
                raise ValueError('Simulator creation did not return one valid device UUID')
            existing_ids = {device['udid'].lower() for entries in devices.values() for device in entries}
            if created_id.lower() in existing_ids:
                raise ValueError('Simulator creation returned a preexisting device; refusing reuse or cleanup')
            simulator = created_id
            result.update(simulator_udid=simulator, created_by_this_job=True,
                          simulator_source='fresh-created', preinstalled_simulator_verified=False)
            stage('verify-fresh-simulator')
            created_devices = json.loads(run(['xcrun', 'simctl', 'list', 'devices', 'available', '--json']))['devices']
            created_runtime, created_type = selected_device(created_devices, simulator, plan['simulator_runtime'], types)
            if (created_runtime, created_type) != (runtime, device_type):
                raise ValueError('Created simulator changed the selected model or runtime')
            initial_state = next(device['state'] for device in created_devices[runtime] if device['udid'] == simulator)
            if initial_state != 'Shutdown':
                raise ValueError('Freshly created simulator must start in Shutdown state')
            result['fresh_simulator_verified'] = True
            (build / 'native-simulator.txt').write_text(simulator + '\n')
        else:
            stage('reuse-preinstalled-simulator')
            simulator = selected_id
            result.update(simulator_udid=simulator, created_by_this_job=False,
                          simulator_source='preinstalled-selected', preinstalled_simulator_verified=True)
        result['initial_simulator_state'] = initial_state
        result['runtime_identifier'] = runtime
        stage('prepare-selected-simulator-gui')
        developer = Path(run(['xcrun', '--find', 'simctl'])).parents[2]
        simulator_app = developer / 'Applications/Simulator.app'
        if not simulator_app.is_dir():
            raise ValueError('Selected Xcode image has no Simulator.app')
        run(['xcrun', 'simctl', 'help', 'bootstatus'])
        launch_help = run(['xcrun', 'simctl', 'help', 'launch'])
        if '--terminate-running-process' not in launch_help:
            raise ValueError('Selected Xcode cannot restart the probe safely on a reused device')
        boot_started = time.monotonic()
        # Give initialization up to three minutes inside the same overall
        # limit, reserving at least 25 seconds for installing/running the probe.
        boot_deadline = min(deadline - 25, boot_started + 180)
        result['boot_timeout_seconds'] = max(0, boot_deadline - boot_started)
        # One boot initiator. Simulator.app also boots shutdown devices, and
        # bootstatus -b can race its asynchronous boot (SimError 405/exit 149).
        # Start with simctl before opening the GUI; then only monitor progress.
        result['simulator_boot_requested_by_this_job'] = initial_state == 'Shutdown'
        if initial_state == 'Shutdown':
            stage('start-selected-simulator')
            run(['xcrun', 'simctl', 'boot', simulator], timeout=result['boot_timeout_seconds'])
            result['simulator_boot_request_accepted'] = True
        else:
            result['simulator_was_already_booted'] = True
        stage('open-selected-simulator-gui')
        run(['/usr/bin/open', '-n', '-a', str(simulator_app), '--args', '-CurrentDeviceUDID', simulator],
            timeout=min(25, max(0, boot_deadline - time.monotonic())))
        result['simulator_gui_requested'] = True
        stage('wait-simulator-boot')
        run(['xcrun', 'simctl', 'bootstatus', simulator, '-d'], timeout=max(0, boot_deadline - time.monotonic()))
        result['boot_elapsed_seconds'] = round(time.monotonic() - boot_started, 3)
        result['simulator_boot_completed'] = True
        stage('install-playback-probe')
        run(['xcrun', 'simctl', 'install', simulator, str(app)])
        stage('locate-probe-report-container')
        container = Path(run(['xcrun', 'simctl', 'get_app_container', simulator, BUNDLE, 'data']))
        stage('prepare-probe-report-storage')
        report = prepare_report_path(container)
        result['probe_report_directory_prepared'] = True
        # Installing onto a reused device preserves application data. Remove
        # the previous probe result before restarting its process.
        report.unlink(missing_ok=True)
        result['previous_probe_report_cleared'] = True
        run_id = uuid.uuid4().hex
        result['probe_run_id'] = run_id
        result['probe_launch_attempted'] = True
        # A freshly booted device has no probe from an earlier live session to
        # restart. Run 7's launch waiter entered FrontBoard termination first.
        # Keep restart protection when reusing an initially running device.
        result['probe_restart_requested'] = initial_state == 'Booted'
        restart_options = ['--terminate-running-process'] if result['probe_restart_requested'] else []
        stage('launch-playback-probe')
        launch = run(['xcrun', 'simctl', 'launch', *restart_options, simulator, BUNDLE,
                      '--stall-probe-run-id', run_id])
        match = re.search(r':\s*(\d+)\s*$', launch)
        process_id = int(match.group(1)) if match else None
        stage('await-real-paused-readiness-and-playback')
        probe_deadline = min(deadline, time.monotonic() + 18)
        probe = {}
        while time.monotonic() < probe_deadline:
            probe = read_probe(report, run_id)
            if probe.get('finished') is True:
                break
            time.sleep(.1)
        retain_probe_report(report, run_id, result, build)
        if result.get('probe_report_retained') is not True or not validate_probe(result.get('probe', {}), run_id):
            raise ValueError('Real simulator playback failed before app compilation; see probe/media/stack diagnostics')
        result.update(passed=True, stage='completed')
        status = 0
    except Exception as error:
        result['error'] = str(error)
        result['error_type'] = type(error).__name__
        if simulator:
            collect_failure(simulator, process_id, build)
        # The app may finish its launch while bounded diagnostics are running.
        # Read the latest report before terminating it, not before collection.
        retain_probe_report(report, run_id, result, build)
    finally:
        if simulator:
            try:
                if process_id or result.get('probe_launch_attempted') is True:
                    run_command(['xcrun', 'simctl', 'terminate', simulator, BUNDLE], log, timeout=5, check=False)
            except Exception:
                pass
            # Preserve the verified runtime for the full suite. Never erase or
            # delete preinstalled devices, or stop one that was already running.
            if ((status or os.environ.get('STALL_PREFLIGHT_ONLY') == 'true')
                    and result.get('simulator_boot_requested_by_this_job') is True):
                try:
                    run_command(['xcrun', 'simctl', 'shutdown', simulator], log, timeout=10)
                    result['selected_simulator_shutdown'] = True
                except Exception as error:
                    result['cleanup_error'] = str(error)
            # The short comparison owns only the new UUID. Never delete or
            # erase a preinstalled device, including the model-selection source.
            if result.get('created_by_this_job') is True:
                try:
                    run_command(['xcrun', 'simctl', 'delete', simulator], log, timeout=5)
                    result['created_simulator_deleted'] = True
                except Exception as error:
                    result['created_simulator_cleanup_error'] = str(error)
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (build / 'simulator-playback-preflight.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
    return status


if __name__ == '__main__':
    sys.exit(main())
