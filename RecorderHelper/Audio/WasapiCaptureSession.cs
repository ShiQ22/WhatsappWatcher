using NAudio.CoreAudioApi;
using NAudio.Wave;

namespace RecorderHelper.Audio;

/// <summary>
/// Captures one segment: mic (optional) and loopback into separate raw temp WAVs.
/// Thread-safe: DataAvailable writes and RecordingStopped disposes are guarded by per-writer locks.
/// Stop() and Dispose() are idempotent.
/// </summary>
public sealed class WasapiCaptureSession : IDisposable
{
    private readonly string _micTempPath;
    private readonly string _loopTempPath;
    private readonly bool   _hasMic;

    private MMDevice?              _micDevice;
    private MMDevice?              _renderDevice;
    private WasapiCapture?         _micCapture;
    private WasapiLoopbackCapture? _loopCapture;

    private WaveFileWriter? _micWriter;
    private WaveFileWriter? _loopWriter;

    // Per-writer locks guard both Write() in DataAvailable and Dispose()/null in RecordingStopped.
    private readonly object _micLock  = new();
    private readonly object _loopLock = new();

    private readonly ManualResetEventSlim _micStopped  = new(false);
    private readonly ManualResetEventSlim _loopStopped = new(false);

    private long        _micBytesWritten;
    private long        _loopBytesWritten;
    private WaveFormat? _micNativeFormat;
    private WaveFormat? _loopNativeFormat;

    // Set to true before StopRecording() to distinguish intentional stop from device loss.
    private bool _stopRequested;
    // Interlocked flag — ensures StopCapture() and Stop() are called at most once.
    private int  _stopCalled;
    private bool _deviceLostDuringCapture;
    private bool _disposed;

    /// <summary>Signalled when either capture stops due to device loss (not due to Stop()).</summary>
    public ManualResetEventSlim AnyDeviceLost { get; } = new(false);

    public bool    DeviceLostDuringCapture => _deviceLostDuringCapture;
    public string? MicTempPath  => _hasMic ? _micTempPath : null;
    public string  LoopTempPath => _loopTempPath;

    public WasapiCaptureSession(
        AudioDeviceInfo? mic,
        AudioDeviceInfo  render,
        string           outputDir,
        string           baseName)
    {
        _hasMic       = mic is not null;
        _micTempPath  = Path.Combine(outputDir, $"{baseName}_mic.wav");
        _loopTempPath = Path.Combine(outputDir, $"{baseName}_loopback.wav");

        Directory.CreateDirectory(outputDir);

        var enumerator = new MMDeviceEnumerator();

        // ── Loopback capture (always required) ──────────────────────────────
        _renderDevice     = enumerator.GetDevice(render.Id);
        _loopCapture      = new WasapiLoopbackCapture(_renderDevice);
        _loopNativeFormat = _loopCapture.WaveFormat;
        _loopWriter       = new WaveFileWriter(_loopTempPath, _loopNativeFormat);

        _loopCapture.DataAvailable += (_, e) =>
        {
            if (e.BytesRecorded > 0)
            {
                lock (_loopLock)
                {
                    _loopWriter?.Write(e.Buffer, 0, e.BytesRecorded);
                    _loopBytesWritten += e.BytesRecorded;
                }
            }
        };

        _loopCapture.RecordingStopped += (_, e) =>
        {
            lock (_loopLock)
            {
                _loopWriter?.Dispose();
                _loopWriter = null;
            }
            if (e.Exception is not null && !_stopRequested)
            {
                Console.Error.WriteLine($"[WARN] Loopback device lost: {e.Exception.Message}");
                _deviceLostDuringCapture = true;
                AnyDeviceLost.Set();
            }
            _loopStopped.Set();
        };

        // ── Mic capture (optional) ───────────────────────────────────────────
        if (mic is not null)
        {
            _micDevice       = enumerator.GetDevice(mic.Id);
            _micCapture      = new WasapiCapture(_micDevice,
                useEventSync: false, audioBufferMillisecondsLength: 100);
            _micNativeFormat = _micCapture.WaveFormat;
            _micWriter       = new WaveFileWriter(_micTempPath, _micNativeFormat);

            _micCapture.DataAvailable += (_, e) =>
            {
                if (e.BytesRecorded > 0)
                {
                    lock (_micLock)
                    {
                        _micWriter?.Write(e.Buffer, 0, e.BytesRecorded);
                        _micBytesWritten += e.BytesRecorded;
                    }
                }
            };

            _micCapture.RecordingStopped += (_, e) =>
            {
                lock (_micLock)
                {
                    _micWriter?.Dispose();
                    _micWriter = null;
                }
                if (e.Exception is not null && !_stopRequested)
                {
                    Console.Error.WriteLine($"[WARN] Mic device lost: {e.Exception.Message}");
                    _deviceLostDuringCapture = true;
                    AnyDeviceLost.Set();
                }
                _micStopped.Set();
            };
        }
        else
        {
            // No mic selected — mark as already stopped so Stop() doesn't wait for it.
            _micStopped.Set();
        }
    }

    public void Start()
    {
        _loopCapture!.StartRecording();
        _micCapture?.StartRecording();
    }

    /// <summary>
    /// Signals both captures to stop and waits for their RecordingStopped events.
    /// Idempotent: safe to call multiple times or concurrently.
    /// </summary>
    public CaptureStats Stop(int timeoutSeconds = 10)
    {
        StopCapture();

        if (!_micStopped.Wait(TimeSpan.FromSeconds(timeoutSeconds)))
            Console.Error.WriteLine("[WARN] Mic did not stop cleanly within timeout.");
        if (!_loopStopped.Wait(TimeSpan.FromSeconds(timeoutSeconds)))
            Console.Error.WriteLine("[WARN] Loopback did not stop cleanly within timeout.");

        return new CaptureStats(_micBytesWritten, _loopBytesWritten, _micNativeFormat, _loopNativeFormat);
    }

    /// <summary>
    /// Idempotent. Ensures captures are stopped and all resources are released.
    /// Always safe to call even if Stop() was not called first.
    /// </summary>
    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;

        // Ensure captures are signalled to stop (no-op if already done).
        StopCapture();

        // Wait for RecordingStopped handlers to flush and close the writers.
        try { _micStopped.Wait(TimeSpan.FromSeconds(3)); }  catch { }
        try { _loopStopped.Wait(TimeSpan.FromSeconds(3)); } catch { }

        // Safety net: dispose writers if handlers didn't (e.g., timeout above).
        lock (_micLock)  { _micWriter?.Dispose();  _micWriter  = null; }
        lock (_loopLock) { _loopWriter?.Dispose(); _loopWriter = null; }

        _micCapture?.Dispose();
        _loopCapture?.Dispose();
        _micDevice?.Dispose();
        _renderDevice?.Dispose();

        // Dispose signalling objects last — handlers may reference them until the waits above complete.
        AnyDeviceLost.Dispose();
        _micStopped.Dispose();
        _loopStopped.Dispose();
    }

    private void StopCapture()
    {
        // Interlocked ensures exactly one thread executes the stop logic.
        if (Interlocked.Exchange(ref _stopCalled, 1) == 0)
        {
            _stopRequested = true;
            _micCapture?.StopRecording();
            _loopCapture?.StopRecording();
        }
    }
}
