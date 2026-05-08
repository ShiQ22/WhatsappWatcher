using NAudio.Wave;
using NAudio.Wave.SampleProviders;

namespace RecorderHelper.Audio;

/// <summary>
/// Normalizes a WaveFileReader to float32 / 48000 Hz / mono ISampleProvider.
/// The caller owns the WaveFileReader lifetime and must dispose it after the provider is consumed.
/// </summary>
public static class PcmNormalizer
{
    public const int TargetSampleRate = 48000;

    public static ISampleProvider Normalize(WaveFileReader reader)
    {
        ISampleProvider samples = reader.ToSampleProvider();

        // Convert to mono
        int channels = samples.WaveFormat.Channels;
        if (channels == 2)
        {
            samples = new StereoToMonoSampleProvider(samples);
        }
        else if (channels != 1)
        {
            // More than 2 channels is unusual for WASAPI shared mode,
            // but handle it by taking only the left/first channel pair.
            Console.Error.WriteLine($"[WARN] Unexpected channel count {channels} — reducing to mono via stereo path may fail; proceeding.");
        }

        // Resample if needed
        if (samples.WaveFormat.SampleRate != TargetSampleRate)
            samples = new WdlResamplingSampleProvider(samples, TargetSampleRate);

        return samples;
    }
}
