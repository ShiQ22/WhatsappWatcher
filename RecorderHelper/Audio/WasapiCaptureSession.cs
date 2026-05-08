using NAudio.CoreAudioApi;
using NAudio.Wave;

namespace RecorderHelper.Audio;

/// <summary>
/// Owns one WasapiCapture (mic) and one WasapiLoopbackCapture (render loopback).
/// Each writes raw bytes to its own temp WAV file. No mixing or resampling during capture.
/// Call Start(), wait, then Stop(). Mix happens offline after Stop() returns.
/// </summary>
public sealed class WasapiCaptureSession : IDisposable
{
    private readonly string _micTempPath;
    private readonly string _loopTempPath;
    private readonly bool   _hasMic;

    private MMDevice?               _micDevice;
    private MMDevice?               _renderDevice;
    private WasapiCapture?          _micCapture;
    private WasapiLoopbackCapture?  _loopCapture;
    private WaveFileWriter?         _micWriter;
    private WaveFileWriter?         _loopWriter;

    private readonly ManualResetEventSlim _micStopped  = new(false);
    private readonly ManualResetEventSlim _loopStopped = new(false);

    private long        _micBytesWritten;
    private long        _loopBytesWritten;
    private WaveFormat? _micNativeFormat;
    private WaveFormat? _loopNativeFormat;

    // Null when no mic device was provided.
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

        // --- Loopback capture (always required) ---
        _renderDevice    = enumerator.GetDevice(render.Id);
        _loopCapture     = new WasapiLoopbackCapture(_renderDevice);
        _loopNativeFormat = _loopCapture.WaveFormat;
        _loopWriter      = new WaveFileWriter(_loopTempPath, _loopNativeFormat);

        _loopCapture.DataAvailable += (_, e) =>
        {
            if (e.BytesRecorded > 0)
            {
                _loopWriter?.Write(e.Buffer, 0, e.BytesRecorded);
                _loopBytesWritten += e.BytesRecorded;
            }
        };
        _loopCapture.RecordingStopped += (_, e) =>
        {
            _loopWriter?.Dispose();
            _loopWriter = null;
            if (e.Exception is not null)
                Console.Error.WriteLine($"[WARN] Loopback capture stopped with error: {e.Exception.Message}");
            _loopStopped.Set();
        };

        // --- Mic capture (optional) ---
        if (mic is not null)
        {
            _micDevice       = enumerator.GetDevice(mic.Id);
            _micCapture      = new WasapiCapture(_micDevice, useEventSync: false, audioBufferMillisecondsLength: 100);
            _micNativeFormat = _micCapture.WaveFormat;
            _micWriter       = new WaveFileWriter(_micTempPath, _micNativeFormat);

            _micCapture.DataAvailable += (_, e) =>
            {
                if (e.BytesRecorded > 0)
                {
                    _micWriter?.Write(e.Buffer, 0, e.BytesRecorded);
                    _micBytesWritten += e.BytesRecorded;
                }
            };
            _micCapture.RecordingStopped += (_, e) =>
            {
                _micWriter?.Dispose();
                _micWriter = null;
                if (e.Exception is not null)
                    Console.Error.WriteLine($"[WARN] Mic capture stopped with error: {e.Exception.Message}");
                _micStopped.Set();
            };
        }
        else
        {
            // No mic — signal immediately so Stop() doesn't wait.
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
    /// Writers are flushed and closed before this returns.
    /// </summary>
    public CaptureStats Stop(int timeoutSeconds = 10)
    {
        _micCapture?.StopRecording();
        _loopCapture?.StopRecording();

        if (!_micStopped.Wait(TimeSpan.FromSeconds(timeoutSeconds)))
            Console.Error.WriteLine("[WARN] Mic capture did not stop cleanly within timeout");

        if (!_loopStopped.Wait(TimeSpan.FromSeconds(timeoutSeconds)))
            Console.Error.WriteLine("[WARN] Loopback capture did not stop cleanly within timeout");

        return new CaptureStats(_micBytesWritten, _loopBytesWritten, _micNativeFormat, _loopNativeFormat);
    }

    public void Dispose()
    {
        _micCapture?.Dispose();
        _loopCapture?.Dispose();
        _micWriter?.Dispose();
        _loopWriter?.Dispose();
        _micDevice?.Dispose();
        _renderDevice?.Dispose();
        _micStopped.Dispose();
        _loopStopped.Dispose();
    }
}
