using System.Text.Json;
using System.Text.Json.Serialization;
using RecorderHelper.Commands;
using RecorderHelper.Models;

var settings = LoadSettings();

if (args.Length == 0)
{
    PrintUsage();
    return 1;
}

return args[0] switch
{
    "--list-devices" => ListDevicesCommand.Run(settings),
    "--record-test"  => RecordTestCommand.Run(args, settings),
    _                => UnknownCommand(args[0])
};

static AppSettings LoadSettings()
{
    var exeDir = AppContext.BaseDirectory;
    var path = Path.Combine(exeDir, "appsettings.json");

    if (!File.Exists(path))
        return new AppSettings();

    try
    {
        var json = File.ReadAllText(path);
        var doc = JsonDocument.Parse(json);
        if (!doc.RootElement.TryGetProperty("recorder_helper", out var section))
            return new AppSettings();

        var opts = new JsonSerializerOptions { PropertyNameCaseInsensitive = true };
        return JsonSerializer.Deserialize<AppSettings>(section.GetRawText(), opts) ?? new AppSettings();
    }
    catch (Exception ex)
    {
        Console.Error.WriteLine($"[WARN] Failed to load appsettings.json: {ex.Message} — using defaults");
        return new AppSettings();
    }
}

static void PrintUsage()
{
    Console.WriteLine("Usage:");
    Console.WriteLine("  RecorderHelper.exe --list-devices");
    Console.WriteLine("  RecorderHelper.exe --record-test [--seconds N] [--output-dir PATH]");
}

static int UnknownCommand(string cmd)
{
    Console.Error.WriteLine($"[ERROR] Unknown command: {cmd}");
    PrintUsage();
    return 1;
}
