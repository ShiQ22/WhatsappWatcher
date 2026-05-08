using NAudio.CoreAudioApi;
using RecorderHelper.Models;

namespace RecorderHelper.Audio;

public record AudioDeviceInfo(int Index, string Id, string FriendlyName, bool IsDefaultComms);

public static class DeviceResolver
{
    public static List<AudioDeviceInfo> EnumerateCaptureDevices()
    {
        var enumerator = new MMDeviceEnumerator();
        var defaultCommsId = TryGetDefaultDeviceId(enumerator, DataFlow.Capture);

        var devices = enumerator
            .EnumerateAudioEndPoints(DataFlow.Capture, DeviceState.Active)
            .ToList();

        var result = new List<AudioDeviceInfo>(devices.Count);
        for (int i = 0; i < devices.Count; i++)
        {
            var d = devices[i];
            result.Add(new AudioDeviceInfo(i, d.ID, d.FriendlyName, d.ID == defaultCommsId));
        }
        return result;
    }

    public static List<AudioDeviceInfo> EnumerateRenderDevices()
    {
        var enumerator = new MMDeviceEnumerator();
        var defaultCommsId = TryGetDefaultDeviceId(enumerator, DataFlow.Render);

        var devices = enumerator
            .EnumerateAudioEndPoints(DataFlow.Render, DeviceState.Active)
            .ToList();

        var result = new List<AudioDeviceInfo>(devices.Count);
        for (int i = 0; i < devices.Count; i++)
        {
            var d = devices[i];
            result.Add(new AudioDeviceInfo(i, d.ID, d.FriendlyName, d.ID == defaultCommsId));
        }
        return result;
    }

    public static AudioDeviceInfo? SelectMic(List<AudioDeviceInfo> captureDevices, AppSettings settings)
    {
        var scored = captureDevices
            .Where(d => !IsLoopbackDevice(d.FriendlyName))
            .Select(d => (device: d, score: ScoreMic(d, settings)))
            .OrderByDescending(x => x.score)
            .ToList();

        if (scored.Count == 0)
            return null;

        var best = scored[0];

        // If builtin fallback is disabled, require at least one name-pattern match
        if (!settings.AllowBuiltinMicFallback)
        {
            bool hasPatternMatch = settings.MicNamePatterns.Any(p =>
                best.device.FriendlyName.Contains(p, StringComparison.OrdinalIgnoreCase));
            if (!hasPatternMatch && !best.device.IsDefaultComms)
                return null;
        }

        return best.device;
    }

    public static AudioDeviceInfo? SelectRender(List<AudioDeviceInfo> renderDevices, AppSettings settings)
    {
        var scored = renderDevices
            .Select(d => (device: d, score: ScoreRender(d, settings)))
            .OrderByDescending(x => x.score)
            .ToList();

        return scored.Count > 0 ? scored[0].device : null;
    }

    private static int ScoreMic(AudioDeviceInfo device, AppSettings settings)
    {
        int score = 0;
        if (settings.PreferDefaultCommunicationsDevice && device.IsDefaultComms)
            score += 10;
        foreach (var pattern in settings.MicNamePatterns)
            if (device.FriendlyName.Contains(pattern, StringComparison.OrdinalIgnoreCase))
                score += 5;
        return score;
    }

    private static int ScoreRender(AudioDeviceInfo device, AppSettings settings)
    {
        int score = 0;
        if (settings.PreferDefaultCommunicationsDevice && device.IsDefaultComms)
            score += 10;
        foreach (var pattern in settings.RenderNamePatterns)
            if (device.FriendlyName.Contains(pattern, StringComparison.OrdinalIgnoreCase))
                score += 5;
        return score;
    }

    private static bool IsLoopbackDevice(string friendlyName) =>
        friendlyName.Contains("[Loopback]", StringComparison.OrdinalIgnoreCase);

    private static string? TryGetDefaultDeviceId(MMDeviceEnumerator enumerator, DataFlow flow)
    {
        try
        {
            return enumerator.GetDefaultAudioEndpoint(flow, Role.Communications).ID;
        }
        catch
        {
            return null;
        }
    }
}
