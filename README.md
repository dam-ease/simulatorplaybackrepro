# Standalone iOS Simulator playback reproduction

This sample isolates a UIKit/AVPlayer probe from the application. Start with
**`Tools/simulator_playback_probe.swift` (136 lines)**. It uses only Apple frameworks
and the bundled 12-second H.264/AAC fixture. There is no application project,
Chromecast SDK, CocoaPods dependency, website, account, signing credential or network media.

The two Swift probes, their Python runners, simulator selector and MP4 are copied
byte-for-byte from the source used in build `6acf6cf9546ce6236cee7db6`.
`run.py` is a new standalone entry point that replaces the original repository,
release and CocoaPods checks. `Release/codemagic-focused-recheck.json` is reduced
to the one runtime setting the playback runner reads. `PACKAGE.json` records
source hashes and the original Git blob identities. The `Stall/Resources` folder
contains only the test MP4; its path is preserved for the unchanged runners.

## Run on Codemagic

Place the **contents** of this directory at the root of a temporary support
repository, so `codemagic.yaml` and `run.py` are at its root. No access to the
original private application repository is needed.

The included YAML has two independent, **manual-only, five-minute Mac mini M2**
workflows. Select one explicitly; neither starts the other:

| Workflow | Xcode | iOS runtime | Device | Existing observation |
| --- | --- | --- | --- | --- |
| `reproduce-26-6-launch` | 26.6 | 26.5 | Newly created iPhone of the selected preinstalled model | Run 10: launch timeout after successful boot/install |
| `reproduce-26-5-readiness` | 26.5 | 26.5 | Selected preinstalled iPhone | Run 8: launched, then paused item readiness failed |

The default comparison is the first workflow, matching the latest fresh-device
check. The second lets support investigate the earlier readiness failure. These
are sample configurations for support; the application's workflow is unchanged.

## Run directly on an Apple silicon Mac

Requirements: full Xcode 26.6 or 26.5, Python 3.9 or newer, and an **installed iOS
26.5 simulator runtime with an available iPhone device**. The selector prefers a
Pro Max model; the observed builds selected iPhone 17 Pro Max. Fresh mode creates
a new device using that existing device's model and runtime. Nothing is downloaded
or installed from a package manager.

Unzip and enter `SimulatorPlaybackRepro`. For the latest launch failure:

```bash
DEVELOPER_DIR=/Applications/Xcode-26.6.app/Contents/Developer \
  python3 run.py --simulator fresh --expect-xcode 26.6
```

For the earlier readiness failure:

```bash
DEVELOPER_DIR=/Applications/Xcode-26.5.app/Contents/Developer \
  python3 run.py --simulator preinstalled --expect-xcode 26.5
```

If Xcode is installed elsewhere, use its actual `Contents/Developer` path.
Support can explicitly choose `--simulator fresh --expect-xcode 26.5` for a fresh
26.5-toolchain comparison; **that combination has not been run in the supplied
build history**. The script checks the selected Xcode version and exact runtime;
it does not silently substitute another version or change `xcode-select` globally.

For a portable integrity check that starts no Apple tools:

```bash
python3 run.py --check-only
```

Each native run writes `build/`. The script refuses to overwrite an existing
`build/` directory. Preserve/rename it or unpack a clean copy before another run.
The manifest check also rejects modified source; if support deliberately edits
a listed file, update that file's SHA-256 entry in `PACKAGE.json` and retain the
diff so the changed experiment is distinguishable.

## What executes

1. Record actual Xcode, host macOS version/build, package identity and build ID.
2. Compile the small Mac probe; fully decode the fixture's video and audio,
   generate a fresh nonzero half-second PCM preview, and run bounded host `afplay`.
   This preserves the original host-output preparation. If the default Mac output
   is invalid, the helper selects an available existing output, as in the failed jobs.
3. Compile the 136-line UIKit app with `xcrun swiftc` and construct the exact
   `Info.plist` used in run 10. No Xcode project or external dependencies are needed.
4. Select the exact iOS 26.5 model/runtime. Fresh mode runs `simctl create` with a
   unique name, verifies the returned UUID against inventory, then uses it exclusively.
5. Issue one `simctl boot` if needed, attach the selected Simulator.app, and wait
   for `simctl bootstatus <UUID> -d` to complete before `simctl install` and launch.
6. Prepare writable report storage and launch the probe with a unique run ID.
7. Preserve the original pass requirements and time limits. On failure, collect
   media/launch logs and a live process sample when launch returned a PID.
8. Terminate the dedicated probe. Shut down a device only if this run requested
   its boot. Delete only a new device created by this run. Never erase/delete
   preinstalled devices, restart system services, or automatically retry.

The UIKit app sets the playback/moviePlayback/longFormVideo audio category,
constructs `AVPlayerItem(url:)` and immediately associates it with `AVPlayer`,
attaches a visible player layer, and waits up to eight seconds for paused
`.readyToPlay`. It then activates the audio session and requires one audio track,
a playing clock advancing at least 0.25 seconds, and a nonempty output route.
Failure reporting writes cached observations so it does not call back into a
potentially blocked audio pipeline.

Limits are unchanged: boot up to 180 seconds within the runner/shared budget,
launch up to 25 seconds, runner work up to 210 seconds and shared preparation up
to 240 seconds, with bounded diagnostics/cleanup. The standalone wrapper has an
additional 295-second limit and Codemagic enforces five minutes. A launch or boot
timeout remains a failure even if a player report later appears.

## Observed outcomes and artifacts

| Build ID | Environment | Observed outcome |
| --- | --- | --- |
| `6acf6cf9546ce6236cee7db6` (run 10) | Xcode 26.6 (17F113), macOS 26.5.1 (25F80), M2, fresh iOS 26.5 | Host output passed in 0.944 s; bootstatus completed in 103.934 s; install passed; launch timed out after 25 s without a PID or playback report. New device shutdown/deletion succeeded. |
| `6aceeb7b546ce6236cee5db7` (run 9) | Xcode 26.6 (17F113), M2, preinstalled iOS 26.5 | Host output/boot/install passed; launch timed out after 25 s without a PID or report. |
| `6acedc35546ce6236cee5ab5` (run 8) | Xcode 26.5 (17F42), macOS 26.5 (25F71), M2, preinstalled iOS 26.5 | Host output passed; boot completed in 41.417 s; visible probe launched as PID 2056; matching report failed paused readiness with clock 0. Live sample showed AVPlayer/Core Audio IO initialization waits. |

For run 10, FrontBoard began bootstrapping at **2026-10-14 11:55:57.935 UTC**.
A SplashBoard snapshot timeout/error 8 followed at 11:55:59.956; FrontBoard still
reported a pending app process at 11:55:59.968. These are observations, not a
confirmed cause. That launch failure does **not** establish reproduction of the
earlier audio/readiness stall. The original run ZIPs remain separate evidence.

Expected on a healthy run: exit status 0, `build/support-run.json` and
`build/simulator-playback-preflight.json` both `passed: true`, plus a matching
`build/playback-probe.json` proving paused readiness, audio and clock advancement.
Host audio success alone is insufficient. Exit status 1 means a failed check;
read the recorded stage rather than assuming it is the same failure.

Please retain `build/*.json`, `build/*.txt`, and `build/*.log`, along with the new
build ID. No PID or app sample is available when launch times out before returning
one. For the run 10 symptom, launch/RunningBoard/launchd-side diagnostics may help
explain the pending process; the sample does not diagnose a provider or Apple defect.

## Validation scope

This extracted ZIP has been checked locally for package integrity, Python and
workflow syntax, standalone entry-point behavior and exact source/fixture/package
metadata identity. **The standalone wrapper has not been executed on a Mac.**
The unchanged probes did compile in the cited original builds; their runtime
failures are recorded above. No new Codemagic build was started to prepare this
sample. No real simulator playback pass is claimed.

## Local comparison with Xcode 26.4.1

This adapted package also accepts explicit toolchain and runtime versions. For
the installed Xcode 26.4.1 and iOS 26.4.1 runtime (identifier `iOS-26-4`):

```bash
python3 run.py --simulator fresh --expect-xcode 26.4.1 --simulator-os 26.4
```

Preserve or rename an existing `build/` before running again. This comparison
uses different versions from the Codemagic failures. The reports record the
actual Xcode and runtime identifier. Existing Codemagic defaults remain 26.6/26.5.
The playback runner now reads the runtime override from `STALL_SIMULATOR_OS`.
