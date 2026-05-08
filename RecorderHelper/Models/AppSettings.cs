using System.Text.Json.Serialization;

namespace RecorderHelper.Models;

public class AppSettings
{
    [JsonPropertyName("mic_name_patterns")]
    public string[] MicNamePatterns { get; set; } = ["USB", "Headset", "Microphone"];

    [JsonPropertyName("render_name_patterns")]
    public string[] RenderNamePatterns { get; set; } = ["USB", "Headset", "Speakers", "Headphones"];

    [JsonPropertyName("prefer_default_communications_device")]
    public bool PreferDefaultCommunicationsDevice { get; set; } = true;

    [JsonPropertyName("allow_builtin_mic_fallback")]
    public bool AllowBuiltinMicFallback { get; set; } = true;

    [JsonPropertyName("sample_rate")]
    public int SampleRate { get; set; } = 48000;

    [JsonPropertyName("channels")]
    public int Channels { get; set; } = 1;

    [JsonPropertyName("preserve_usb_gap_silence")]
    public bool PreserveUsbGapSilence { get; set; } = true;

    [JsonPropertyName("gap_silence_threshold_seconds")]
    public double GapSilenceThresholdSeconds { get; set; } = 0.5;

    [JsonPropertyName("keep_temp_segments")]
    public bool KeepTempSegments { get; set; } = false;

    [JsonPropertyName("startup_timeout_seconds")]
    public double StartupTimeoutSeconds { get; set; } = 5.0;

    [JsonPropertyName("stop_timeout_seconds")]
    public double StopTimeoutSeconds { get; set; } = 60.0;

    [JsonPropertyName("ping_interval_seconds")]
    public double PingIntervalSeconds { get; set; } = 5.0;
}
