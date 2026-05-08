using RecorderHelper.Audio;
using RecorderHelper.Models;

namespace RecorderHelper.Commands;

public static class RecordTestCommand
{
    public static int Run(string[] args, AppSettings settings)
    {
        // --- Parse args ---
        int    seconds   = 15;
        string outputDir = Path.Combine(Path.GetTempPath(), "RecTest");
        bool   keepTemp  = false;

        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--seconds" && i + 1 < args.Length && int.TryParse(args[i + 1], out int s))
                seconds = s;
            else if (args[i] == "--output-dir" && i + 1 < args.Length)
                outputDir = args[i + 1];
            else if (args[i] == "--keep-temp")
                keepTemp = true;
        }

        Console.WriteLine($"[record-test] seconds={seconds}  output-dir={outputDir}  keep-temp={keepTemp}");

        // --- Enumerate and select devices ---
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
            Console.Error.WriteLine("[ERROR] No render device found — loopback capture is not possible. Aborting.");
            return 1;
        }

        Console.WriteLine($"[record-test] Selected mic:    {selectedMic?.FriendlyName ?? "(none — loopback only)"}");
        Console.WriteLine($"[record-test] Selected render: {selectedRender.FriendlyName}");

        if (selectedMic is null)
            Console.Error.WriteLine("[WARN] No mic selected — output will contain remote audio only");

        // --- Resolve output paths up front so they are always printed ---
        Directory.CreateDirectory(outputDir);

        const string BaseName = "test";
        string micTempPath  = Path.Combine(outputDir, $"{BaseName}_mic.wav");
        string loopTempPath = Path.Combine(outputDir, $"{BaseName}_loopback.wav");
        string mixedPath    = Path.Combine(outputDir, $"{BaseName}_seg1.wav");

        Console.WriteLine();
        Console.WriteLine($"[record-test] Mic temp:      {micTempPath}");
        Console.WriteLine($"[record-test] Loopback temp: {loopTempPath}");
        Console.WriteLine($"[record-test] Mixed output:  {mixedPath}");
        Console.WriteLine();

        // --- Capture ---
        Console.WriteLine($"[record-test] Recording for {seconds} seconds...");

        CaptureStats stats;

        try
        {
            using var session = new WasapiCaptureSession(selectedMic, selectedRender, outputDir, BaseName);
            session.Start();
            Thread.Sleep(seconds * 1000);
            stats = session.Stop();
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[ERROR] Capture failed: {ex.Message}");
            Console.Error.WriteLine("Temp files preserved (if any data was written):");
            Console.Error.WriteLine($"  mic temp:      {micTempPath}");
            Console.Error.WriteLine($"  loopback temp: {loopTempPath}");
            return 1;
        }

        Console.WriteLine();
        Console.WriteLine($"[capture]  mic bytes:      {stats.MicBytesWritten:N0}");
        Console.WriteLine($"[capture]  loopback bytes: {stats.LoopbackBytesWritten:N0}");

        if (stats.MicNativeFormat is not null)
            Console.WriteLine($"[capture]  mic format:     {stats.MicNativeFormat}");
        if (stats.LoopbackNativeFormat is not null)
            Console.WriteLine($"[capture]  loopback format:{stats.LoopbackNativeFormat}");

        // --- Validate capture output ---
        bool loopEmpty = !File.Exists(loopTempPath) || new FileInfo(loopTempPath).Length == 0;
        bool micEmpty  = selectedMic is null
                      || !File.Exists(micTempPath)
                      || new FileInfo(micTempPath).Length == 0;

        if (loopEmpty)
        {
            Console.Error.WriteLine("[ERROR] Loopback capture produced no data.");
            Console.Error.WriteLine("Temp files preserved:");
            Console.Error.WriteLine($"  mic temp:      {micTempPath}");
            Console.Error.WriteLine($"  loopback temp: {loopTempPath}");
            return 1;
        }

        if (micEmpty && selectedMic is not null)
            Console.Error.WriteLine("[WARN] Mic capture produced no data — output will contain remote audio only");

        // --- Mix ---
        Console.WriteLine();
        Console.WriteLine("[mix] Normalizing and mixing...");

        MixResult result;

        try
        {
            result = WavMixer.Mix(
                micPath:      micEmpty ? null : micTempPath,
                loopbackPath: loopTempPath,
                outputPath:   mixedPath,
                micGain:      0.75f,
                loopGain:     0.65f);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[ERROR] Mix failed: {ex.Message}");
            Console.Error.WriteLine("Temp files preserved:");
            Console.Error.WriteLine($"  mic temp:      {micTempPath}");
            Console.Error.WriteLine($"  loopback temp: {loopTempPath}");
            return 1;
        }

        if (!result.Success)
        {
            Console.Error.WriteLine("[ERROR] Mix reported failure. Temp files preserved:");
            Console.Error.WriteLine($"  mic temp:      {micTempPath}");
            Console.Error.WriteLine($"  loopback temp: {loopTempPath}");
            return 1;
        }

        long mixedSize = File.Exists(mixedPath) ? new FileInfo(mixedPath).Length : 0;

        Console.WriteLine();
        Console.WriteLine("--- Output ---");
        Console.WriteLine($"  Mixed output:  {mixedPath}");
        Console.WriteLine($"  Size:          {mixedSize:N0} bytes");
        Console.WriteLine($"  Duration:      ~{result.DurationSeconds:F1}s");
        Console.WriteLine();

        // --- Cleanup ---
        if (keepTemp)
        {
            Console.WriteLine("Temp files kept (--keep-temp):");
            if (selectedMic is not null)
                Console.WriteLine($"  {micTempPath}");
            Console.WriteLine($"  {loopTempPath}");
        }
        else
        {
            if (!micEmpty)
                TryDelete(micTempPath,  "mic temp");
            TryDelete(loopTempPath, "loopback temp");
        }

        return 0;
    }

    private static void TryDelete(string path, string label)
    {
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
