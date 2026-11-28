using System;
using System.Collections.Generic;
using System.Windows;
using System.Windows.Media;

namespace OmegaDesktop.Controls;

public sealed class CosmicSphereHost : FrameworkElement
{
    private const int StarCount = 740;
    private const int SynapseCount = 85;
    private const int AlphaBuckets = 32;
    private const int ArcSamples = 14;

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

    private struct CapitalNode
    {
        public double X;
        public double Y;
        public double Z;
        public byte Tier; // 2 = Tehran HQ, 1 = Global Hub, 0 = Capital
        public int ColorTier;
        public double PhaseOffset;
        public double ScreenX;
        public double ScreenY;
        public double Depth;
    }

    private struct EarthArc
    {
        public double[] X;
        public double[] Y;
        public double[] Z;
        public bool IsTehranLink;
        public double PulsePhase;
        public double PulseSpeed;
    }

    // Cosmic Mode Buffers
    private readonly StarData[] _stars = new StarData[StarCount];

    // Tactical Earth Mode Buffers (pre-projected unit sphere coords -> zero trig per frame)
    private double[] _borderX = Array.Empty<double>();
    private double[] _borderY = Array.Empty<double>();
    private double[] _borderZ = Array.Empty<double>();
    private bool[] _borderStart = Array.Empty<bool>();
    private CapitalNode[] _capitals = Array.Empty<CapitalNode>();
    private EarthArc[] _earthArcs = Array.Empty<EarthArc>();

    // Pre-frozen brush tables [tier][alphaBucket] -> ZERO heap allocations per frame
    private SolidColorBrush[][] _darkStarBrushes = null!;
    private SolidColorBrush[][] _lightStarBrushes = null!;
    private SolidColorBrush[][] _alertStarBrushes = null!;

    private Pen[] _darkSynapsePens = null!;
    private Pen[] _darkSpeakSynapsePens = null!;
    private Pen[] _darkThinkSynapsePens = null!;
    private Pen[] _darkCrimsonArcPens = null!;
    private Pen[] _lightSynapsePens = null!;
    private Pen[] _lightCrimsonArcPens = null!;
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

    // Earth-specific frozen pens
    private Pen _earthBorderFrontDark = null!;
    private Pen _earthBorderBackDark = null!;
    private Pen _earthBorderFrontLight = null!;
    private Pen _earthBorderFrontAlert = null!;
    private Pen _earthBorderBackAlert = null!;
    private Pen _earthRimDark = null!;
    private Pen _earthRimLight = null!;
    private Pen _earthRimAlert = null!;

    private double _rotY;
    private double _rotX;
    // Start Earth centered nicely on Tehran (51.4E)
    private double _earthRotY = 51.4 * Math.PI / 180.0;
    private double _phaseTime;
    private double _outerOrbitAngle;
    private double _innerOrbitAngle;
    private double _currentRadius = 220.0;

    // Envelope followers for tight, musical sync to real WASAPI audio
    private double _smoothedMicRms;
    private double _voiceFastEnv;
    private double _voiceSlowEnv;
    private double _voiceTransient;

    // Live state inputs updated from MainWindow
    public bool IsEarthMode { get; set; } = true;
    public bool IsLightLcdTheme { get; set; }
    public bool IsRedAlert { get; set; }
    public bool IsMuted { get; set; }
    public string SystemState { get; set; } = "IDLE";
    public double TargetRms { get; set; }
    public double LiveVoicePeak { get; set; }

    public CosmicSphereHost()
    {
        IsHitTestVisible = false;
        SnapsToDevicePixels = false;
        UseLayoutRounding = false;
        RenderOptions.SetEdgeMode(this, EdgeMode.Unspecified);
        InitStars();
        InitEarthGeometry();
        InitFrozenPalette();
    }

    private static void LatLonToUnit(double lonDeg, double latDeg, out double x, out double y, out double z)
    {
        double lon = lonDeg * (Math.PI / 180.0);
        double lat = latDeg * (Math.PI / 180.0);
        double cosLat = Math.Cos(lat);
        x = cosLat * Math.Sin(lon);
        y = -Math.Sin(lat); // North = UP (-Y in WPF)
        z = cosLat * Math.Cos(lon);
    }

    private void InitEarthGeometry()
    {
        // 1. Pre-compute border vertices
        var bxList = new List<double>(2600);
        var byList = new List<double>(2600);
        var bzList = new List<double>(2600);
        var startList = new List<bool>(2600);

        var raw = EarthGeoData.BorderPolylines;
        bool nextIsStart = true;
        int idx = 0;
        while (idx < raw.Length)
        {
            if (raw[idx] == EarthGeoData.PolyBreak)
            {
                nextIsStart = true;
                idx++;
                continue;
            }
            if (idx + 1 >= raw.Length) break;

            double lonDeg = raw[idx] * 0.1;
            double latDeg = raw[idx + 1] * 0.1;
            LatLonToUnit(lonDeg, latDeg, out double x, out double y, out double z);

            bxList.Add(x);
            byList.Add(y);
            bzList.Add(z);
            startList.Add(nextIsStart);

            nextIsStart = false;
            idx += 2;
        }

        _borderX = bxList.ToArray();
        _borderY = byList.ToArray();
        _borderZ = bzList.ToArray();
        _borderStart = startList.ToArray();

        // 2. Pre-compute world capital nodes
        var rng = new Random(1337);
        var caps = EarthGeoData.Capitals;
        _capitals = new CapitalNode[caps.Length];
        for (int i = 0; i < caps.Length; i++)
        {
            LatLonToUnit(caps[i].Lon10 * 0.1, caps[i].Lat10 * 0.1, out double x, out double y, out double z);
            byte tier = caps[i].Tier;
            int colorTier;
            if (tier == 2)
                colorTier = 3; // Tehran HQ -> Crimson Accent
            else if (tier == 1)
                colorTier = (i % 4 == 0) ? 3 : 1; // Global hubs: mostly bright cyan + some crimson accents
            else
                colorTier = (i % 11 == 0) ? 3 : ((i % 3 == 0) ? 0 : 2);

            _capitals[i] = new CapitalNode
            {
                X = x,
                Y = y,
                Z = z,
                Tier = tier,
                ColorTier = colorTier,
                PhaseOffset = rng.NextDouble() * Math.PI * 2.0
            };
        }

        // 3. Pre-compute elevated Great-Circle Synaptic Arcs
        var arcDefs = EarthGeoData.SynapticArcs;
        _earthArcs = new EarthArc[arcDefs.Length];
        for (int a = 0; a < arcDefs.Length; a++)
        {
            int fromIdx = arcDefs[a].FromIdx;
            int toIdx = arcDefs[a].ToIdx;
            var nA = _capitals[fromIdx];
            var nB = _capitals[toIdx];

            double dot = Math.Clamp(nA.X * nB.X + nA.Y * nB.Y + nA.Z * nB.Z, -0.9999, 0.9999);
            double omega = Math.Acos(dot);
            double sinOmega = Math.Sin(omega);
            // Higher orbital arc for longer intercontinental links
            double maxLift = Math.Clamp(0.05 + (omega / Math.PI) * 0.16, 0.06, 0.21);

            var ax = new double[ArcSamples];
            var ay = new double[ArcSamples];
            var az = new double[ArcSamples];

            for (int s = 0; s < ArcSamples; s++)
            {
                double t = s / (double)(ArcSamples - 1);
                double wA = sinOmega > 1e-4 ? Math.Sin((1.0 - t) * omega) / sinOmega : (1.0 - t);
                double wB = sinOmega > 1e-4 ? Math.Sin(t * omega) / sinOmega : t;
                double alt = 1.0 + Math.Sin(t * Math.PI) * maxLift;

                ax[s] = (nA.X * wA + nB.X * wB) * alt;
                ay[s] = (nA.Y * wA + nB.Y * wB) * alt;
                az[s] = (nA.Z * wA + nB.Z * wB) * alt;
            }

            _earthArcs[a] = new EarthArc
            {
                X = ax,
                Y = ay,
                Z = az,
                IsTehranLink = (fromIdx == 0 || toIdx == 0),
                PulsePhase = rng.NextDouble(),
                PulseSpeed = 0.14 + rng.NextDouble() * 0.10
            };
        }
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

            int colorTier;
            if (i % 10 == 3)
            {
                colorTier = 3;
                diameter = Math.Max(diameter, 4.6);
            }
            else if (i % 7 == 0)
            {
                colorTier = 0;
            }
            else if (i % 3 == 0)
            {
                colorTier = 1;
            }
            else
            {
                colorTier = 2;
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
        // Gruvbox Material Dark Palette (#141617 / #D4BE98 / #E78A4E + complementary warm gold & coral)
        Color frostWhite = (Color)ColorConverter.ConvertFromString("#F2E5BC");    // Warm Light Cream
        Color quantumCyan = (Color)ColorConverter.ConvertFromString("#E78A4E");   // Gruvbox Orange Accent
        Color iceBlue = (Color)ColorConverter.ConvertFromString("#D4BE98");       // Gruvbox Warm Sand
        Color crimsonBright = (Color)ColorConverter.ConvertFromString("#EA6962"); // Gruvbox Coral Red (Tehran HQ / Accents)
        Color crimsonCore = (Color)ColorConverter.ConvertFromString("#EA6962");
        Color crimsonDeep = (Color)ColorConverter.ConvertFromString("#C14A4A");
        Color amberGold = (Color)ColorConverter.ConvertFromString("#D8A657");     // Gruvbox Warm Gold
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

        _darkSynapsePens = BuildPenTable(quantumCyan, 0.95);
        _darkSpeakSynapsePens = BuildPenTable(frostWhite, 1.10);
        _darkThinkSynapsePens = BuildPenTable(amberGold, 1.10);
        _darkCrimsonArcPens = BuildPenTable(crimsonBright, 1.10);
        _lightSynapsePens = BuildPenTable(pitchBlack, 1.25);
        _lightCrimsonArcPens = BuildPenTable(crimsonDeep, 1.30);
        _alertSynapsePens = BuildPenTable(crimsonCore, 1.35);

        _darkWaveCyanPens = BuildPenTable(quantumCyan, 1.8);
        _darkWaveIcePens = BuildPenTable(iceBlue, 1.9);
        _darkWaveCrimsonPens = BuildPenTable(crimsonBright, 1.6);
        _lightWavePens = BuildPenTable(pitchBlack, 2.2);
        _alertWavePens = BuildPenTable(crimsonCore, 2.2);
        _alertWaveLightPens = BuildPenTable(crimsonDeep, 2.6);

        _outerOrbitDark = BuildDashedPen(iceBlue, 1.1, 0.28, new double[] { 10, 6, 2, 6 });
        _innerOrbitDark = BuildDashedPen(quantumCyan, 1.1, 0.24, new double[] { 4, 14 });
        _outerOrbitSpeak = BuildDashedPen(quantumCyan, 1.35, 0.48, new double[] { 10, 6, 2, 6 });
        _innerOrbitSpeak = BuildDashedPen(frostWhite, 1.25, 0.38, new double[] { 4, 14 });
        _outerOrbitLight = BuildDashedPen(pitchBlack, 1.6, 0.56, new double[] { 10, 6, 2, 6 });
        _innerOrbitLight = BuildDashedPen(pitchBlack, 1.5, 0.46, new double[] { 4, 14 });
        _outerOrbitAlert = BuildDashedPen(crimsonCore, 1.8, 0.78, new double[] { 10, 6, 2, 6 });
        _innerOrbitAlert = BuildDashedPen(crimsonDeep, 1.6, 0.68, new double[] { 4, 14 });
        _outerOrbitThink = BuildDashedPen(amberGold, 1.4, 0.56, new double[] { 10, 6, 2, 6 });

        // Crisp country border & globe rim pens (Warm Sand borders + Gruvbox Orange rim)
        _earthBorderFrontDark = BuildSolidPen(iceBlue, 1.05, 0.74);
        _earthBorderBackDark = BuildSolidPen(iceBlue, 0.70, 0.11);
        _earthBorderFrontLight = BuildSolidPen(pitchBlack, 1.25, 0.88);
        _earthBorderFrontAlert = BuildSolidPen(crimsonCore, 1.15, 0.85);
        _earthBorderBackAlert = BuildSolidPen(crimsonDeep, 0.75, 0.16);

        _earthRimDark = BuildSolidPen(quantumCyan, 1.15, 0.30);
        _earthRimLight = BuildSolidPen(pitchBlack, 1.4, 0.48);
        _earthRimAlert = BuildSolidPen(crimsonCore, 1.4, 0.58);
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

    private static Pen BuildSolidPen(Color baseColor, double thickness, double opacity)
    {
        byte alpha = (byte)Math.Clamp((int)Math.Round(opacity * 255.0), 8, 255);
        var b = new SolidColorBrush(Color.FromArgb(alpha, baseColor.R, baseColor.G, baseColor.B));
        b.Freeze();
        var p = new Pen(b, thickness) { StartLineCap = PenLineCap.Round, EndLineCap = PenLineCap.Round };
        p.Freeze();
        return p;
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
        double gatedVoice = rawVoice > 0.006 ? Math.Pow(Math.Clamp((rawVoice - 0.006) * 1.65, 0.0, 1.0), 0.72) : 0.0;

        double fastRate = gatedVoice > _voiceFastEnv ? 0.58 : 0.16;
        _voiceFastEnv += (gatedVoice - _voiceFastEnv) * Math.Min(1.0, fastRate * stepScale);

        double slowRate = gatedVoice > _voiceSlowEnv ? 0.12 : 0.08;
        _voiceSlowEnv += (gatedVoice - _voiceSlowEnv) * Math.Min(1.0, slowRate * stepScale);

        double instantTransient = Math.Clamp((_voiceFastEnv - _voiceSlowEnv) * 2.4, 0.0, 1.0);
        double transRate = instantTransient > _voiceTransient ? 0.65 : 0.22;
        _voiceTransient += (instantTransient - _voiceTransient) * Math.Min(1.0, transRate * stepScale);

        // 2. Live Mic RMS Envelope
        double micLerp = TargetRms > _smoothedMicRms ? 0.45 : 0.14;
        _smoothedMicRms += (TargetRms - _smoothedMicRms) * Math.Min(1.0, micLerp * stepScale);
        double visualMic = Math.Sqrt(Math.Clamp(_smoothedMicRms * 3.2, 0.0, 1.0));

        bool isRedAlert = IsRedAlert;
        bool isActivelySpeaking = !IsMuted && _voiceFastEnv > 0.025;
        bool isListening = !IsMuted && !isActivelySpeaking && IsListeningState();
        bool isThinking = !IsMuted && !isActivelySpeaking && IsThinkingState();

        _outerOrbitAngle = (_outerOrbitAngle + (isRedAlert ? 0.62 : (isThinking ? 0.42 : (isActivelySpeaking ? 0.16 + _voiceFastEnv * 0.22 : 0.11))) * stepScale) % 360.0;
        _innerOrbitAngle = (_innerOrbitAngle - (isRedAlert ? 0.72 : (isActivelySpeaking ? 0.22 + _voiceFastEnv * 0.32 : (isListening ? 0.30 : 0.16))) * stepScale) % 360.0;

        double rotSpeed = isRedAlert ? 0.014
            : (isThinking ? 0.017
            : (isActivelySpeaking ? 0.0045 + _voiceFastEnv * 0.0045 + _voiceTransient * 0.003
            : (isListening ? 0.0065 + visualMic * 0.0040 : 0.0035)));

        _rotY += rotSpeed * stepScale;
        _rotX = Math.Sin(_phaseTime * 0.24) * (isThinking ? 0.36 : 0.26);

        // Earth spins smoothly West-to-East (slower & majestic so continents are easy to read)
        double earthSpin = isRedAlert ? 0.009 : (isThinking ? 0.011 : (isActivelySpeaking ? 0.0034 + _voiceFastEnv * 0.0025 : 0.0026));
        _earthRotY -= earthSpin * stepScale;

        double targetRadius = 220.0;
        if (isRedAlert)
        {
            targetRadius = 224.0 + Math.Sin(_phaseTime * 5.2) * 14.0 + Math.Cos(_phaseTime * 10.4) * 5.0;
        }
        else if (isActivelySpeaking)
        {
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

        // 1. Shared Orbital Rings
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

        // 2. Shared Reactive WASAPI Vocal / Radar Waves
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

        if (IsEarthMode)
        {
            RenderTacticalEarth(dc, cx, cy, center, isRedAlert, isActivelySpeaking, isListening, isThinking, visualMic);
        }
        else
        {
            RenderCosmicSphere(dc, cx, cy, isRedAlert, isActivelySpeaking, isListening, isThinking, visualMic);
        }
    }

    // ==================== MODE A: TACTICAL EARTH GLOBE ====================

    private void RenderTacticalEarth(
        DrawingContext dc,
        double cx,
        double cy,
        Point center,
        bool isRedAlert,
        bool isActivelySpeaking,
        bool isListening,
        bool isThinking,
        double visualMic)
    {
        double R = _currentRadius;
        double cosY = Math.Cos(_earthRotY), sinY = Math.Sin(_earthRotY);
        // Gentle 16 deg tilt toward Northern Hemisphere + subtle breathing sway
        double tilt = 0.28 + Math.Sin(_phaseTime * 0.18) * 0.06;
        double cosX = Math.Cos(tilt), sinX = Math.Sin(tilt);

        // 1. Subtle Globe Horizon Rim
        Pen rimPen = isRedAlert ? _earthRimAlert : (IsLightLcdTheme ? _earthRimLight : _earthRimDark);
        dc.DrawEllipse(null, rimPen, center, R, R);

        // 2. Country Borders in a single batched StreamGeometry (front + faint glass back)
        var frontGeo = new StreamGeometry();
        var backGeo = !IsLightLcdTheme ? new StreamGeometry() : null;

        using (var fCtx = frontGeo.Open())
        using (var bCtx = backGeo?.Open())
        {
            double prevSx = 0, prevSy = 0, prevRz = 0;
            bool hasPrev = false;

            for (int i = 0; i < _borderX.Length; i++)
            {
                double x = _borderX[i];
                double y = _borderY[i];
                double z = _borderZ[i];

                double rx = x * cosY - z * sinY;
                double rz1 = x * sinY + z * cosY;
                double ry = y * cosX + rz1 * sinX;
                double rz = -y * sinX + rz1 * cosX;

                double perspective = 560.0 / (560.0 - (rz * R) * 0.55);
                double sx = cx + (rx * R) * perspective;
                double sy = cy + (ry * R) * perspective;

                if (_borderStart[i] || !hasPrev)
                {
                    hasPrev = true;
                }
                else
                {
                    bool front = (rz > -0.04 && prevRz > -0.04);
                    if (front)
                    {
                        fCtx.BeginFigure(new Point(prevSx, prevSy), false, false);
                        fCtx.LineTo(new Point(sx, sy), true, false);
                    }
                    else if (bCtx != null && rz <= -0.04 && prevRz <= -0.04)
                    {
                        bCtx.BeginFigure(new Point(prevSx, prevSy), false, false);
                        bCtx.LineTo(new Point(sx, sy), true, false);
                    }
                }

                prevSx = sx;
                prevSy = sy;
                prevRz = rz;
            }
        }

        if (backGeo != null)
        {
            backGeo.Freeze();
            dc.DrawGeometry(null, isRedAlert ? _earthBorderBackAlert : _earthBorderBackDark, backGeo);
        }

        frontGeo.Freeze();
        Pen frontPen = isRedAlert
            ? _earthBorderFrontAlert
            : (IsLightLcdTheme ? _earthBorderFrontLight : _earthBorderFrontDark);
        dc.DrawGeometry(null, frontPen, frontGeo);

        // 3. Great-Circle Curved Synaptic Arcs between World Capitals
        SolidColorBrush[][] palette = isRedAlert
            ? _alertStarBrushes
            : (IsLightLcdTheme ? _lightStarBrushes : _darkStarBrushes);

        Pen[] cyanArcPens = isRedAlert
            ? _alertSynapsePens
            : (IsLightLcdTheme ? _lightSynapsePens
            : (isThinking ? _darkThinkSynapsePens
            : (isActivelySpeaking ? _darkSpeakSynapsePens : _darkSynapsePens)));

        Pen[] crimsonArcPens = isRedAlert
            ? _alertSynapsePens
            : (IsLightLcdTheme ? _lightCrimsonArcPens : _darkCrimsonArcPens);

        double arcBaseAlpha = isRedAlert ? 0.62
            : (IsLightLcdTheme
                ? (isActivelySpeaking ? 0.56 + _voiceFastEnv * 0.25 : 0.44)
                : (isActivelySpeaking ? 0.34 + _voiceFastEnv * 0.34 + _voiceTransient * 0.16
                : (isThinking ? 0.44 : (isListening ? 0.30 + visualMic * 0.22 : 0.24))));

        for (int a = 0; a < _earthArcs.Length; a++)
        {
            ref EarthArc arc = ref _earthArcs[a];
            Pen[] arcPens = arc.IsTehranLink ? crimsonArcPens : cyanArcPens;

            double prevSx = 0, prevSy = 0, prevRz = 0;
            bool started = false;

            // Animated signal packet position along this arc
            double speedBoost = isActivelySpeaking ? (1.0 + _voiceFastEnv * 1.4) : (isThinking ? 1.6 : 1.0);
            double pulseT = (arc.PulsePhase + _phaseTime * arc.PulseSpeed * 0.22 * speedBoost) % 1.0;
            int pulseSeg = (int)(pulseT * (ArcSamples - 1));

            for (int s = 0; s < ArcSamples; s++)
            {
                double x = arc.X[s];
                double y = arc.Y[s];
                double z = arc.Z[s];

                double rx = x * cosY - z * sinY;
                double rz1 = x * sinY + z * cosY;
                double ry = y * cosX + rz1 * sinX;
                double rz = -y * sinX + rz1 * cosX;

                double perspective = 560.0 / (560.0 - (rz * R) * 0.55);
                double sx = cx + (rx * R) * perspective;
                double sy = cy + (ry * R) * perspective;

                if (started && rz > 0.0 && prevRz > 0.0)
                {
                    double depthFade = Math.Clamp(Math.Min(rz, prevRz) * 1.35, 0.10, 1.0);
                    double segAlpha = Math.Clamp(arcBaseAlpha * depthFade, 0.04, 0.95);
                    dc.DrawLine(arcPens[ToBucket(segAlpha)], new Point(prevSx, prevSy), new Point(sx, sy));

                    // Glowing signal packet dot traveling along visible arc
                    if (s == pulseSeg)
                    {
                        double pAlpha = Math.Clamp((isActivelySpeaking ? 0.95 : 0.78) * depthFade, 0.20, 1.0);
                        int pTier = arc.IsTehranLink ? 3 : 1;
                        double pRad = arc.IsTehranLink ? 2.4 : 1.9;
                        dc.DrawEllipse(palette[pTier][ToBucket(pAlpha)], null, new Point(sx, sy), pRad, pRad);
                    }
                }

                prevSx = sx;
                prevSy = sy;
                prevRz = rz;
                started = true;
            }
        }

        // 4. World Capital City Dots + Tehran Primary Beacon
        double lcdScaleBoost = IsLightLcdTheme ? 1.12 : 1.0;
        for (int i = 0; i < _capitals.Length; i++)
        {
            ref CapitalNode c = ref _capitals[i];
            double rx = c.X * cosY - c.Z * sinY;
            double rz1 = c.X * sinY + c.Z * cosY;
            double ry = c.Y * cosX + rz1 * sinX;
            double rz = -c.Y * sinX + rz1 * cosX;

            if (rz <= -0.06)
            {
                // Very subtle back-side capital glow only in dark mode
                if (!IsLightLcdTheme && c.Tier >= 1)
                {
                    double pBack = 560.0 / (560.0 - (rz * R) * 0.55);
                    double bx = Math.Round((cx + (rx * R) * pBack) * 2.0) * 0.5;
                    double by = Math.Round((cy + (ry * R) * pBack) * 2.0) * 0.5;
                    dc.DrawEllipse(palette[c.ColorTier][ToBucket(0.14)], null, new Point(bx, by), 1.4, 1.4);
                }
                continue;
            }

            double perspective = 560.0 / (560.0 - (rz * R) * 0.55);
            double sx = Math.Round((cx + (rx * R) * perspective) * 2.0) * 0.5;
            double sy = Math.Round((cy + (ry * R) * perspective) * 2.0) * 0.5;

            double twinkle = 0.88 + 0.12 * Math.Sin(_phaseTime * (isActivelySpeaking ? 3.5 : 2.2) + c.PhaseOffset);
            double frontFade = Math.Clamp((rz + 0.12) * 1.15, 0.20, 1.0);
            double alpha = Math.Clamp(frontFade * twinkle * (c.Tier >= 1 ? 1.0 : 0.86), 0.18, 1.0);

            double baseRad = c.Tier == 2 ? 4.2 : (c.Tier == 1 ? 2.9 : 1.9);
            double scale = lcdScaleBoost;
            if (isActivelySpeaking)
            {
                scale *= 1.0 + (_voiceFastEnv * 0.16) + (_voiceTransient * 0.10);
            }
            else if (isListening)
            {
                scale *= 1.0 + (visualMic * 0.12);
            }

            double rad = baseRad * scale;
            dc.DrawEllipse(palette[c.ColorTier][ToBucket(alpha)], null, new Point(sx, sy), rad, rad);

            // Tehran HQ pulsing tactical target ring
            if (c.Tier == 2)
            {
                double beaconProg = (_phaseTime * 0.55) % 1.0;
                double beaconRad = rad + 3.0 + beaconProg * (11.0 + _voiceFastEnv * 7.0);
                double beaconAlpha = (1.0 - beaconProg) * (IsLightLcdTheme ? 0.85 : 0.78);
                if (beaconAlpha > 0.04)
                {
                    Pen[] beaconPens = IsLightLcdTheme ? _lightCrimsonArcPens : _darkCrimsonArcPens;
                    dc.DrawEllipse(null, beaconPens[ToBucket(beaconAlpha)], new Point(sx, sy), beaconRad, beaconRad);
                }
            }
        }
    }

    // ==================== MODE B: COSMIC STAR SPHERE ====================

    private void RenderCosmicSphere(
        DrawingContext dc,
        double cx,
        double cy,
        bool isRedAlert,
        bool isActivelySpeaking,
        bool isListening,
        bool isThinking,
        double visualMic)
    {
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