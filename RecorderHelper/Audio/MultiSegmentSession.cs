using System.Diagnostics;
using RecorderHelper.Models;

namespace RecorderHelper.Audio;

/// <summary>
/// Orchestrates multi-segment recording with USB unplug/replug recovery.
/// Manages the segment loop, silence gap insertion, per-segment mixing, and final merge.
/// </summary>
public sealed class MultiSegmentSession
{
    private readonly AppSettings _settings;

    public MultiSegmentSession(AppSettings settings) => _settings = settings;

    /// <summary>
    /// Runs the full recording session.
    /// </summary>
    /// <returns>0 on success (final WAV produced), 1 if no valid audio could be produced.</returns>
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
        long deadline    = Stopwatch.GetTimestamp() + (long)(durationSeconds * Stopwatch.Frequency);
        int  segIndex    = 1;
        var  currentMic    = mic;
        var  currentRender = render;

        // Ordered list for SegmentMerger: mixed segment WAVs interleaved with silence WAVs
        var mergeList    = new List<string>();
        var segInfoList  = new List<SegmentInfo>();
        var silencePaths = new List<string>();

        while (true)
        {
            if (RemainingSeconds(deadline) < 0.5) break;

            // ── Capture one segment ──────────────────────────────────────────
            var segBaseName = $"{baseName}_seg{segIndex}";
            var mixedPath   = Path.Combine(outputDir, $"{segBaseName}_mixed.wav");

            Console.WriteLine();
            Console.WriteLine($"[segment {segIndex}] Starting capture");
            Console.WriteLine($"[segment {segIndex}] Mic:    {currentMic?.FriendlyName ?? "(none — loopback only)"}");
            Console.WriteLine($"[segment {segIndex}] Render: {currentRender.FriendlyName}");

            CaptureStats stats;
            bool         deviceLost;
            string?      micTempPath;
            string       loopTempPath;
            long         lostAt = 0;

            try
            {
                using var session = new WasapiCaptureSession(
                    currentMic, currentRender, outputDir, segBaseName);

                micTempPath  = session.MicTempPath;
                loopTempPath = session.LoopTempPath;

                Console.WriteLine($"[segment {segIndex}] Mic temp:      {micTempPath ?? "(none)"}");
                Console.WriteLine($"[segment {segIndex}] Loopback temp: {loopTempPath}");
                Console.WriteLine($"[segment {segIndex}] Recording...");

                session.Start();

                // Block until deadline or spontaneous device loss
                while (!session.AnyDeviceLost.IsSet && Stopwatch.GetTimestamp() < deadline)
                    Thread.Sleep(100);

                if (session.AnyDeviceLost.IsSet)
                    lostAt = Stopwatch.GetTimestamp();

                stats      = session.Stop();
                deviceLost = session.DeviceLostDuringCapture;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[segment {segIndex}] Capture exception: {ex.Message}");
                Console.Error.WriteLine("[segment] Ending recording with segments captured so far.");
                break;
            }

            Console.WriteLine($"[segment {segIndex}] Stopped");
            Console.WriteLine($"[segment {segIndex}] Mic bytes:      {stats.MicBytesWritten:N0}");
            Console.WriteLine($"[segment {segIndex}] Loopback bytes: {stats.LoopbackBytesWritten:N0}");
            if (stats.MicNativeFormat     is not null) Console.WriteLine($"[segment {segIndex}] Mic format:    {stats.MicNativeFormat}");
            if (stats.LoopbackNativeFormat is not null) Console.WriteLine($"[segment {segIndex}] Loop format:   {stats.LoopbackNativeFormat}");

            // ── Mix this segment ─────────────────────────────────────────────
            bool loopHasData = HasAudioData(loopTempPath);
            bool micHasData  = HasAudioData(micTempPath);

            if (!loopHasData)
            {
                Console.Error.WriteLine($"[segment {segIndex}] Loopback has no audio data — skipping segment.");
            }
            else
            {
                Console.WriteLine($"[segment {segIndex}] Mixing → {mixedPath}");
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
                        Console.WriteLine($"[segment {segIndex}] Mixed — {result.OutputBytes:N0}B  ~{result.DurationSeconds:F1}s");
                        if (result.ClippedSamples > 0)
                            Console.Error.WriteLine($"[WARN] Clipping: {result.ClippedSamples:N0} samples exceeded [-1,1] (consider reducing mic_gain or loopback_gain)");
                        mergeList.Add(mixedPath);
                        segInfoList.Add(new SegmentInfo(segIndex, micTempPath, loopTempPath, mixedPath, stats));
                        mixOk = true;
                    }
                    else
                    {
                        Console.Error.WriteLine($"[segment {segIndex}] Mix reported failure — segment excluded.");
                    }
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine($"[segment {segIndex}] Mix exception: {ex.Message}");
                }

                if (!mixOk)
                {
                    Console.Error.WriteLine($"[segment {segIndex}] Temp files preserved for inspection:");
                    if (micTempPath is not null) Console.Error.WriteLine($"  {micTempPath}");
                    Console.Error.WriteLine($"  {loopTempPath}");
                }
            }

            if (!deviceLost) break;

            // ── USB recovery ─────────────────────────────────────────────────
            Console.WriteLine();
            Console.WriteLine($"[recovery] Device loss detected — waiting for replug...");

            string? originalMicName    = currentMic?.FriendlyName;
            string  originalRenderName = currentRender.FriendlyName;

            var (newMic, newRender, recoveredAt) =
                WaitForDeviceRecovery(originalMicName, originalRenderName, deadline);

            if (newRender is null)
            {
                Console.Error.WriteLine("[recovery] Devices did not return before deadline.");
                Console.Error.WriteLine("[recovery] Ending recording with segments captured so far.");
                break;
            }

            double gapSeconds = (recoveredAt - lostAt) / (double)Stopwatch.Frequency;
            Console.WriteLine($"[recovery] Devices returned — gap={gapSeconds:F1}s");

            if (gapSeconds >= _settings.GapSilenceThresholdSeconds)
            {
                string silencePath = Path.Combine(outputDir, $"{baseName}_silence_{segIndex}.wav");
                try
                {
                    SilenceGenerator.Generate(silencePath, gapSeconds);
                    silencePaths.Add(silencePath);
                    mergeList.Add(silencePath);
                    Console.WriteLine($"[silence] Generated {gapSeconds:F1}s → {silencePath}");
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine($"[silence] Failed to generate silence WAV: {ex.Message}");
                }
            }
            else
            {
                Console.WriteLine($"[silence] Gap {gapSeconds:F2}s < threshold {_settings.GapSilenceThresholdSeconds}s — no silence inserted.");
            }

            currentMic    = newMic;
            currentRender = newRender;
            segIndex++;
        }

        // ── Merge ─────────────────────────────────────────────────────────────
        Console.WriteLine();

        if (mergeList.Count == 0)
        {
            Console.Error.WriteLine("[merge] No valid mixed segments available — cannot produce output.");
            return 1;
        }

        string finalPath = Path.Combine(outputDir, $"{baseName}_seg1.wav");

        Console.WriteLine($"[merge] {mergeList.Count} item(s) to merge:");
        foreach (var f in mergeList)
            Console.WriteLine($"[merge]   {f}");
        Console.WriteLine($"[merge] Output: {finalPath}");

        bool merged = SegmentMerger.Merge(mergeList, finalPath);

        if (!merged || !HasAudioData(finalPath))
        {
            Console.Error.WriteLine("[merge] Merge failed or output is empty.");
            Console.Error.WriteLine("[done]  All temp files preserved. Exit 1.");
            return 1;
        }

        long   finalSize = new FileInfo(finalPath).Length;
        double duration  = (finalSize - 44) / (double)(PcmNormalizer.TargetSampleRate * 2);

        Console.WriteLine();
        Console.WriteLine("--- Output ---");
        Console.WriteLine($"  Final output:  {finalPath}");
        Console.WriteLine($"  Size:          {finalSize:N0} bytes");
        Console.WriteLine($"  Duration:      ~{duration:F1}s");

        // ── Cleanup ──────────────────────────────────────────────────────────
        if (keepTemp)
        {
            Console.WriteLine();
            Console.WriteLine("Temp files kept (--keep-temp):");
            foreach (var seg in segInfoList)
            {
                if (seg.MicTempPath is not null) Console.WriteLine($"  {seg.MicTempPath}");
                Console.WriteLine($"  {seg.LoopTempPath}");
                Console.WriteLine($"  {seg.MixedPath}");
            }
            foreach (var s in silencePaths)
                Console.WriteLine($"  {s}");
            Console.WriteLine($"  {finalPath}  ← final merged output");
        }
        else
        {
            foreach (var seg in segInfoList)
            {
                TryDelete(seg.MicTempPath, "mic temp");
                TryDelete(seg.LoopTempPath, "loopback temp");
                // Don't delete mixedPath if SegmentMerger used File.Copy and it IS the final
                if (!string.Equals(seg.MixedPath, finalPath, StringComparison.OrdinalIgnoreCase))
                    TryDelete(seg.MixedPath, "mixed temp");
            }
            foreach (var s in silencePaths)
                TryDelete(s, "silence temp");
        }

        Console.WriteLine();
        Console.WriteLine("[done] Exit 0.");
        return 0;
    }

    // ── Private helpers ──────────────────────────────────────────────────────

    /// <summary>
    /// Polls every 500ms until the originally-selected devices (matched by FriendlyName) reappear,
    /// the 60s recovery timeout elapses, or the recording deadline is reached.
    /// Matching by FriendlyName avoids false positives from lower-quality fallback devices.
    /// </summary>
    private (AudioDeviceInfo? mic, AudioDeviceInfo? render, long recoveredAt)
        WaitForDeviceRecovery(string? originalMicName, string originalRenderName, long deadline)
    {
        const int PollMs           = 500;
        const double RecoveryMaxSec = 60.0;

        long recoveryDeadline = Math.Min(
            Stopwatch.GetTimestamp() + (long)(RecoveryMaxSec * Stopwatch.Frequency),
            deadline);

        int attempt = 0;

        while (Stopwatch.GetTimestamp() < recoveryDeadline)
        {
            Thread.Sleep(PollMs);
            attempt++;

            if (attempt % 4 == 0) // log every ~2s
                Console.WriteLine($"[recovery] Waiting... {attempt * PollMs / 1000.0:F0}s elapsed");

            try
            {
                var captures = DeviceResolver.EnumerateCaptureDevices();
                var renders  = DeviceResolver.EnumerateRenderDevices();

                // Match by exact FriendlyName to avoid accepting lower-quality fallback devices
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
                Console.WriteLine($"[recovery] Device found — mic={foundMic?.FriendlyName ?? "(none)"}  render={foundRender.FriendlyName}");
                return (foundMic, foundRender, recoveredAt);
            }
            catch
            {
                // MMDeviceEnumerator can throw transiently during USB replug — retry on next poll
            }
        }

        Console.Error.WriteLine("[recovery] Timed out waiting for device return.");
        return (null, null, 0);
    }

    private static double RemainingSeconds(long deadline) =>
        (deadline - Stopwatch.GetTimestamp()) / (double)Stopwatch.Frequency;

    /// <summary>
    /// Returns true if the file exists and has more than just a WAV header (>200 bytes).
    /// Does NOT check for silence — a valid-but-silent WAV passes this check.
    /// </summary>
    private static bool HasAudioData(string? path) =>
        path is not null && File.Exists(path) && new FileInfo(path).Length > 200;

    private static void TryDelete(string? path, string label)
    {
        if (path is null) return;
        try
        {
            if (File.Exists(path))
            {
                File.Delete(path);
                Console.WriteLine($"[cleanup] Deleted {label}: {path}");
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] Could not delete {label} ({path}): {ex.Message}");
        }
    }
}
