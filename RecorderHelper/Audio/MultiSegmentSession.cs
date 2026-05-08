using System.Diagnostics;
using System.Text.Json;
using RecorderHelper.Models;

namespace RecorderHelper.Audio;

/// <summary>
/// Orchestrates multi-segment recording with USB unplug/replug recovery.
/// Manages the segment loop, silence gap insertion, per-segment mixing, and final merge.
/// </summary>
public sealed class MultiSegmentSession
{
    private readonly AppSettings _settings;

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    public MultiSegmentSession(AppSettings settings) => _settings = settings;

    /// <summary>
    /// Runs a fixed-duration recording session. Used by --record-test.
    /// Signature is unchanged — RecordTestCommand calls this directly.
    /// </summary>
    public int Run(
        AudioDeviceInfo? mic,
        AudioDeviceInfo  render,
        string           outputDir,
        string           baseName,
        int              durationSeconds,
        bool             keepTemp,
        float            micGain,
        float            loopGain)
    {
        var stopSignal  = new ManualResetEventSlim(false);
        var forceSignal = new ManualResetEventSlim(false);

        // Fire stopSignal after durationSeconds so RunCore exits the capture loop.
        _ = Task.Delay(TimeSpan.FromSeconds(durationSeconds))
                .ContinueWith(_ => stopSignal.Set());

        return RunCore(
            mic, render, outputDir, baseName, keepTemp, micGain, loopGain,
            stopSignal, forceSignal,
            emitJson:  null,
            writeText: Console.WriteLine,
            writeWarn: Console.Error.WriteLine);
    }

    /// <summary>
    /// Runs an open-ended recording session driven by stop/force signals. Used by --ipc.
    /// emitJson receives pre-serialised JSON event strings; diag receives diagnostic text.
    /// </summary>
    internal int RunIpc(
        AudioDeviceInfo? mic,
        AudioDeviceInfo  render,
        string           outputDir,
        string           baseName,
        bool             keepTemp,
        float            micGain,
        float            loopGain,
        ManualResetEventSlim stopSignal,
        ManualResetEventSlim forceSignal,
        Action<string>   emitJson,
        Action<string>   diag)
    {
        return RunCore(
            mic, render, outputDir, baseName, keepTemp, micGain, loopGain,
            stopSignal, forceSignal,
            emitJson:  emitJson,
            writeText: diag,
            writeWarn: diag);
    }

    // ── Core implementation ──────────────────────────────────────────────────

    private int RunCore(
        AudioDeviceInfo? mic,
        AudioDeviceInfo  render,
        string           outputDir,
        string           baseName,
        bool             keepTemp,
        float            micGain,
        float            loopGain,
        ManualResetEventSlim stopSignal,
        ManualResetEventSlim forceSignal,
        Action<string>?  emitJson,
        Action<string>   writeText,
        Action<string>   writeWarn)
    {
        int  segIndex      = 1;
        var  currentMic    = mic;
        var  currentRender = render;

        var mergeList    = new List<string>();
        var segInfoList  = new List<SegmentInfo>();
        var silencePaths = new List<string>();

        // Serialise and forward a JSON event (no-op in --record-test mode).
        void Emit(object payload) =>
            emitJson?.Invoke(JsonSerializer.Serialize(payload, JsonOpts));

        while (!stopSignal.IsSet && !forceSignal.IsSet)
        {
            // ── Capture one segment ──────────────────────────────────────────
            var segBaseName = $"{baseName}_seg{segIndex}";
            var mixedPath   = Path.Combine(outputDir, $"{segBaseName}_mixed.wav");

            writeText($"");
            writeText($"[segment {segIndex}] Starting capture");
            writeText($"[segment {segIndex}] Mic:    {currentMic?.FriendlyName ?? "(none — loopback only)"}");
            writeText($"[segment {segIndex}] Render: {currentRender.FriendlyName}");

            CaptureStats stats;
            bool         deviceLost;
            string?      micTempPath;
            string       loopTempPath;
            long         lostAt = 0;

            try
            {
                using var captureSession = new WasapiCaptureSession(
                    currentMic, currentRender, outputDir, segBaseName);

                micTempPath  = captureSession.MicTempPath;
                loopTempPath = captureSession.LoopTempPath;

                writeText($"[segment {segIndex}] Mic temp:      {micTempPath ?? "(none)"}");
                writeText($"[segment {segIndex}] Loopback temp: {loopTempPath}");
                writeText($"[segment {segIndex}] Recording...");

                captureSession.Start();

                // Emit started only after capture has actually begun.
                Emit(new { @event = "started", segment = segIndex, path = mixedPath });

                // Block until a stop/force signal or a spontaneous device loss.
                while (!captureSession.AnyDeviceLost.IsSet
                       && !stopSignal.IsSet
                       && !forceSignal.IsSet)
                    Thread.Sleep(100);

                if (captureSession.AnyDeviceLost.IsSet)
                    lostAt = Stopwatch.GetTimestamp();

                stats      = captureSession.Stop();
                deviceLost = captureSession.DeviceLostDuringCapture;
            }
            catch (Exception ex)
            {
                writeWarn($"[segment {segIndex}] Capture exception: {ex.Message}");
                writeWarn("[segment] Ending recording with segments captured so far.");
                Emit(new { @event = "error", code = "capture_exception", message = ex.Message });
                break;
            }

            writeText($"[segment {segIndex}] Stopped");
            writeText($"[segment {segIndex}] Mic bytes:      {stats.MicBytesWritten:N0}");
            writeText($"[segment {segIndex}] Loopback bytes: {stats.LoopbackBytesWritten:N0}");
            if (stats.MicNativeFormat      is not null) writeText($"[segment {segIndex}] Mic format:    {stats.MicNativeFormat}");
            if (stats.LoopbackNativeFormat is not null) writeText($"[segment {segIndex}] Loop format:   {stats.LoopbackNativeFormat}");

            // ── Mix this segment ─────────────────────────────────────────────
            bool loopHasData = HasAudioData(loopTempPath);
            bool micHasData  = HasAudioData(micTempPath);

            if (!loopHasData)
            {
                writeWarn($"[segment {segIndex}] Loopback has no audio data — skipping segment.");
            }
            else
            {
                writeText($"[segment {segIndex}] Mixing → {mixedPath}");
                bool mixOk = false;

                try
                {
                    var result = WavMixer.Mix(
                        micPath:      micHasData ? micTempPath : null,
                        loopbackPath: loopTempPath,
                        outputPath:   mixedPath,
                        micGain:      micGain,
                        loopGain:     loopGain);

                    if (result.Success)
                    {
                        writeText($"[segment {segIndex}] Mixed — {result.OutputBytes:N0}B  ~{result.DurationSeconds:F1}s");
                        if (result.ClippedSamples > 0)
                            writeWarn($"[WARN] Clipping: {result.ClippedSamples:N0} samples clipped (consider reducing gains)");

                        mergeList.Add(mixedPath);
                        segInfoList.Add(new SegmentInfo(segIndex, micTempPath, loopTempPath, mixedPath, stats));
                        mixOk = true;

                        // seg_saved fires only when a valid mixed segment is available.
                        Emit(new { @event = "seg_saved", segment = segIndex, path = mixedPath });
                    }
                    else
                    {
                        writeWarn($"[segment {segIndex}] Mix reported failure — segment excluded.");
                    }
                }
                catch (Exception ex)
                {
                    writeWarn($"[segment {segIndex}] Mix exception: {ex.Message}");
                }

                if (!mixOk)
                    writeWarn($"[segment {segIndex}] Temp files preserved for inspection.");
            }

            if (!deviceLost) break;

            // ── USB recovery ─────────────────────────────────────────────────
            writeText($"");
            writeText($"[recovery] Device loss detected — waiting for replug...");
            Emit(new { @event = "device_lost", segment = segIndex });

            string? originalMicName    = currentMic?.FriendlyName;
            string  originalRenderName = currentRender.FriendlyName;

            var (newMic, newRender, recoveredAt) =
                WaitForDeviceRecovery(originalMicName, originalRenderName,
                    stopSignal, forceSignal, writeText, writeWarn);

            if (newRender is null)
            {
                writeWarn("[recovery] Devices did not return. Ending recording with segments captured so far.");
                break;
            }

            double gapSeconds = (recoveredAt - lostAt) / (double)Stopwatch.Frequency;
            writeText($"[recovery] Devices returned — gap={gapSeconds:F1}s");
            Emit(new { @event = "device_restored", segment = segIndex + 1, gapSeconds = Math.Round(gapSeconds, 1) });

            if (gapSeconds >= _settings.GapSilenceThresholdSeconds)
            {
                string silencePath = Path.Combine(outputDir, $"{baseName}_silence_{segIndex}.wav");
                try
                {
                    SilenceGenerator.Generate(silencePath, gapSeconds);
                    silencePaths.Add(silencePath);
                    mergeList.Add(silencePath);
                    writeText($"[silence] Generated {gapSeconds:F1}s → {silencePath}");
                }
                catch (Exception ex)
                {
                    writeWarn($"[silence] Failed to generate silence WAV: {ex.Message}");
                }
            }
            else
            {
                writeText($"[silence] Gap {gapSeconds:F2}s < threshold {_settings.GapSilenceThresholdSeconds}s — no silence inserted.");
            }

            currentMic    = newMic;
            currentRender = newRender;
            segIndex++;
        }

        // ── Merge ─────────────────────────────────────────────────────────────
        writeText($"");

        if (mergeList.Count == 0)
        {
            writeWarn("[merge] No valid mixed segments — cannot produce output.");
            Emit(new { @event = "merge_failed", error = "no valid segments", segments = Array.Empty<string>() });
            return 1;
        }

        string finalPath = Path.Combine(outputDir, $"{baseName}_seg1.wav");

        writeText($"[merge] {mergeList.Count} item(s) → {finalPath}");
        foreach (var f in mergeList)
            writeText($"[merge]   {f}");

        bool merged = SegmentMerger.Merge(mergeList, finalPath);

        if (!merged || !HasAudioData(finalPath))
        {
            writeWarn("[merge] Merge failed or output is empty. All temp files preserved.");
            Emit(new { @event = "merge_failed", error = "merge failed or empty output", segments = mergeList.ToArray() });
            return 1;
        }

        long   finalSize = new FileInfo(finalPath).Length;
        double duration  = (finalSize - 44) / (double)(PcmNormalizer.TargetSampleRate * 2);

        writeText($"");
        writeText($"--- Output ---");
        writeText($"  Final output:  {finalPath}");
        writeText($"  Size:          {finalSize:N0} bytes");
        writeText($"  Duration:      ~{duration:F1}s");

        Emit(new { @event = "merged", path = finalPath, segments = segInfoList.Count });

        // ── Cleanup ──────────────────────────────────────────────────────────
        if (keepTemp)
        {
            writeText($"");
            writeText($"Temp files kept (--keep-temp):");
            foreach (var seg in segInfoList)
            {
                if (seg.MicTempPath is not null) writeText($"  {seg.MicTempPath}");
                writeText($"  {seg.LoopTempPath}");
                writeText($"  {seg.MixedPath}");
            }
            foreach (var s in silencePaths)
                writeText($"  {s}");
            writeText($"  {finalPath}  ← final merged output");
        }
        else
        {
            foreach (var seg in segInfoList)
            {
                TryDelete(seg.MicTempPath, "mic temp", writeText, writeWarn);
                TryDelete(seg.LoopTempPath, "loopback temp", writeText, writeWarn);
                if (!string.Equals(seg.MixedPath, finalPath, StringComparison.OrdinalIgnoreCase))
                    TryDelete(seg.MixedPath, "mixed temp", writeText, writeWarn);
            }
            foreach (var s in silencePaths)
                TryDelete(s, "silence temp", writeText, writeWarn);
        }

        writeText($"");
        writeText($"[done] Exit 0.");
        return 0;
    }

    // ── Private helpers ──────────────────────────────────────────────────────

    /// <summary>
    /// Polls every 500ms until the original devices (matched by FriendlyName) reappear,
    /// a 60s hard timeout elapses, or a stop/force signal fires.
    /// </summary>
    private (AudioDeviceInfo? mic, AudioDeviceInfo? render, long recoveredAt)
        WaitForDeviceRecovery(
            string? originalMicName,
            string  originalRenderName,
            ManualResetEventSlim stopSignal,
            ManualResetEventSlim forceSignal,
            Action<string> writeText,
            Action<string> writeWarn)
    {
        const int    PollMs          = 500;
        const double RecoveryMaxSec  = 60.0;

        long recoveryDeadline = Stopwatch.GetTimestamp() + (long)(RecoveryMaxSec * Stopwatch.Frequency);
        int  attempt          = 0;

        while (Stopwatch.GetTimestamp() < recoveryDeadline
               && !stopSignal.IsSet
               && !forceSignal.IsSet)
        {
            Thread.Sleep(PollMs);
            attempt++;

            if (attempt % 4 == 0)
                writeText($"[recovery] Waiting... {attempt * PollMs / 1000.0:F0}s elapsed");

            try
            {
                var captures = DeviceResolver.EnumerateCaptureDevices();
                var renders  = DeviceResolver.EnumerateRenderDevices();

                var foundRender = renders.FirstOrDefault(d =>
                    d.FriendlyName.Equals(originalRenderName, StringComparison.OrdinalIgnoreCase));

                if (foundRender is null) continue;

                AudioDeviceInfo? foundMic = null;
                if (originalMicName is not null)
                {
                    foundMic = captures.FirstOrDefault(d =>
                        d.FriendlyName.Equals(originalMicName, StringComparison.OrdinalIgnoreCase));
                    if (foundMic is null) continue;
                }

                long recoveredAt = Stopwatch.GetTimestamp();
                writeText($"[recovery] Device found — mic={foundMic?.FriendlyName ?? "(none)"}  render={foundRender.FriendlyName}");
                return (foundMic, foundRender, recoveredAt);
            }
            catch
            {
                // MMDeviceEnumerator can throw transiently during USB replug — retry on next poll
            }
        }

        if (stopSignal.IsSet || forceSignal.IsSet)
            writeWarn("[recovery] Aborted — stop signal received.");
        else
            writeWarn("[recovery] Timed out waiting for device return.");

        return (null, null, 0);
    }

    /// <summary>Returns true if the file exists and has more than just a WAV header (>200 bytes).</summary>
    private static bool HasAudioData(string? path) =>
        path is not null && File.Exists(path) && new FileInfo(path).Length > 200;

    private static void TryDelete(string? path, string label, Action<string> writeText, Action<string> writeWarn)
    {
        if (path is null) return;
        try
        {
            if (File.Exists(path))
            {
                File.Delete(path);
                writeText($"[cleanup] Deleted {label}: {path}");
            }
        }
        catch (Exception ex)
        {
            writeWarn($"[WARN] Could not delete {label} ({path}): {ex.Message}");
        }
    }
}
