namespace RecorderHelper.Audio;

public record SegmentInfo(
    int          Index,
    string?      MicTempPath,
    string       LoopTempPath,
    string       MixedPath,
    CaptureStats Stats
);
