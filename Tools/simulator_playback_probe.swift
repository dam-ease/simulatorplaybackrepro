import AVFoundation
import Foundation
import UIKit

// A simulator-only UIKit app. No app source, SDK, StoreKit or website handoff.
// It exercises the same paused URL-player setup on a visible playback surface.
@main
@MainActor
final class SimulatorPlaybackProbe: NSObject, UIApplicationDelegate {
    var window: UIWindow?
    private var player: AVPlayer?
    private var item: AVPlayerItem?
    private var result: [String: Any] = ["passed": false, "finished": false,
                                      "schema": 1, "physical_sound_verified": false,
                                      "elapsed_seconds": 0, "item_status": -1, "player_status": -1,
                                      "time_control_status": -1, "media_error": "none",
                                      "validation_run_id": CommandLine.arguments.last ?? ""]

    static func main() {
        _ = UIApplicationMain(CommandLine.argc, CommandLine.unsafeArgv, nil, NSStringFromClass(SimulatorPlaybackProbe.self))
    }

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        save(stage: "configure-audio")
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback,
                                                           policy: .longFormVideo, options: [])
            captureAudioOutput()
            guard let url = Bundle.main.url(forResource: "fullscreen-check", withExtension: "mp4") else {
                throw NSError(domain: "StallPlaybackProbe", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "Missing bundled fixture"])
            }
            save(stage: "create-player")
            let item = AVPlayerItem(url: url)
            let player = AVPlayer(playerItem: item)
            self.item = item; self.player = player
            result["item_status"] = item.status.rawValue
            result["player_status"] = player.status.rawValue
            result["time_control_status"] = player.timeControlStatus.rawValue
            player.automaticallyWaitsToMinimizeStalling = true
            player.allowsExternalPlayback = true
            player.audiovisualBackgroundPlaybackPolicy = .continuesIfPossible
            let controller = UIViewController()
            controller.view.backgroundColor = .black
            let window = UIWindow(frame: UIScreen.main.bounds)
            window.rootViewController = controller
            window.makeKeyAndVisible()
            let layer = AVPlayerLayer(player: player)
            layer.frame = controller.view.bounds
            layer.videoGravity = .resizeAspect
            controller.view.layer.addSublayer(layer)
            self.window = window
            save(stage: "await-paused-readiness")
            Task { await verifyPlayback() }
        } catch {
            finish(error: String(describing: error))
        }
        return true
    }

    private func verifyPlayback() async {
        guard let player, let item else { finish(error: "Player was not created"); return }
        for _ in 0..<80 {
            if item.status != .unknown { break }
            try? await Task.sleep(for: .milliseconds(100))
        }
        result["item_status"] = item.status.rawValue
        guard item.status == .readyToPlay, item.presentationSize.width > 0, player.rate == 0 else {
            finish(error: "Paused URL item did not become ready on its visible surface"); return
        }
        result["paused_ready"] = true
        do {
            let tracks = try await item.asset.loadTracks(withMediaType: .audio)
            result["audio_track_count"] = tracks.count
            guard tracks.count == 1 else { finish(error: "Audio track must remain present"); return }
            save(stage: "activate-audio")
            try AVAudioSession.sharedInstance().setActive(true)
            captureAudioOutput()
        } catch { finish(error: String(describing: error)); return }
        save(stage: "advance-audiovisual-playback")
        player.playImmediately(atRate: 1)
        for _ in 0..<40 {
            result["item_status"] = item.status.rawValue
            result["time_control_status"] = player.timeControlStatus.rawValue
            if item.status == .readyToPlay, player.timeControlStatus == .playing {
                // This clock read is required playback evidence. Never perform
                // it again while reporting a failure on an unready/stalled item.
                save(stage: "sample-playing-clock")
                let seconds = player.currentTime().seconds
                result["elapsed_seconds"] = seconds.isFinite ? seconds : 0
                let audio = AVAudioSession.sharedInstance()
                if seconds.isFinite, seconds >= 0.25, !audio.currentRoute.outputs.isEmpty,
                   audio.outputNumberOfChannels > 0, audio.sampleRate > 0 {
                    captureAudioOutput()
                    result["player_status"] = player.status.rawValue
                    result["passed"] = true
                    finish(error: nil); return
                }
            }
            if item.status == .failed { break }
            try? await Task.sleep(for: .milliseconds(100))
        }
        finish(error: "Playback did not advance with an audio output")
    }

    private func save(stage: String) {
        // Run 5 reached finish(error:) but this writer blocked in currentTime().
        // Persist only already-observed facts: error reporting must not call
        // AVPlayer/AVAudioSession back into the stalled audio pipeline.
        result["stage"] = stage
        guard let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first,
              let data = try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]) else { return }
        try? data.write(to: directory.appendingPathComponent("playback-probe.json"), options: .atomic)
    }

    private func captureAudioOutput() {
        let audio = AVAudioSession.sharedInstance()
        result["route"] = audio.currentRoute.outputs.map { $0.portType.rawValue + ":" + $0.portName }
        result["output_channels"] = audio.outputNumberOfChannels
        result["sample_rate"] = audio.sampleRate
    }

    private func finish(error: String?) {
        result["finished"] = true
        if let error { result["error"] = error; result["passed"] = false }
        save(stage: error == nil ? "completed" : "failed")
        print("STALL_SIMULATOR_PLAYBACK_PROBE|\(result)")
        // Keep failed initialization alive for the parent's thread sample.
        // The bounded runner captures it, then terminates this probe.
        if error != nil { return }
        player?.pause()
        player?.replaceCurrentItem(with: nil)
        exit(0)
    }
}
