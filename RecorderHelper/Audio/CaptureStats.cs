using NAudio.Wave;

namespace RecorderHelper.Audio;

public record CaptureStats(
    long MicBytesWritten,
    long LoopbackBytesWritten,
    WaveFormat? MicNativeFormat,
    WaveFormat? LoopbackNativeFormat
);
