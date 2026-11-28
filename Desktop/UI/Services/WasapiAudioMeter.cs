using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;

namespace OmegaDesktop.Services;

/// <summary>
/// Lightweight, zero-dependency Windows Core Audio (WASAPI) playback meter.
/// Reads real-time audio peak levels specifically from Python (Gooya/Gemini TTS)
/// playback sessions so the 3D sphere pulses in exact sync with spoken syllables.
/// </summary>
public sealed class WasapiAudioMeter : IDisposable
{
    private const int CLSCTX_ALL = 23;
    private const int eRender = 0;
    private const int eMultimedia = 1;
    private const int AudioSessionStateActive = 1;

    private static readonly Guid CLSID_MMDeviceEnumerator = new("BCDE0395-E52F-467C-8E3D-C4579291692E");
    private static readonly Guid IID_IAudioSessionManager2 = new("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F");

    private IMMDeviceEnumerator? _enumerator;
    private IMMDevice? _device;
    private IAudioSessionManager2? _sessionManager;
    private readonly List<IAudioMeterInformation> _pythonMeters = new();
    private DateTime _lastRescanUtc = DateTime.MinValue;
    private bool _disposed;

    public WasapiAudioMeter()
    {
        TryInitDevice();
    }

    private void TryInitDevice()
    {
        ReleaseDevice();
        try
        {
            var enumType = Type.GetTypeFromCLSID(CLSID_MMDeviceEnumerator);
            if (enumType == null) return;
            _enumerator = Activator.CreateInstance(enumType) as IMMDeviceEnumerator;
            if (_enumerator == null) return;

            int hr = _enumerator.GetDefaultAudioEndpoint(eRender, eMultimedia, out _device);
            if (hr != 0 || _device == null) return;

            Guid iid = IID_IAudioSessionManager2;
            hr = _device.Activate(ref iid, CLSCTX_ALL, IntPtr.Zero, out object mgrObj);
            if (hr == 0)
            {
                _sessionManager = mgrObj as IAudioSessionManager2;
            }
        }
        catch
        {
            ReleaseDevice();
        }
    }

    /// <summary>
    /// Returns the real-time peak amplitude (0.0 to 1.0) of active Python TTS audio playback.
    /// Returns 0.0 when Python is silent (including while synthesizing before audio starts).
    /// </summary>
    public double GetPythonPlaybackPeak()
    {
        if (_disposed) return 0.0;

        try
        {
            var now = DateTime.UtcNow;
            if (_sessionManager == null)
            {
                if ((now - _lastRescanUtc).TotalSeconds < 2.0) return 0.0;
                _lastRescanUtc = now;
                TryInitDevice();
                if (_sessionManager == null) return 0.0;
            }

            // Rescan sessions periodically (every 350ms when idle, 1200ms when active)
            double rescanInterval = _pythonMeters.Count == 0 ? 0.35 : 1.2;
            if ((now - _lastRescanUtc).TotalSeconds >= rescanInterval)
            {
                _lastRescanUtc = now;
                RefreshPythonSessions();
            }

            float maxPeak = 0.0f;
            for (int i = 0; i < _pythonMeters.Count; i++)
            {
                try
                {
                    if (_pythonMeters[i].GetPeakValue(out float p) == 0 && p > maxPeak)
                    {
                        maxPeak = p;
                    }
                }
                catch
                {
                    // Session may have closed; force rescan on next tick
                    _lastRescanUtc = DateTime.MinValue;
                }
            }

            return Math.Clamp((double)maxPeak, 0.0, 1.0);
        }
        catch
        {
            return 0.0;
        }
    }

    private void RefreshPythonSessions()
    {
        ReleasePythonMeters();
        if (_sessionManager == null) return;

        IAudioSessionEnumerator? sessionEnum = null;
        try
        {
            if (_sessionManager.GetSessionEnumerator(out sessionEnum) != 0 || sessionEnum == null)
                return;

            if (sessionEnum.GetCount(out int count) != 0 || count <= 0)
                return;

            for (int i = 0; i < count; i++)
            {
                IAudioSessionControl? ctrl = null;
                try
                {
                    if (sessionEnum.GetSession(i, out ctrl) != 0 || ctrl == null)
                        continue;

                    if (ctrl.GetState(out int state) != 0 || state != AudioSessionStateActive)
                        continue;

                    if (ctrl is IAudioSessionControl2 ctrl2 && ctrl2.GetProcessId(out uint pid) == 0 && pid > 0)
                    {
                        if (IsPythonProcess(pid) && ctrl is IAudioMeterInformation meter)
                        {
                            _pythonMeters.Add(meter);
                            ctrl = null; // Ownership transferred to _pythonMeters
                        }
                    }
                }
                catch
                {
                    // Ignore inaccessible or exiting process
                }
                finally
                {
                    if (ctrl != null)
                    {
                        try { Marshal.ReleaseComObject(ctrl); } catch { }
                    }
                }
            }
        }
        catch
        {
            // Device may have changed; re-init next cycle
            ReleaseDevice();
        }
        finally
        {
            if (sessionEnum != null)
            {
                try { Marshal.ReleaseComObject(sessionEnum); } catch { }
            }
        }
    }

    private static bool IsPythonProcess(uint pid)
    {
        try
        {
            using var proc = Process.GetProcessById((int)pid);
            string name = proc.ProcessName;
            return name.Contains("python", StringComparison.OrdinalIgnoreCase);
        }
        catch
        {
            return false;
        }
    }

    private void ReleasePythonMeters()
    {
        for (int i = 0; i < _pythonMeters.Count; i++)
        {
            try { Marshal.ReleaseComObject(_pythonMeters[i]); } catch { }
        }
        _pythonMeters.Clear();
    }

    private void ReleaseDevice()
    {
        ReleasePythonMeters();
        if (_sessionManager != null)
        {
            try { Marshal.ReleaseComObject(_sessionManager); } catch { }
            _sessionManager = null;
        }
        if (_device != null)
        {
            try { Marshal.ReleaseComObject(_device); } catch { }
            _device = null;
        }
        if (_enumerator != null)
        {
            try { Marshal.ReleaseComObject(_enumerator); } catch { }
            _enumerator = null;
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        ReleaseDevice();
    }

    // ==================== CORE AUDIO COM INTERFACES ====================

    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDeviceEnumerator
    {
        [PreserveSig]
        int EnumAudioEndpoints(int dataFlow, int dwStateMask, out IntPtr ppDevices);

        [PreserveSig]
        int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppEndpoint);
    }

    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDevice
    {
        [PreserveSig]
        int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams, [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
    }

    [ComImport, Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioSessionManager2
    {
        [PreserveSig]
        int GetAudioSessionControl(IntPtr audioSessionGuid, int streamFlags, out IntPtr sessionControl);

        [PreserveSig]
        int GetSimpleAudioVolume(IntPtr audioSessionGuid, int streamFlags, out IntPtr audioVolume);

        [PreserveSig]
        int GetSessionEnumerator(out IAudioSessionEnumerator sessionEnum);
    }

    [ComImport, Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioSessionEnumerator
    {
        [PreserveSig]
        int GetCount(out int sessionCount);

        [PreserveSig]
        int GetSession(int sessionCount, out IAudioSessionControl session);
    }

    [ComImport, Guid("F4B1A599-7266-4319-A8CA-E70ACB11E8CD"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioSessionControl
    {
        [PreserveSig]
        int GetState(out int pRetVal);
    }

    [ComImport, Guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioSessionControl2
    {
        // IAudioSessionControl vtable slots (3..11)
        [PreserveSig] int GetState(out int pRetVal);
        [PreserveSig] int GetDisplayName([MarshalAs(UnmanagedType.LPWStr)] out string pRetVal);
        [PreserveSig] int SetDisplayName([MarshalAs(UnmanagedType.LPWStr)] string value, ref Guid eventContext);
        [PreserveSig] int GetIconPath([MarshalAs(UnmanagedType.LPWStr)] out string pRetVal);
        [PreserveSig] int SetIconPath([MarshalAs(UnmanagedType.LPWStr)] string value, ref Guid eventContext);
        [PreserveSig] int GetGroupingParam(out Guid pRetVal);
        [PreserveSig] int SetGroupingParam(ref Guid overrideValue, ref Guid eventContext);
        [PreserveSig] int RegisterAudioSessionNotification(IntPtr newNotifications);
        [PreserveSig] int UnregisterAudioSessionNotification(IntPtr newNotifications);

        // IAudioSessionControl2 vtable slots (12..14)
        [PreserveSig] int GetSessionIdentifier([MarshalAs(UnmanagedType.LPWStr)] out string pRetVal);
        [PreserveSig] int GetSessionInstanceIdentifier([MarshalAs(UnmanagedType.LPWStr)] out string pRetVal);
        [PreserveSig] int GetProcessId(out uint pRetVal);
    }

    [ComImport, Guid("C02216F6-8C67-4B5B-9D00-D008E73E0064"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioMeterInformation
    {
        [PreserveSig]
        int GetPeakValue(out float pfPeak);
    }
}