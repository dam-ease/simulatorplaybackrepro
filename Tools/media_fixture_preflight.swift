import AVFoundation
import AudioToolbox
import CoreMedia
import CoreAudio
import CoreVideo
import Foundation

// Mac-host decoding before app/SDK compilation. Write a short PCM preview
// of the same fixture for the runner's bounded host-output check. A pass is
// not an iOS, AVPlayer readiness, handoff or background-audio pass.
@main
struct MediaFixturePreflight {
    static func require(_ condition: Bool, _ detail: String) throws {
        if !condition { throw NSError(domain: "StallMediaFixture", code: 1,
                                      userInfo: [NSLocalizedDescriptionKey: detail]) }
    }

    static func integerProperty(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32 {
        var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value) == noErr ? value : 0
    }

    static func outputChannels(_ device: AudioDeviceID) -> UInt32 {
        var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamConfiguration,
                                                 mScope: kAudioObjectPropertyScopeOutput,
                                                 mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(device, &address, 0, nil, &size) == noErr,
              size >= UInt32(MemoryLayout<AudioBufferList>.size) else { return 0 }
        let memory = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { memory.deallocate() }
        guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, memory) == noErr else { return 0 }
        return UnsafeMutableAudioBufferListPointer(memory.assumingMemoryBound(to: AudioBufferList.self))
            .reduce(0) { $0 + $1.mNumberChannels }
    }

    static func prepareHostAudio() throws -> [String: Any] {
        let system = AudioObjectID(kAudioObjectSystemObject)
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                                 mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        try require(AudioObjectGetPropertyDataSize(system, &address, 0, nil, &size) == noErr && size > 0,
                    "Mac host has no audio device inventory")
        var devices = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        let status = devices.withUnsafeMutableBytes {
            AudioObjectGetPropertyData(system, &address, 0, nil, &size, $0.baseAddress!)
        }
        try require(status == noErr, "Could not read Mac audio devices")
        let inventory: [[String: Any]] = devices.map { device in
            ["id": device, "alive": integerProperty(device, kAudioDevicePropertyDeviceIsAlive) == 1,
             "output_channels": outputChannels(device),
             "built_in": integerProperty(device, kAudioDevicePropertyTransportType) == kAudioDeviceTransportTypeBuiltIn]
        }
        let usable = devices.filter { integerProperty($0, kAudioDevicePropertyDeviceIsAlive) == 1 && outputChannels($0) > 0 }
        let oldDefault = integerProperty(system, kAudioHardwarePropertyDefaultOutputDevice)
        if usable.isEmpty {
            return ["devices": inventory, "old_default_output": oldDefault, "host_output_valid": false,
                    "default_output_repaired": false, "physical_sound_verified": false]
        }
        var selected = oldDefault
        var repaired = false
        if !usable.contains(oldDefault) {
            selected = usable.first { integerProperty($0, kAudioDevicePropertyTransportType) == kAudioDeviceTransportTypeBuiltIn } ?? usable[0]
            address.mSelector = kAudioHardwarePropertyDefaultOutputDevice
            try require(AudioObjectSetPropertyData(system, &address, 0, nil,
                                                  UInt32(MemoryLayout<AudioDeviceID>.size), &selected) == noErr,
                        "Could not select an available Mac audio output")
            repaired = true
        }
        let current = integerProperty(system, kAudioHardwarePropertyDefaultOutputDevice)
        try require(usable.contains(current), "Mac default output is still unavailable")
        return ["devices": inventory, "old_default_output": oldDefault, "default_output": current,
                "default_output_repaired": repaired, "default_input": integerProperty(system, kAudioHardwarePropertyDefaultInputDevice),
                "host_output_valid": true, "physical_sound_verified": false]
    }

    static func decode(_ asset: AVAsset, track: AVAssetTrack, video: Bool) throws -> Int {
        let reader = try AVAssetReader(asset: asset)
        let settings: [String: Any] = video
            ? [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
            : [AVFormatIDKey: kAudioFormatLinearPCM]
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: settings)
        output.alwaysCopiesSampleData = false
        try require(reader.canAdd(output), "Cannot attach fixture decoder output")
        reader.add(output)
        try require(reader.startReading(), "Decoder could not start: \(String(describing: reader.error))")
        var count = 0
        while let sample = output.copyNextSampleBuffer() {
            try require(CMSampleBufferDataIsReady(sample), "Fixture sample is not ready")
            if video {
                try require(CMSampleBufferGetImageBuffer(sample) != nil, "Video was not decoded")
            } else {
                try require(CMSampleBufferGetNumSamples(sample) > 0, "Audio was not decoded")
                try require(CMSampleBufferGetFormatDescription(sample).map {
                    CMFormatDescriptionGetMediaSubType($0) == kAudioFormatLinearPCM
                } == true, "Audio decoder did not produce PCM")
                try require(CMSampleBufferGetDataBuffer(sample).map {
                    CMBlockBufferGetDataLength($0) > 0
                } == true, "Decoded audio has no sample data")
            }
            count += 1
            try require(count <= 6000, "Fixture contains unexpectedly many samples")
        }
        try require(count > 0 && reader.status == .completed,
                    "Decoder did not complete: \(String(describing: reader.error))")
        return count
    }

    static func writeAudioPreview(_ asset: AVAsset, track: AVAssetTrack, url: URL) throws -> Int {
        let settings: [String: Any] = [AVFormatIDKey: kAudioFormatLinearPCM,
                                      AVSampleRateKey: 48000, AVNumberOfChannelsKey: 2,
                                      AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true,
                                      AVLinearPCMIsBigEndianKey: false,
                                      AVLinearPCMIsNonInterleaved: true]
        let reader = try AVAssetReader(asset: asset)
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: settings)
        try require(reader.canAdd(output), "Cannot attach the host-output preview decoder")
        reader.add(output)
        try require(reader.startReading(), "Cannot start the host-output preview decoder")
        defer { reader.cancelReading() }
        guard let format = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 2) else {
            throw NSError(domain: "StallMediaFixture", code: 3,
                          userInfo: [NSLocalizedDescriptionKey: "Cannot create the PCM preview format"])
        }
        var fileSettings = settings
        fileSettings[AVLinearPCMIsNonInterleaved] = false
        let file = try AVAudioFile(forWriting: url, settings: fileSettings,
                                  commonFormat: .pcmFormatFloat32, interleaved: false)
        var written = 0
        var nonzero = false
        // The preview is actual fixture audio, not a synthesized or muted substitute.
        while written < 24000, let sample = output.copyNextSampleBuffer() {
            let frames = min(CMSampleBufferGetNumSamples(sample), 24000 - written)
            try require(CMSampleBufferDataIsReady(sample) && frames > 0, "Preview PCM is not ready")
            guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)),
                  let channels = buffer.floatChannelData else {
                throw NSError(domain: "StallMediaFixture", code: 4,
                              userInfo: [NSLocalizedDescriptionKey: "Cannot allocate the PCM preview"])
            }
            buffer.frameLength = AVAudioFrameCount(frames)
            try require(CMSampleBufferCopyPCMDataIntoAudioBufferList(sample, at: 0,
                            frameCount: Int32(frames), into: buffer.mutableAudioBufferList) == noErr,
                        "Cannot copy fixture PCM into the preview")
            for channel in 0..<2 {
                for frame in 0..<frames {
                    let value = channels[channel][frame]
                    try require(value.isFinite, "Preview contains invalid PCM")
                    if abs(value) > 0.000001 { nonzero = true }
                }
            }
            try file.write(from: buffer)
            written += frames
        }
        try require(written == 24000 && nonzero, "Preview must contain half a second of real fixture audio")
        return written
    }

    static func saveProgress(_ result: [String: Any]) {
        guard CommandLine.arguments.count == 4,
              let data = try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]) else { return }
        try? data.write(to: URL(fileURLWithPath: CommandLine.arguments[3]), options: .atomic)
    }

    static func main() async {
        var result: [String: Any] = ["passed": false, "platform": "macOS-host",
                                    "ios_readiness_verified": false, "playback_verified": false,
                                    "stage": "prepare-host-audio-inventory"]
        saveProgress(result)
        do {
            try require(CommandLine.arguments.count == 4, "Expected fixture, audio preview and progress paths")
            let hostAudio = try prepareHostAudio()
            result["host_audio"] = hostAudio
            try require(hostAudio["host_output_valid"] as? Bool == true,
                        "Mac host has no live audio output; stop before compiling the app")
            result["stage"] = "decode-media-fixture"
            saveProgress(result)
            let asset = AVURLAsset(url: URL(fileURLWithPath: CommandLine.arguments[1]))
            let playable = try await asset.load(.isPlayable)
            let duration = try await asset.load(.duration).seconds
            let videos = try await asset.loadTracks(withMediaType: .video)
            let audios = try await asset.loadTracks(withMediaType: .audio)
            try require(playable && duration.isFinite && duration >= 11.9 && duration <= 12.1,
                        "Fixture must contain approximately twelve playable seconds")
            try require(videos.count == 1 && audios.count == 1, "Expected one video and one audio track")
            let size = try await videos[0].load(.naturalSize)
            let videoFormats = try await videos[0].load(.formatDescriptions)
            let audioFormats = try await audios[0].load(.formatDescriptions)
            try require(size.width == 320 && size.height == 180, "Fixture dimensions changed")
            try require(videoFormats.first.map { CMFormatDescriptionGetMediaSubType($0) == kCMVideoCodecType_H264 } == true,
                        "Fixture video is not H.264")
            guard let audioFormat = audioFormats.first,
                  let audio = CMAudioFormatDescriptionGetStreamBasicDescription(audioFormat)?.pointee else {
                throw NSError(domain: "StallMediaFixture", code: 2,
                              userInfo: [NSLocalizedDescriptionKey: "Missing audio format"])
            }
            try require(audio.mFormatID == kAudioFormatMPEG4AAC && audio.mSampleRate == 48000 && audio.mChannelsPerFrame == 2,
                        "Fixture must contain 48 kHz stereo AAC")
            result["video_decoded_buffers"] = try decode(asset, track: videos[0], video: true)
            result["audio_decoded_buffers"] = try decode(asset, track: audios[0], video: false)
            result["duration_seconds"] = duration
            result["audio_sample_rate"] = audio.mSampleRate
            result["audio_channels"] = audio.mChannelsPerFrame
            result["stage"] = "write-fixture-audio-preview"
            saveProgress(result)
            result["audio_preview_frames"] = try writeAudioPreview(asset, track: audios[0],
                url: URL(fileURLWithPath: CommandLine.arguments[2]))
            result["audio_preview_nonzero"] = true
            result["passed"] = true
            result["stage"] = "decoded-fixture-and-audio-preview"
        } catch {
            result["error"] = String(describing: error)
        }
        saveProgress(result)
        if let data = try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]),
           let text = String(data: data, encoding: .utf8) { print(text) }
        exit(result["passed"] as? Bool == true ? 0 : 1)
    }
}
