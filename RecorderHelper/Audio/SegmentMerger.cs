using NAudio.Wave;

namespace RecorderHelper.Audio;

/// <summary>
/// Concatenates a list of same-format WAV files into one output file.
/// All inputs must be 48000 Hz / 1ch / int16 (WavMixer + SilenceGenerator output).
/// </summary>
public static class SegmentMerger
{
    private const int BufferSize = 65536;

    public static bool Merge(IReadOnlyList<string> wavPaths, string outputPath)
    {
        if (wavPaths.Count == 0)
        {
            Console.Error.WriteLine("[merge] No input WAVs provided.");
            return false;
        }

        string first = wavPaths[0];
        if (!File.Exists(first))
        {
            Console.Error.WriteLine($"[merge] First WAV not found: {first}");
            return false;
        }

        WaveFormat expectedFormat;
        try
        {
            using var probe = new WaveFileReader(first);
            expectedFormat = probe.WaveFormat;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[merge] Cannot read format from {first}: {ex.Message}");
            return false;
        }

        // Single file: copy (avoids a re-encode round-trip)
        if (wavPaths.Count == 1)
        {
            try
            {
                File.Copy(first, outputPath, overwrite: true);
                Console.WriteLine($"[merge] Single segment — copied to {outputPath}");
                return true;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[merge] Copy failed: {ex.Message}");
                return false;
            }
        }

        try
        {
            byte[] buf = new byte[BufferSize];
            using var writer = new WaveFileWriter(outputPath, expectedFormat);

            foreach (var path in wavPaths)
            {
                if (!File.Exists(path))
                {
                    Console.Error.WriteLine($"[merge] Skipping missing file: {path}");
                    continue;
                }

                using var reader = new WaveFileReader(path);

                if (reader.WaveFormat.SampleRate    != expectedFormat.SampleRate  ||
                    reader.WaveFormat.Channels      != expectedFormat.Channels    ||
                    reader.WaveFormat.BitsPerSample != expectedFormat.BitsPerSample)
                {
                    Console.Error.WriteLine($"[merge] Format mismatch — skipping: {path}");
                    continue;
                }

                int n;
                while ((n = reader.Read(buf, 0, buf.Length)) > 0)
                    writer.Write(buf, 0, n);

                Console.WriteLine($"[merge]   + {path}");
            }

            Console.WriteLine($"[merge]   → {outputPath}");
            return true;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[merge] Failed: {ex.Message}");
            return false;
        }
    }
}
