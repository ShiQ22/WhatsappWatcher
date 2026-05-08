using RecorderHelper.Audio;
using RecorderHelper.Models;

namespace RecorderHelper.Commands;

public static class ListDevicesCommand
{
    public static int Run(AppSettings settings)
    {
        List<AudioDeviceInfo> captureDevices;
        List<AudioDeviceInfo> renderDevices;

        try
        {
            captureDevices = DeviceResolver.EnumerateCaptureDevices();
            renderDevices = DeviceResolver.EnumerateRenderDevices();
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[ERROR] Failed to enumerate audio devices: {ex.Message}");
            return 1;
        }

        Console.WriteLine("Capture devices:");
        if (captureDevices.Count == 0)
        {
            Console.WriteLine("  (none found)");
        }
        else
        {
            foreach (var d in captureDevices)
            {
                var tag = d.IsDefaultComms ? " — default-communications" : "";
                Console.WriteLine($"  [{d.Index}] {d.FriendlyName}{tag}");
            }
        }

        Console.WriteLine("Render devices:");
        if (renderDevices.Count == 0)
        {
            Console.WriteLine("  (none found)");
        }
        else
        {
            foreach (var d in renderDevices)
            {
                var tag = d.IsDefaultComms ? " — default-communications" : "";
                Console.WriteLine($"  [{d.Index}] {d.FriendlyName}{tag}");
            }
        }

        var selectedMic = DeviceResolver.SelectMic(captureDevices, settings);
        var selectedRender = DeviceResolver.SelectRender(renderDevices, settings);

        Console.WriteLine($"Selected mic:    {selectedMic?.FriendlyName ?? "(none)"}");
        Console.WriteLine($"Selected render: {selectedRender?.FriendlyName ?? "(none)"}");

        if (selectedRender is null)
        {
            Console.Error.WriteLine("[ERROR] No render device found — loopback capture will not be possible.");
            return 1;
        }

        return 0;
    }
}
