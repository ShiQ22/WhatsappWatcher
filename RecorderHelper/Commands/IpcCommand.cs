using System.Text.Json;
using System.Text.Json.Nodes;
using RecorderHelper.Audio;
using RecorderHelper.Models;

namespace RecorderHelper.Commands;

/// <summary>
/// Implements --ipc mode: reads newline-delimited JSON commands from stdin,
/// emits newline-delimited JSON events to stdout, diagnostics to stderr.
/// stdout is JSON only — never mixed with diagnostic text.
/// </summary>
public static class IpcCommand
{
    private const string Version = "1.0.0";

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    // Guards Console.Out.WriteLine + Flush so events from the session thread
    // and the stdin loop never interleave on stdout.
    private static readonly object StdoutLock = new();

    public static int Run(AppSettings settings)
    {
        // Emit ready immediately — Python waits for this before sending start.
        WriteEvent(new { @event = "ready", version = Version });

        // Shared state — accessed from stdin loop thread and session background thread.
        ManualResetEventSlim? stopSignal  = null;
        ManualResetEventSlim? forceSignal = null;
        Task?                 sessionTask = null;
        bool                  isRecording = false;
        var                   stateLock   = new object();

        foreach (var line in ReadLines())
        {
            if (string.IsNullOrWhiteSpace(line)) continue;

            JsonNode? node;
            try   { node = JsonNode.Parse(line); }
            catch (Exception ex)
            {
                WriteEvent(new { @event = "error", code = "parse_error", message = ex.Message });
                continue;
            }

            string? cmd = node?["cmd"]?.GetValue<string>();
            if (cmd is null)
            {
                WriteEvent(new { @event = "error", code = "missing_cmd", message = "Field 'cmd' is required." });
                continue;
            }

            switch (cmd)
            {
                // ── ping ──────────────────────────────────────────────────────
                case "ping":
                    WriteEvent(new { @event = "pong" });
                    break;

                // ── start ─────────────────────────────────────────────────────
                case "start":
                {
                    bool alreadyRecording;
                    lock (stateLock) alreadyRecording = isRecording;

                    if (alreadyRecording)
                    {
                        WriteEvent(new { @event = "error", code = "already_recording", message = "A recording session is already active." });
                        break;
                    }

                    string? outputDir = node!["output_dir"]?.GetValue<string>();
                    string? baseName  = node["base_name"]?.GetValue<string>();

                    if (string.IsNullOrWhiteSpace(outputDir) || string.IsNullOrWhiteSpace(baseName))
                    {
                        WriteEvent(new { @event = "error", code = "bad_params", message = "output_dir and base_name are required." });
                        break;
                    }

                    float micGain  = TryGetFloat(node, "mic_gain")       ?? settings.MicGain;
                    float loopGain = TryGetFloat(node, "loopback_gain")  ?? settings.LoopbackGain;
                    bool  keepTemp = node["keep_temp"] is { } ktNode && ktNode.GetValue<bool>();

                    // Enumerate and select devices on the stdin thread (fast, <100ms).
                    List<AudioDeviceInfo> captures;
                    List<AudioDeviceInfo> renders;
                    try
                    {
                        captures = DeviceResolver.EnumerateCaptureDevices();
                        renders  = DeviceResolver.EnumerateRenderDevices();
                    }
                    catch (Exception ex)
                    {
                        WriteEvent(new { @event = "error", code = "device_enum_failed", message = ex.Message });
                        break;
                    }

                    var selectedMic    = DeviceResolver.SelectMic(captures, settings);
                    var selectedRender = DeviceResolver.SelectRender(renders, settings);

                    if (selectedRender is null)
                    {
                        WriteEvent(new { @event = "error", code = "no_render_device", message = "No render device found — loopback capture not possible." });
                        break;
                    }

                    try   { Directory.CreateDirectory(outputDir); }
                    catch (Exception ex)
                    {
                        WriteEvent(new { @event = "error", code = "output_dir_error", message = ex.Message });
                        break;
                    }

                    // Create signals and flip state under lock before launching the task.
                    var stop  = new ManualResetEventSlim(false);
                    var force = new ManualResetEventSlim(false);

                    lock (stateLock)
                    {
                        stopSignal  = stop;
                        forceSignal = force;
                        isRecording = true;
                    }

                    // Capture loop variables for the closure.
                    var capMic    = selectedMic;
                    var capRender = selectedRender;
                    var capOutDir = outputDir;
                    var capBase   = baseName;
                    var capMg     = micGain;
                    var capLg     = loopGain;
                    var capKt     = keepTemp;

                    sessionTask = Task.Run(() =>
                    {
                        try
                        {
                            var session = new MultiSegmentSession(settings);
                            session.RunIpc(
                                capMic, capRender, capOutDir, capBase,
                                capKt, capMg, capLg,
                                stop, force,
                                emitJson: WriteJsonLine,
                                diag:     Console.Error.WriteLine);
                        }
                        catch (Exception ex)
                        {
                            WriteEvent(new { @event = "error", code = "session_exception", message = ex.Message });
                        }
                        finally
                        {
                            // Reset state under lock before any I/O so stop/force_stop
                            // handlers see isRecording=false immediately.
                            lock (stateLock)
                            {
                                isRecording = false;
                                stopSignal  = null;
                                forceSignal = null;
                            }
                            stop.Dispose();
                            force.Dispose();
                            // stopped is always the last event for a session.
                            WriteEvent(new { @event = "stopped" });
                        }
                    });
                    break;
                }

                // ── stop ──────────────────────────────────────────────────────
                case "stop":
                {
                    bool shouldError;
                    lock (stateLock)
                    {
                        shouldError = !isRecording || stopSignal is null;
                        if (!shouldError) stopSignal!.Set();
                    }
                    if (shouldError)
                        WriteEvent(new { @event = "error", code = "not_recording", message = "No active recording session." });
                    break;
                }

                // ── force_stop ────────────────────────────────────────────────
                case "force_stop":
                {
                    bool shouldError;
                    lock (stateLock)
                    {
                        shouldError = !isRecording || stopSignal is null;
                        if (!shouldError)
                        {
                            forceSignal?.Set();
                            stopSignal!.Set();
                        }
                    }
                    if (shouldError)
                        WriteEvent(new { @event = "error", code = "not_recording", message = "No active recording session." });
                    break;
                }

                default:
                    WriteEvent(new { @event = "error", code = "unknown_cmd", message = $"Unknown command: '{cmd}'" });
                    break;
            }
        }

        // stdin closed — if a session is still running, stop it gracefully.
        lock (stateLock)
        {
            if (isRecording) stopSignal?.Set();
        }

        sessionTask?.Wait();
        return 0;
    }

    // ── Stdout helpers ───────────────────────────────────────────────────────

    /// <summary>Writes a pre-serialised JSON line to stdout and flushes. Thread-safe.</summary>
    private static void WriteJsonLine(string json)
    {
        lock (StdoutLock)
        {
            Console.Out.WriteLine(json);
            Console.Out.Flush();
        }
    }

    /// <summary>Serialises payload to JSON and writes it to stdout.</summary>
    private static void WriteEvent(object payload) =>
        WriteJsonLine(JsonSerializer.Serialize(payload, JsonOpts));

    // ── Stdin reader ─────────────────────────────────────────────────────────

    private static IEnumerable<string> ReadLines()
    {
        string? line;
        while ((line = Console.In.ReadLine()) is not null)
            yield return line;
    }

    // ── Parsing helpers ──────────────────────────────────────────────────────

    private static float? TryGetFloat(JsonNode node, string key)
    {
        var n = node[key];
        if (n is null) return null;
        try   { return (float)n.GetValue<double>(); }
        catch { return null; }
    }
}
