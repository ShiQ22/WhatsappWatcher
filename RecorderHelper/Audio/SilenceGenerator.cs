using NAudio.Wave;

namespace RecorderHelper.Audio;

public static class SilenceGenerator
{
    /// <summary>
    /// Writes a silence WAV of the requested duration.
    /// Format: 48000 Hz / 1 channel / int16 — same as WavMixer output.
    /// </summary>
    public static void Generate(string path, double durationSeconds, int sampleRate = 48000)
    {
        int sampleCount = Math.Max(1, (int)(durationSeconds * sampleRate));
        var format = new WaveFormat(sampleRate, 16, 1);
        using var writer = new WaveFileWriter(path, format);
        // int16 silence = all-zero bytes
        byte[] silence = new byte[sampleCount * 2];
        writer.Write(silence, 0, silence.Length);
    }
}
