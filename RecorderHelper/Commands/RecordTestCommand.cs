using RecorderHelper.Models;

namespace RecorderHelper.Commands;

// RH-1 placeholder. Real capture implemented in RH-2.
public static class RecordTestCommand
{
    public static int Run(string[] args, AppSettings settings)
    {
        int seconds = 15;
        string outputDir = Path.Combine(Path.GetTempPath(), "RecTest");

        for (int i = 0; i < args.Length - 1; i++)
        {
            if (args[i] == "--seconds" && int.TryParse(args[i + 1], out int s))
                seconds = s;
            else if (args[i] == "--output-dir")
                outputDir = args[i + 1];
        }

        Console.WriteLine($"[record-test] seconds={seconds} output-dir={outputDir}");
        Console.WriteLine("[record-test] TODO: real capture not yet implemented (Phase RH-2)");
        Console.WriteLine($"[record-test] Will use: mic_patterns=[{string.Join(", ", settings.MicNamePatterns)}]  sample_rate={settings.SampleRate}  channels={settings.Channels}");
        return 0;
    }
}
