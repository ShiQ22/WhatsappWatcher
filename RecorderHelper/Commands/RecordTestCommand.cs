using System.Globalization;
using RecorderHelper.Audio;
using RecorderHelper.Models;

namespace RecorderHelper.Commands;

public static class RecordTestCommand
{
    public static int Run(string[] args, AppSettings settings)
    {
        // ── Parse args ──────────────────────────────────────────────────────
        int    seconds   = 15;
        string outputDir = Path.Combine(Path.GetTempPath(), "RecTest");
        bool   keepTemp  = false;
        float  micGain   = settings.MicGain;
        float  loopGain  = settings.LoopbackGain;

        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--seconds" && i + 1 < args.Length && int.TryParse(args[i + 1], out int s))
                seconds = s;
            else if (args[i] == "--output-dir" && i + 1 < args.Length)
                outputDir = args[i + 1];
            else if (args[i] == "--keep-temp")
                keepTemp = true;
            else if (args[i] == "--mic-gain" && i + 1 < args.Length
                && float.TryParse(args[i + 1], NumberStyles.Float, CultureInfo.InvariantCulture, out float mg))
                micGain = mg;
            else if (args[i] == "--loopback-gain" && i + 1 < args.Length
                && float.TryParse(args[i + 1], NumberStyles.Float, CultureInfo.InvariantCulture, out float lg))
                loopGain = lg;
        }

        Console.WriteLine($"[record-test] seconds={seconds}  output-dir={outputDir}  keep-temp={keepTemp}");
        Console.WriteLine($"[record-test] mic_gain={micGain:F2}  loopback_gain={loopGain:F2}");

        // ── Enumerate and select devices ─────────────────────────────────────
        List<AudioDeviceInfo> captureDevices;
        List<AudioDeviceInfo> renderDevices;

        try
        {
            captureDevices = DeviceResolver.EnumerateCaptureDevices();
            renderDevices  = DeviceResolver.EnumerateRenderDevices();
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[ERROR] Device enumeration failed: {ex.Message}");
            return 1;
        }

        var selectedMic    = DeviceResolver.SelectMic(captureDevices, settings);
        var selectedRender = DeviceResolver.SelectRender(renderDevices, settings);

        if (selectedRender is null)
        {
            Console.Error.WriteLine("[ERROR] No render device found — loopback capture not possible. Aborting.");
            return 1;
        }

        Console.WriteLine($"[record-test] Selected mic:    {selectedMic?.FriendlyName ?? "(none — loopback only)"}");
        Console.WriteLine($"[record-test] Selected render: {selectedRender.FriendlyName}");
        Console.WriteLine($"[record-test] Output dir:      {outputDir}");

        if (selectedMic is null)
            Console.Error.WriteLine("[WARN] No mic selected — output will contain remote audio only.");

        Directory.CreateDirectory(outputDir);

        // ── Run multi-segment session ────────────────────────────────────────
        var session = new MultiSegmentSession(settings);
        return session.Run(
            mic:             selectedMic,
            render:          selectedRender,
            outputDir:       outputDir,
            baseName:        "test",
            durationSeconds: seconds,
            keepTemp:        keepTemp,
            micGain:         micGain,
            loopGain:        loopGain);
    }
}
