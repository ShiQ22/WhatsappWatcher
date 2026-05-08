using System.Buffers.Binary;
using NAudio.Wave;

namespace RecorderHelper.Audio;

public record MixResult(bool Success, long OutputBytes, double DurationSeconds, long ClippedSamples = 0);

/// <summary>
/// Offline mixer: reads two normalized ISampleProviders and writes a single int16 mono WAV.
/// Both providers must already be float32 / 48000 Hz / mono (output of PcmNormalizer).
/// </summary>
public static class WavMixer
{
    // 100ms block at 48000 Hz mono
    private const int BlockSizeSamples = 4800;

    /// <param name="micPath">Path to mic temp WAV, or null if no mic was captured.</param>
    /// <param name="loopbackPath">Path to loopback temp WAV. Must exist and be non-empty.</param>
    /// <param name="outputPath">Destination mixed WAV path.</param>
    /// <param name="micGain">Mic amplitude scale.</param>
    /// <param name="loopGain">Loopback amplitude scale.</param>
    public static MixResult Mix(
        string? micPath,
        string  loopbackPath,
        string  outputPath,
        float   micGain  = 2.0f,
        float   loopGain = 0.55f)
    {
        if (!File.Exists(loopbackPath) || new FileInfo(loopbackPath).Length == 0)
        {
            Console.Error.WriteLine($"[ERROR] Loopback file missing or empty: {loopbackPath}");
            return new MixResult(false, 0, 0);
        }

        WaveFileReader? micReader  = null;
        WaveFileReader  loopReader = new WaveFileReader(loopbackPath);

        try
        {
            ISampleProvider? micProvider  = null;
            ISampleProvider  loopProvider = PcmNormalizer.Normalize(loopReader);

            if (micPath is not null && File.Exists(micPath) && new FileInfo(micPath).Length > 0)
            {
                micReader   = new WaveFileReader(micPath);
                micProvider = PcmNormalizer.Normalize(micReader);
            }

            var (bytesWritten, clippedSamples) =
                WriteBlocks(micProvider, micGain, loopProvider, loopGain, outputPath);

            if (bytesWritten == 0)
            {
                Console.Error.WriteLine("[ERROR] Mix produced zero output bytes.");
                return new MixResult(false, 0, 0);
            }

            // 48000 Hz / 1ch / 16-bit = 2 bytes per sample
            double duration = (double)bytesWritten / (TargetSampleRate * 2);
            return new MixResult(true, bytesWritten, duration, clippedSamples);
        }
        finally
        {
            micReader?.Dispose();
            loopReader.Dispose();
        }
    }

    private const int TargetSampleRate = PcmNormalizer.TargetSampleRate;

    private static (long bytesWritten, long clippedSamples) WriteBlocks(
        ISampleProvider? micProvider,  float micGain,
        ISampleProvider  loopProvider, float loopGain,
        string outputPath)
    {
        var outFormat = new WaveFormat(TargetSampleRate, 16, 1);
        using var writer = new WaveFileWriter(outputPath, outFormat);

        float[] micBuf  = new float[BlockSizeSamples];
        float[] loopBuf = new float[BlockSizeSamples];
        byte[]  outBuf  = new byte[BlockSizeSamples * 2];
        long    total   = 0;
        long    clipped = 0;

        while (true)
        {
            int micRead  = micProvider?.Read(micBuf,  0, BlockSizeSamples) ?? 0;
            int loopRead = loopProvider.Read(loopBuf, 0, BlockSizeSamples);
            int toWrite  = Math.Max(micRead, loopRead);

            if (toWrite == 0) break;

            for (int i = 0; i < toWrite; i++)
            {
                float m     = i < micRead  ? micBuf[i]  * micGain  : 0f;
                float l     = i < loopRead ? loopBuf[i] * loopGain : 0f;
                float mixed = m + l;

                if (mixed > 1f || mixed < -1f) clipped++;

                short s = (short)(Math.Clamp(mixed, -1f, 1f) * short.MaxValue);
                BinaryPrimitives.WriteInt16LittleEndian(outBuf.AsSpan(i * 2), s);
            }

            writer.Write(outBuf, 0, toWrite * 2);
            total += toWrite * 2;
        }

        return (total, clipped);
    }
}
