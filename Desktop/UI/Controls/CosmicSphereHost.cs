using System;
using System.Windows;
using System.Windows.Media;

namespace JarvisDesktop.Controls;

public sealed class CosmicSphereHost : FrameworkElement
{
    private const int StarCount = 740;
    private const int SynapseCount = 85;
    private const int AlphaBuckets = 32;

    private struct StarData
    {
        public double BaseX;
        public double BaseY;
        public double BaseZ;
        public double BaseRadius;
        public double PhaseOffset;
        public double ScreenX;
        public double ScreenY;
        public double Depth;
        public int ColorTier; // 0 = Core, 1 = Primary, 2 = Secondary, 3 = Crimson Accent
    }

    private readonly StarData[] _stars = new StarData[StarCount];

    // Pre-frozen brush tables [tier][alphaBucket] -> ZERO heap allocations per frame
    private SolidColorBrush[][] _darkStarBrushes = null!;
    private SolidColorBrush[][] _lightStarBrushes = null!;
    private SolidColorBrush[][] _alertStarBrushes = null!;

    private Pen[] _darkSynapsePens = null!;
    private Pen[] _darkSpeakSynapsePens = null!;
    private Pen[] _darkThinkSynapsePens = null!;
    private Pen[] _lightSynapsePens = null!;
    private Pen[] _alertSynapsePens = null!;

    private Pen[] _darkWaveCyanPens = null!;
    private Pen[] _darkWaveIcePens = null!;
    private Pen[] _darkWaveCrimsonPens = null!;
    private Pen[] _lightWavePens = null!;
    private Pen[] _alertWavePens = null!;
    private Pen[] _alertWaveLightPens = null!;

    private Pen _outerOrbitDark = null!;
    private Pen _innerOrbitDark = null!;
    private Pen _outerOrbitSpeak = null!;
    private Pen _innerOrbitSpeak = null!;
    private Pen _outerOrbitLight = null!;
    private Pen _innerOrbitLight = null!;
    private Pen _outerOrbitAlert = null!;
    private Pen _innerOrbitAlert = null!;
    private Pen _outerOrbitThink = null!;

    private double _rotY;
    private double _rotX;
    private double _phaseTime;
    private double _outerOrbitAngle;
    private double _innerOrbitAngle;
    private double _currentRadius = 220.0;

    // Envelope followers for tight, musical sync to real audio
    private double _smoothedMicRms;
    private double _voiceFastEnv;
    private double _voiceSlowEnv;
    private double _voiceTransient;

    // Live state inputs updated from MainWindow
    public bool IsLightLcdTheme { get; set; }
    public bool IsRedAlert { get; set; }
    public bool IsMuted { get; set; }
    public string SystemState { get; set; } = "IDLE";
    public double TargetRms { get; set; }
    /// <summary>Real-time WASAPI playback peak (0..1) from Python TTS process.</summary>
    public double LiveVoicePeak { get; set; }

    public CosmicSphereHost()
    {
        IsHitTestVisible = false;
        SnapsToDevicePixels = false;
        UseLayoutRounding = false;
        RenderOptions.SetEdgeMode(this, EdgeMode.Unspecified);
        InitStars();
        InitFrozenPalette();
    }

    private void InitStars()
    {
        double goldenRatio = (1.0 + Math.Sqrt(5.0)) / 2.0;
        var rng = new Random(42);

        for (int i = 0; i < StarCount; i++)
        {
            double theta = 2.0 * Math.PI * i / goldenRatio;
            double phi = Math.Acos(1.0 - 2.0 * (i + 0.5) / StarCount);

            double bx = Math.Cos(theta) * Math.Sin(phi);
            double by = Math.Sin(theta) * Math.Sin(phi);
            double bz = Math.Cos(phi);

            double diameter = (i % 9 == 0) ? 5.6 : (i % 3 == 0 ? 4.0 : 2.8);

            // ~10% tactical crimson accent dots evenly distributed on the sphere
            int colorTier;
            if (i % 10 == 3)
            {
                colorTier = 3; // Crimson Accent
                diameter = Math.Max(diameter, 4.6);
            }
            else if (i % 7 == 0)
            {
                colorTier = 0; // Core
            }
            else if (i % 3 == 0)
            {
                colorTier = 1; // Primary
            }
            else
            {
                colorTier = 2; // Secondary
            }

            _stars[i] = new StarData
            {
                BaseX = bx,
                BaseY = by,
                BaseZ = bz,
                BaseRadius = diameter * 0.5,
                PhaseOffset = rng.NextDouble() * Math.PI * 2.0,
                ColorTier = colorTier
            };
        }
    }

    private void InitFrozenPalette()
    {
        Color frostWhite = (Color)ColorConverter.ConvertFromString("#FFFFFF");
        Color quantumCyan = (Color)ColorConverter.ConvertFromString("#00F0FF");
        Color iceBlue = (Color)ColorConverter.ConvertFromString("#38BDF8");
        Color crimsonBright = (Color)ColorConverter.ConvertFromString("#FF2A55");
        Color crimsonCore = (Color)ColorConverter.ConvertFromString("#F43F5E");
        Color crimsonDeep = (Color)ColorConverter.ConvertFromString("#BE123C");
        Color amberGold = (Color)ColorConverter.ConvertFromString("#F59E0B");
        Color pitchBlack = (Color)ColorConverter.ConvertFromString("#000000");
        Color deepCarbon = (Color)ColorConverter.ConvertFromString("#0F172A");

        _darkStarBrushes = new[]
        {
            BuildBrushTable(frostWhite),
            BuildBrushTable(quantumCyan),
            BuildBrushTable(iceBlue),
            BuildBrushTable(crimsonBright)
        };

        _lightStarBrushes = new[]
        {
            BuildBrushTable(pitchBlack),
            BuildBrushTable(pitchBlack),
            BuildBrushTable(deepCarbon),
            BuildBrushTable(crimsonDeep)
        };

        _alertStarBrushes = new[]
        {
            BuildBrushTable(crimsonBright),
            BuildBrushTable(crimsonCore),
            BuildBrushTable(crimsonDeep),
            BuildBrushTable(crimsonBright)
        };

        _darkSynapsePens = BuildPenTable(quantumCyan, 0.90);
        _darkSpeakSynapsePens = BuildPenTable(frostWhite, 1.05);
        _darkThinkSynapsePens = BuildPenTable(amberGold, 1.05);
        _lightSynapsePens = BuildPenTable(pitchBlack, 1.30);
        _alertSynapsePens = BuildPenTable(crimsonCore, 1.35);

        _darkWaveCyanPens = BuildPenTable(quantumCyan, 1.8);
        _darkWaveIcePens = BuildPenTable(iceBlue, 1.9);
        _darkWaveCrimsonPens = BuildPenTable(crimsonBright, 1.6);
        _lightWavePens = BuildPenTable(pitchBlack, 2.2);
        _alertWavePens = BuildPenTable(crimsonCore, 2.2);
        _alertWaveLightPens = BuildPenTable(crimsonDeep, 2.6);

        _outerOrbitDark = BuildDashedPen(iceBlue, 1.1, 0.26, new double[] { 10, 6, 2, 6 });
        _innerOrbitDark = BuildDashedPen(quantumCyan, 1.1, 0.22, new double[] { 4, 14 });
        _outerOrbitSpeak = BuildDashedPen(quantumCyan, 1.35, 0.44, new double[] { 10, 6, 2, 6 });
        _innerOrbitSpeak = BuildDashedPen(frostWhite, 1.25, 0.36, new double[] { 4, 14 });
        _outerOrbitLight = BuildDashedPen(pitchBlack, 1.6, 0.56, new double[] { 10, 6, 2, 6 });
        _innerOrbitLight = BuildDashedPen(pitchBlack, 1.5, 0.46, new double[] { 4, 14 });
        _outerOrbitAlert = BuildDashedPen(crimsonCore, 1.8, 0.78, new double[] { 10, 6, 2, 6 });
        _innerOrbitAlert = BuildDashedPen(crimsonDeep, 1.6, 0.68, new double[] { 4, 14 });
        _outerOrbitThink = BuildDashedPen(amberGold, 1.4, 0.54, new double[] { 10, 6, 2, 6 });
    }

    private static SolidColorBrush[] BuildBrushTable(Color baseColor)
    {
        var table = new SolidColorBrush[AlphaBuckets];
        for (int i = 0; i < AlphaBuckets; i++)
        {
            byte alpha = (byte)Math.Clamp((int)Math.Round(((i + 1) / (double)AlphaBuckets) * 255.0), 12, 255);
            var b = new SolidColorBrush(Color.FromArgb(alpha, baseColor.R, baseColor.G, baseColor.B));
            b.Freeze();
            table[i] = b;
        }
        return table;
    }

    private static Pen[] BuildPenTable(Color baseColor, double thickness)
    {
        var table = new Pen[AlphaBuckets];
        for (int i = 0; i < AlphaBuckets; i++)
        {
            byte alpha = (byte)Math.Clamp((int)Math.Round(((i + 1) / (double)AlphaBuckets) * 255.0), 10, 255);
            var b = new SolidColorBrush(Color.FromArgb(alpha, baseColor.R, baseColor.G, baseColor.B));
            b.Freeze();
            var p = new Pen(b, thickness);
            p.Freeze();
            table[i] = p;
        }
        return table;
    }

    private static Pen BuildDashedPen(Color baseColor, double thickness, double opacity, double[] dashes)
    {
        byte alpha = (byte)Math.Clamp((int)Math.Round(opacity * 255.0), 12, 255);
        var b = new SolidColorBrush(Color.FromArgb(alpha, baseColor.R, baseColor.G, baseColor.B));
        b.Freeze();
        var dashStyle = new DashStyle(dashes, 0);
        dashStyle.Freeze();
        var p = new Pen(b, thickness) { DashStyle = dashStyle };
        p.Freeze();
        return p;
    }

    private static int ToBucket(double alpha)
    {
        int idx = (int)Math.Round(Math.Clamp(alpha, 0.0, 1.0) * (AlphaBuckets - 1));
        return Math.Clamp(idx, 0, AlphaBuckets - 1);
    }

    private bool IsListeningState() =>
        SystemState.Contains("LISTEN", StringComparison.OrdinalIgnoreCase) ||
        SystemState.Contains("RECORD", StringComparison.OrdinalIgnoreCase) ||
        SystemState.Contains("WAKE", StringComparison.OrdinalIgnoreCase) ||
        SystemState.Contains("STT", StringComparison.OrdinalIgnoreCase);

    private bool IsThinkingState() =>
        SystemState.Contains("THINK", StringComparison.OrdinalIgnoreCase) ||
        SystemState.Contains("PROCESS", StringComparison.OrdinalIgnoreCase) ||
        SystemState.Contains("LLM", StringComparison.OrdinalIgnoreCase) ||
        SystemState.Contains("SEARCH", StringComparison.OrdinalIgnoreCase);

    public void Step(double dtSeconds)
    {
        double clampedDt = Math.Clamp(dtSeconds, 0.004, 0.050);
        double stepScale = clampedDt * 60.0;
        _phaseTime += 0.034 * stepScale;

        // 1. Live Python TTS Audio Envelope (exact syllable/tone tracking)
        double rawVoice = IsMuted ? 0.0 : Math.Clamp(LiveVoicePeak, 0.0, 1.0);
        // Apply gentle noise gate + perceptual curve so soft/loud syllables map cleanly
        double gatedVoice = rawVoice > 0.006 ? Math.Pow(Math.Clamp((rawVoice - 0.006) * 1.65, 0.0, 1.0), 0.72) : 0.0;

        // Fast attack (crisp syllable onset), smooth natural release (no jarring snap)
        double fastRate = gatedVoice > _voiceFastEnv ? 0.58 : 0.16;
        _voiceFastEnv += (gatedVoice - _voiceFastEnv) * Math.Min(1.0, fastRate * stepScale);

        // Slow baseline to extract syllable beat transients (tone emphasis)
        double slowRate = gatedVoice > _voiceSlowEnv ? 0.12 : 0.08;
        _voiceSlowEnv += (gatedVoice - _voiceSlowEnv) * Math.Min(1.0, slowRate * stepScale);

        double instantTransient = Math.Clamp((_voiceFastEnv - _voiceSlowEnv) * 2.4, 0.0, 1.0);
        double transRate = instantTransient > _voiceTransient ? 0.65 : 0.22;
        _voiceTransient += (instantTransient - _voiceTransient) * Math.Min(1.0, transRate * stepScale);

        // 2. Live Mic RMS Envelope (for LISTENING state)
        double micLerp = TargetRms > _smoothedMicRms ? 0.45 : 0.14;
        _smoothedMicRms += (TargetRms - _smoothedMicRms) * Math.Min(1.0, micLerp * stepScale);
        double visualMic = Math.Sqrt(Math.Clamp(_smoothedMicRms * 3.2, 0.0, 1.0));

        bool isRedAlert = IsRedAlert;
        bool isActivelySpeaking = !IsMuted && _voiceFastEnv > 0.025;
        bool isListening = !IsMuted && !isActivelySpeaking && IsListeningState();
        bool isThinking = !IsMuted && !isActivelySpeaking && IsThinkingState();

        // Subtle, refined orbital & rotational motion
        _outerOrbitAngle = (_outerOrbitAngle + (isRedAlert ? 0.62 : (isThinking ? 0.42 : (isActivelySpeaking ? 0.16 + _voiceFastEnv * 0.22 : 0.11))) * stepScale) % 360.0;
        _innerOrbitAngle = (_innerOrbitAngle - (isRedAlert ? 0.72 : (isActivelySpeaking ? 0.22 + _voiceFastEnv * 0.32 : (isListening ? 0.30 : 0.16))) * stepScale) % 360.0;

        double rotSpeed = isRedAlert ? 0.015
            : (isThinking ? 0.019
            : (isActivelySpeaking ? 0.0055 + _voiceFastEnv * 0.0055 + _voiceTransient * 0.004
            : (isListening ? 0.0075 + visualMic * 0.0045 : 0.0042)));
        _rotY += rotSpeed * stepScale;
        _rotX = Math.Sin(_phaseTime * 0.24) * (isThinking ? 0.36 : 0.26);

        // Restrained, tactile radius breathing directly tied to narrator's voice
        double targetRadius = 220.0;
        if (isRedAlert)
        {
            targetRadius = 224.0 + Math.Sin(_phaseTime * 5.2) * 14.0 + Math.Cos(_phaseTime * 10.4) * 5.0;
        }
        else if (isActivelySpeaking)
        {
            // Tight, proportional beat: ~8-22px max excursion synced strictly to real audio
            targetRadius = 220.0 + (_voiceFastEnv * 14.0) + (_voiceTransient * 10.0);
        }
        else if (isListening)
        {
            targetRadius = 218.0 + Math.Sin(_phaseTime * 2.4) * 5.0 + (visualMic * 18.0);
        }
        else if (isThinking)
        {
            targetRadius = 212.0 + Math.Sin(_phaseTime * 4.2) * 8.0;
        }

        double radLerp = isActivelySpeaking ? 0.36 : 0.18;
        _currentRadius += (targetRadius - _currentRadius) * Math.Min(1.0, radLerp * stepScale);

        InvalidateVisual();
    }

    protected override void OnRender(DrawingContext dc)
    {
        double w = ActualWidth > 0 ? ActualWidth : Width;
        double h = ActualHeight > 0 ? ActualHeight : Height;
        if (double.IsNaN(w) || double.IsNaN(h) || w <= 0 || h <= 0) return;

        double cx = Math.Round(w * 0.5);
        double cy = Math.Round(h * 0.5);
        var center = new Point(cx, cy);

        bool isRedAlert = IsRedAlert;
        bool isActivelySpeaking = !IsMuted && _voiceFastEnv > 0.025;
        bool isListening = !IsMuted && !isActivelySpeaking && IsListeningState();
        bool isThinking = !IsMuted && !isActivelySpeaking && IsThinkingState();
        double visualMic = Math.Sqrt(Math.Clamp(_smoothedMicRms * 3.2, 0.0, 1.0));

        // 1. Orbital Rings
        Pen outerPen = isRedAlert ? _outerOrbitAlert
            : (IsLightLcdTheme ? _outerOrbitLight
            : (isThinking ? _outerOrbitThink
            : (isActivelySpeaking ? _outerOrbitSpeak : _outerOrbitDark)));
        Pen innerPen = isRedAlert ? _innerOrbitAlert
            : (IsLightLcdTheme ? _innerOrbitLight
            : (isActivelySpeaking ? _innerOrbitSpeak : _innerOrbitDark));

        dc.PushTransform(new RotateTransform(_outerOrbitAngle, cx, cy));
        dc.DrawEllipse(null, outerPen, center, 300.0, 300.0);
        dc.Pop();

        dc.PushTransform(new RotateTransform(_innerOrbitAngle, cx, cy));
        dc.DrawEllipse(null, innerPen, center, 260.0, 260.0);
        dc.Pop();

        // 2. Reactive Radar / Vocal Waves (strictly scaled by real audio energy)
        if (isRedAlert || isListening || (isActivelySpeaking && _voiceFastEnv > 0.04))
        {
            double waveSpeed = isRedAlert ? 0.60 : (isActivelySpeaking ? 0.44 : 0.42);
            double waveAlphaScale = isRedAlert ? 0.78
                : (isActivelySpeaking ? (0.14 + _voiceFastEnv * 0.34 + _voiceTransient * 0.16)
                : (0.20 + visualMic * 0.38));
            if (IsLightLcdTheme) waveAlphaScale = Math.Min(0.90, waveAlphaScale + 0.16);

            int ringCount = isActivelySpeaking ? 2 : 3;
            for (int rIdx = 0; rIdx < ringCount; rIdx++)
            {
                double waveProgress = ((_phaseTime * waveSpeed) + (rIdx / (double)ringCount)) % 1.0;
                double waveRadius = _currentRadius * (0.94 + waveProgress * 0.56);
                double alpha = (1.0 - waveProgress) * waveAlphaScale;
                if (alpha > 0.03)
                {
                    Pen[] waveTable = isRedAlert
                        ? (IsLightLcdTheme ? _alertWaveLightPens : _alertWavePens)
                        : (IsLightLcdTheme ? _lightWavePens
                        : (isActivelySpeaking
                            ? (rIdx == 1 ? _darkWaveCrimsonPens : _darkWaveCyanPens)
                            : (isListening ? _darkWaveCyanPens : _darkWaveIcePens)));

                    dc.DrawEllipse(null, waveTable[ToBucket(alpha)], center, waveRadius, waveRadius);
                }
            }
        }

        // 3. Project 740 3D Stars
        double cosY = Math.Cos(_rotY), sinY = Math.Sin(_rotY);
        double cosX = Math.Cos(_rotX), sinX = Math.Sin(_rotX);
        double lcdScaleBoost = IsLightLcdTheme ? 1.16 : 1.0;

        SolidColorBrush[][] palette = isRedAlert
            ? _alertStarBrushes
            : (IsLightLcdTheme ? _lightStarBrushes : _darkStarBrushes);

        double voiceWaveAmp = (_voiceFastEnv * 11.0) + (_voiceTransient * 9.0);

        for (int i = 0; i < StarCount; i++)
        {
            ref StarData s = ref _stars[i];
            double localR = _currentRadius;

            if (isRedAlert)
            {
                localR += Math.Sin(s.BaseX * 6.0 + s.BaseY * 6.0 - _phaseTime * 6.5) * 9.0;
            }
            else if (isActivelySpeaking)
            {
                // Subtle, crisp acoustic ripple synced to real voice amplitude & syllable transients
                localR += Math.Cos(s.BaseX * 4.5 + s.BaseZ * 4.5 + _phaseTime * 4.8) * voiceWaveAmp;
            }
            else if (isListening)
            {
                localR += Math.Sin(s.BaseY * 7.0 - _phaseTime * 4.4) * (4.0 + visualMic * 16.0);
            }
            else if (isThinking)
            {
                localR += Math.Sin(s.BaseZ * 6.0 + _phaseTime * 4.8) * 5.5;
            }

            double x = s.BaseX * localR;
            double y = s.BaseY * localR;
            double z = s.BaseZ * localR;

            double rx = x * cosY - z * sinY;
            double rz1 = x * sinY + z * cosY;
            double ry = y * cosX - rz1 * sinX;
            double rz = y * sinX + rz1 * cosX;

            double depth = rz / (localR + 1.0);
            double perspective = 520.0 / (520.0 - rz * 0.65);

            s.ScreenX = cx + rx * perspective;
            s.ScreenY = cy + ry * perspective;
            s.Depth = depth;
        }

        // 4. Draw Neural Synapses
        Pen[] synapseTable = isRedAlert
            ? _alertSynapsePens
            : (IsLightLcdTheme ? _lightSynapsePens
            : (isThinking ? _darkThinkSynapsePens
            : (isActivelySpeaking ? _darkSpeakSynapsePens : _darkSynapsePens)));

        double depthThreshold = isRedAlert ? 0.04 : 0.12;
        double alertPulse = isRedAlert ? (0.78 + 0.22 * Math.Sin(_phaseTime * 6.0)) : 1.0;

        double baseSynapseOpacity;
        if (isRedAlert)
        {
            baseSynapseOpacity = (IsLightLcdTheme ? 0.88 : 0.72) * alertPulse;
        }
        else if (isActivelySpeaking)
        {
            baseSynapseOpacity = IsLightLcdTheme
                ? (0.44 + _voiceFastEnv * 0.28)
                : (0.20 + _voiceFastEnv * 0.24 + _voiceTransient * 0.14);
        }
        else if (isListening)
        {
            baseSynapseOpacity = IsLightLcdTheme ? 0.55 : (0.20 + visualMic * 0.24);
        }
        else if (isThinking)
        {
            baseSynapseOpacity = IsLightLcdTheme ? 0.58 : 0.38;
        }
        else
        {
            baseSynapseOpacity = IsLightLcdTheme ? 0.38 : 0.16;
        }

        for (int i = 0; i < SynapseCount; i++)
        {
            int idxA = (i * 8) % StarCount;
            int idxB = (i * 8 + 1) % StarCount;
            double dA = _stars[idxA].Depth;
            double dB = _stars[idxB].Depth;

            if (dA > depthThreshold && dB > depthThreshold)
            {
                double op = Math.Clamp(Math.Min(dA, dB) * baseSynapseOpacity, 0.0, 1.0);
                if (op > 0.03)
                {
                    dc.DrawLine(
                        synapseTable[ToBucket(op)],
                        new Point(_stars[idxA].ScreenX, _stars[idxA].ScreenY),
                        new Point(_stars[idxB].ScreenX, _stars[idxB].ScreenY));
                }
            }
        }

        // 5. Draw 740 Crisp Stars
        double minAlpha = isRedAlert ? 0.35 : (IsLightLcdTheme ? 0.30 : 0.12);
        double alphaBoost = isRedAlert ? 0.74
            : (IsLightLcdTheme ? 0.72
            : (isActivelySpeaking ? (0.56 + _voiceFastEnv * 0.14) : 0.56));
        double twinkleFreq = isRedAlert ? 5.5 : (isActivelySpeaking ? 3.2 : 2.2);

        for (int i = 0; i < StarCount; i++)
        {
            ref StarData s = ref _stars[i];
            double twinkle = 0.90 + 0.10 * Math.Sin(_phaseTime * twinkleFreq + s.PhaseOffset);
            double alpha = Math.Clamp(((s.Depth + 1.18) * alphaBoost) * twinkle, minAlpha, 1.0);

            if (s.ColorTier == 3 && !isRedAlert)
            {
                alpha = Math.Clamp(alpha * 1.24, 0.25, 1.0);
            }

            double dotScale = Math.Clamp((s.Depth + 1.55) * 0.60, 0.52, 1.55) * lcdScaleBoost;
            if (isRedAlert)
            {
                dotScale *= 1.15;
            }
            else if (isActivelySpeaking)
            {
                dotScale *= 1.0 + (_voiceFastEnv * 0.14) + (_voiceTransient * 0.08);
            }
            else if (isListening)
            {
                dotScale *= 1.0 + (visualMic * 0.14);
            }

            double rad = s.BaseRadius * dotScale;
            double px = Math.Round(s.ScreenX * 2.0) * 0.5;
            double py = Math.Round(s.ScreenY * 2.0) * 0.5;
            dc.DrawEllipse(palette[s.ColorTier][ToBucket(alpha)], null, new Point(px, py), rad, rad);
        }
    }
}