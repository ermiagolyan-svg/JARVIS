using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Shapes;
using System.Windows.Shell;
using System.Windows.Threading;
using JarvisDesktop.Models;
using JarvisDesktop.Services;

namespace JarvisDesktop;

public class ChatDisplayItem
{
    public string SenderLabel { get; set; } = string.Empty;
    public string Content { get; set; } = string.Empty;
    public string Timestamp { get; set; } = string.Empty;
}

internal sealed class StarParticle
{
    public double BaseX;
    public double BaseY;
    public double BaseZ;
    public double BaseSize;
    public double PhaseOffset;
    public double ScreenX;
    public double ScreenY;
    public double Depth;
    public int ColorTier; // 0 = Core, 1 = Primary, 2 = Secondary
    public Ellipse Dot = null!;
}

public partial class MainWindow : Window
{
    private readonly JarvisBackendClient _client;
    private readonly DispatcherTimer _animTimer;
    private readonly DispatcherTimer _pollTimer;
    private readonly ObservableCollection<ChatDisplayItem> _chatMessages = new();
    private List<LogEntryItem> _cachedLogs = new();

    // Window Maximize (WorkArea) & True FullScreen States
    private Rect _restoreBounds = new(120, 80, 1640, 920);
    private bool _isWorkAreaMaximized;
    private bool _isFullScreen;
    private bool _isLightLcdTheme; // Theme 2: Pure White Background + Pitch Black Stars for Transparent Case LCD
    private readonly WindowChrome _defaultWindowChrome;

    // 3D Cosmic Star Sphere (>2x Density: 740 Stars & 85 Neural Synapses)
    private const int StarCount = 740;
    private const int SynapseCount = 85;
    private readonly List<StarParticle> _stars = new(StarCount);
    private readonly List<Ellipse> _listeningWaves = new();
    private readonly List<Line> _synapseLines = new(SynapseCount);
    private Ellipse? _outerOrbitRing;
    private Ellipse? _innerOrbitRing;
    private RotateTransform? _outerOrbitRotate;
    private RotateTransform? _innerOrbitRotate;

    private double _rotY;
    private double _rotX;
    private double _phaseTime;
    private double _currentRadius = 220.0;
    private double _currentRms;
    private bool _isMuted;
    private string _currentState = "IDLE";
    private string? _activePendingActionId;
    private string _lastSeenWsMessage = string.Empty;

    // Slide-out drawer states
    private bool _leftOpen;
    private bool _rightOpen;
    private bool _bottomOpen;

    // Theme 1 (Cyber-Ice Dark) Brushes
    private readonly SolidColorBrush _iceBlueBrush = new((Color)ColorConverter.ConvertFromString("#38BDF8"));
    private readonly SolidColorBrush _quantumCyanBrush = new((Color)ColorConverter.ConvertFromString("#00F0FF"));
    private readonly SolidColorBrush _frostWhiteBrush = new((Color)ColorConverter.ConvertFromString("#F8FAFC"));
    private readonly SolidColorBrush _amberGlitchBrush = new((Color)ColorConverter.ConvertFromString("#F59E0B"));
    private readonly SolidColorBrush _crimsonCoreBrush = new((Color)ColorConverter.ConvertFromString("#F43F5E"));

    // Theme 2 (Transparent LCD White/Black High-Contrast) Brushes
    private readonly SolidColorBrush _lcdPitchBlackBrush = new((Color)ColorConverter.ConvertFromString("#000000"));
    private readonly SolidColorBrush _lcdDeepCarbonBrush = new((Color)ColorConverter.ConvertFromString("#0F172A"));
    private readonly SolidColorBrush _lcdDarkCobaltBrush = new((Color)ColorConverter.ConvertFromString("#0369A1"));

    public MainWindow()
    {
        InitializeComponent();
        _defaultWindowChrome = WindowChrome.GetWindowChrome(this);
        LstConversation.ItemsSource = _chatMessages;

        InitializeCosmicSphere();

        _client = new JarvisBackendClient();
        _client.OnHudUpdate += HandleHudUpdateFromWs;
        _client.OnConnectionStateChanged += HandleWsConnectionState;
        _client.OnClientLog += msg => Dispatcher.InvokeAsync(() => TxtStatusBar.Text = msg);

        // 60 FPS Cosmic Sphere & HUD Renderer
        _animTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(16) };
        _animTimer.Tick += AnimTimer_Tick;
        _animTimer.Start();

        // 2.5s Telemetry & Status sync
        _pollTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(2500) };
        _pollTimer.Tick += async (_, _) => await PollBackendSubsystemsAsync();
        _pollTimer.Start();

        Loaded += MainWindow_Loaded;
        Closed += (_, _) =>
        {
            _animTimer.Stop();
            _pollTimer.Stop();
            _client.Dispose();
        };
    }

    // ==================== THEME SWITCHER (THEME 1 DARK vs THEME 2 TRANSPARENT LCD LIGHT) ====================

    private void BtnThemeSwitch_Click(object sender, RoutedEventArgs e) => ToggleLcdTheme();

    private void ToggleLcdTheme()
    {
        _isLightLcdTheme = !_isLightLcdTheme;
        var res = Application.Current.Resources;

        if (_isLightLcdTheme)
        {
            // THEME 2: Pure White (#FFFFFF = 100% Transparent on Case LCD) + Pitch Black Stars & Controls
            res["BgDeepNavyBrush"] = new SolidColorBrush(Colors.White);
            res["BackdropRadialBrush"] = new SolidColorBrush(Colors.White);
            res["BgPanelBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#F8FFFFFF"));
            res["BgCardBrush"] = new SolidColorBrush(Colors.White);
            res["GlassBorderBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#CC000000"));
            res["CyanGradientBrush"] = new LinearGradientBrush(
                (Color)ColorConverter.ConvertFromString("#000000"),
                (Color)ColorConverter.ConvertFromString("#1E293B"),
                new Point(0, 0), new Point(1, 0));

            res["BgInputBrush"] = new SolidColorBrush(Colors.White);
            res["BgSubtlePillBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#F1F5F9"));
            res["BgDockPillBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#F2FFFFFF"));
            res["GridBgBrush"] = new SolidColorBrush(Colors.White);
            res["GridRowBrush"] = new SolidColorBrush(Colors.White);
            res["GridAltRowBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#F1F5F9"));
            res["GridHeaderBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#E2E8F0"));

            res["CyanPrimaryBrush"] = _lcdPitchBlackBrush;
            res["CyanSecondaryBrush"] = _lcdPitchBlackBrush;
            res["TextPrimaryBrush"] = _lcdPitchBlackBrush;
            res["TextMutedBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#1E293B"));
            res["CyanBorderDimBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#66000000"));

            res["BtnDefaultBgBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#14000000"));
            res["BtnDefaultBorderBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#AA000000"));
            res["BtnHoverBgBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#28000000"));

            AmbientHalo.Visibility = Visibility.Collapsed;
            BtnThemeSwitch.Content = "🌙 CYBER DARK";
        }
        else
        {
            // THEME 1: Original Cyber-Ice Dark Mode
            res["BgDeepNavyBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#05070B"));

            var radial = new RadialGradientBrush { Center = new Point(0.5, 0.5), RadiusX = 0.75, RadiusY = 0.75 };
            radial.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#0D1526"), 0.0));
            radial.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#070A12"), 0.55));
            radial.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#040508"), 1.0));
            res["BackdropRadialBrush"] = radial;

            var panelGrad = new LinearGradientBrush { StartPoint = new Point(0, 0), EndPoint = new Point(1, 1) };
            panelGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#F00E1525"), 0.0));
            panelGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#F5080C14"), 1.0));
            res["BgPanelBrush"] = panelGrad;

            var cardGrad = new LinearGradientBrush { StartPoint = new Point(0, 0), EndPoint = new Point(0, 1) };
            cardGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#CC162033"), 0.0));
            cardGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#CC0F1624"), 1.0));
            res["BgCardBrush"] = cardGrad;

            var borderGrad = new LinearGradientBrush { StartPoint = new Point(0, 0), EndPoint = new Point(1, 1) };
            borderGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#5538BDF8"), 0.0));
            borderGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#1838BDF8"), 0.5));
            borderGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#4400F0FF"), 1.0));
            res["GlassBorderBrush"] = borderGrad;

            var cyanGrad = new LinearGradientBrush { StartPoint = new Point(0, 0), EndPoint = new Point(1, 0) };
            cyanGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#0284C7"), 0.0));
            cyanGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#38BDF8"), 0.5));
            cyanGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#00F0FF"), 1.0));
            res["CyanGradientBrush"] = cyanGrad;

            res["BgInputBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#090D16"));
            res["BgSubtlePillBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#090D16"));
            res["BgDockPillBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#CC090D16"));
            res["GridBgBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#080B12"));
            res["GridRowBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#0B101B"));
            res["GridAltRowBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#0F1624"));
            res["GridHeaderBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#131C2E"));

            res["CyanPrimaryBrush"] = _iceBlueBrush;
            res["CyanSecondaryBrush"] = _quantumCyanBrush;
            res["TextPrimaryBrush"] = _frostWhiteBrush;
            res["TextMutedBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#64748B"));
            res["CyanBorderDimBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#2838BDF8"));

            res["BtnDefaultBgBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#1638BDF8"));
            res["BtnDefaultBorderBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#4538BDF8"));
            res["BtnHoverBgBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#2E38BDF8"));

            AmbientHalo.Visibility = Visibility.Visible;
            BtnThemeSwitch.Content = "☀ LCD THEME";
        }

        UpdateSphereThemeBrushes();
        ApplySystemStatus(_currentState, true, "Qwen 3.5 9B", false, _isMuted, "local", TxtPairingPin.Text);
    }

    private void UpdateSphereThemeBrushes()
    {
        if (_outerOrbitRing != null)
        {
            _outerOrbitRing.Stroke = _isLightLcdTheme ? _lcdPitchBlackBrush : _iceBlueBrush;
            _outerOrbitRing.StrokeThickness = _isLightLcdTheme ? 1.6 : 1.0;
            _outerOrbitRing.Opacity = _isLightLcdTheme ? 0.55 : 0.22;
        }

        if (_innerOrbitRing != null)
        {
            _innerOrbitRing.Stroke = _isLightLcdTheme ? _lcdPitchBlackBrush : _quantumCyanBrush;
            _innerOrbitRing.StrokeThickness = _isLightLcdTheme ? 1.5 : 1.0;
            _innerOrbitRing.Opacity = _isLightLcdTheme ? 0.45 : 0.18;
        }

        foreach (var line in _synapseLines)
        {
            line.Stroke = _isLightLcdTheme ? _lcdPitchBlackBrush : _quantumCyanBrush;
            line.StrokeThickness = _isLightLcdTheme ? 1.25 : 0.85;
        }

        foreach (var s in _stars)
        {
            if (_isLightLcdTheme)
            {
                // Pitch Black & Deep Carbon dots for maximum opacity on Transparent LCD
                s.Dot.Fill = s.ColorTier == 0
                    ? _lcdPitchBlackBrush
                    : (s.ColorTier == 1 ? _lcdPitchBlackBrush : _lcdDeepCarbonBrush);
            }
            else
            {
                s.Dot.Fill = s.ColorTier == 0
                    ? _frostWhiteBrush
                    : (s.ColorTier == 1 ? _quantumCyanBrush : _iceBlueBrush);
            }
        }
    }

    // ==================== CUSTOM WINDOW CHROME, MAXIMIZE & FULLSCREEN ====================

    private void TopHudBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ClickCount == 2)
        {
            BtnMaximize_Click(sender, e);
            return;
        }
        if (e.LeftButton == MouseButtonState.Pressed && !_isWorkAreaMaximized && !_isFullScreen)
        {
            DragMove();
        }
    }

    private void MainWindow_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.F11)
        {
            e.Handled = true;
            ToggleFullScreen();
        }
        else if (e.Key == Key.F10)
        {
            e.Handled = true;
            ToggleLcdTheme();
        }
        else if (e.Key == Key.Escape && _isFullScreen)
        {
            e.Handled = true;
            ToggleFullScreen();
        }
    }

    private void BtnMinimize_Click(object sender, RoutedEventArgs e) => WindowState = WindowState.Minimized;

    private void BtnMaximize_Click(object sender, RoutedEventArgs e)
    {
        if (_isFullScreen)
        {
            ExitFullScreenToNormal();
        }

        if (!_isWorkAreaMaximized)
        {
            _restoreBounds = new Rect(Left, Top, Width, Height);
            Rect workArea = SystemParameters.WorkArea;
            Left = workArea.Left;
            Top = workArea.Top;
            Width = workArea.Width;
            Height = workArea.Height;
            ResizeMode = ResizeMode.NoResize;
            _isWorkAreaMaximized = true;
            BtnMaximize.Content = "❐";
        }
        else
        {
            Left = _restoreBounds.Left;
            Top = _restoreBounds.Top;
            Width = _restoreBounds.Width;
            Height = _restoreBounds.Height;
            ResizeMode = ResizeMode.CanResize;
            _isWorkAreaMaximized = false;
            BtnMaximize.Content = "☐";
        }
    }

    private void BtnFullScreen_Click(object sender, RoutedEventArgs e) => ToggleFullScreen();

    private void ToggleFullScreen()
    {
        if (!_isFullScreen)
        {
            if (!_isWorkAreaMaximized)
            {
                _restoreBounds = new Rect(Left, Top, Width, Height);
            }

            WindowChrome.SetWindowChrome(this, null);
            WindowState = WindowState.Normal;
            ResizeMode = ResizeMode.NoResize;
            Left = 0;
            Top = 0;
            Width = SystemParameters.PrimaryScreenWidth;
            Height = SystemParameters.PrimaryScreenHeight;
            WindowState = WindowState.Maximized;
            OuterFrameBorder.BorderThickness = new Thickness(0);

            _isFullScreen = true;
            BtnFullScreen.Foreground = _isLightLcdTheme ? _lcdPitchBlackBrush : _quantumCyanBrush;
        }
        else
        {
            ExitFullScreenToNormal();
            if (_isWorkAreaMaximized)
            {
                Rect workArea = SystemParameters.WorkArea;
                Left = workArea.Left;
                Top = workArea.Top;
                Width = workArea.Width;
                Height = workArea.Height;
                ResizeMode = ResizeMode.NoResize;
            }
            else
            {
                Left = _restoreBounds.Left;
                Top = _restoreBounds.Top;
                Width = _restoreBounds.Width;
                Height = _restoreBounds.Height;
                ResizeMode = ResizeMode.CanResize;
            }
        }
    }

    private void ExitFullScreenToNormal()
    {
        WindowState = WindowState.Normal;
        WindowChrome.SetWindowChrome(this, _defaultWindowChrome);
        OuterFrameBorder.BorderThickness = new Thickness(1);
        _isFullScreen = false;
        BtnFullScreen.Foreground = (Brush)FindResource("TextMutedBrush");
    }

    private void BtnClose_Click(object sender, RoutedEventArgs e) => Close();

    // ==================== 3D COSMIC SPHERE & ORBITAL HUD ENGINE (740 STARS) ====================

    private void InitializeCosmicSphere()
    {
        CosmicSphereCanvas.Children.Clear();
        _stars.Clear();
        _listeningWaves.Clear();
        _synapseLines.Clear();

        double cx = CosmicSphereCanvas.Width * 0.5;
        double cy = CosmicSphereCanvas.Height * 0.5;

        // 1. Rotating Outer & Inner Telemetry Orbital Rings
        _outerOrbitRotate = new RotateTransform(0, 300, 300);
        _outerOrbitRing = new Ellipse
        {
            Width = 600,
            Height = 600,
            Stroke = _iceBlueBrush,
            StrokeThickness = 1,
            StrokeDashArray = new DoubleCollection { 10, 6, 2, 6 },
            Opacity = 0.22,
            RenderTransform = _outerOrbitRotate,
            IsHitTestVisible = false
        };
        Canvas.SetLeft(_outerOrbitRing, cx - 300);
        Canvas.SetTop(_outerOrbitRing, cy - 300);
        CosmicSphereCanvas.Children.Add(_outerOrbitRing);

        _innerOrbitRotate = new RotateTransform(0, 260, 260);
        _innerOrbitRing = new Ellipse
        {
            Width = 520,
            Height = 520,
            Stroke = _quantumCyanBrush,
            StrokeThickness = 1,
            StrokeDashArray = new DoubleCollection { 4, 14 },
            Opacity = 0.18,
            RenderTransform = _innerOrbitRotate,
            IsHitTestVisible = false
        };
        Canvas.SetLeft(_innerOrbitRing, cx - 260);
        Canvas.SetTop(_innerOrbitRing, cy - 260);
        CosmicSphereCanvas.Children.Add(_innerOrbitRing);

        // 2. Concentric Expanding Wave Rings (Visible during LISTENING / SPEAKING)
        for (int w = 0; w < 3; w++)
        {
            var ring = new Ellipse
            {
                Stroke = _quantumCyanBrush,
                StrokeThickness = 1.8,
                Opacity = 0.0,
                IsHitTestVisible = false
            };
            CosmicSphereCanvas.Children.Add(ring);
            _listeningWaves.Add(ring);
        }

        // 3. Neural Synapse Lines (85 lines connecting active front-hemisphere nodes)
        for (int s = 0; s < SynapseCount; s++)
        {
            var line = new Line
            {
                Stroke = _quantumCyanBrush,
                StrokeThickness = 0.85,
                Opacity = 0.0,
                IsHitTestVisible = false
            };
            CosmicSphereCanvas.Children.Add(line);
            _synapseLines.Add(line);
        }

        // 4. Fibonacci Sphere distribution for 740 larger, high-visibility star particles
        double goldenRatio = (1.0 + Math.Sqrt(5.0)) / 2.0;
        var rng = new Random(42);

        for (int i = 0; i < StarCount; i++)
        {
            double theta = 2.0 * Math.PI * i / goldenRatio;
            double phi = Math.Acos(1.0 - 2.0 * (i + 0.5) / StarCount);

            double bx = Math.Cos(theta) * Math.Sin(phi);
            double by = Math.Sin(theta) * Math.Sin(phi);
            double bz = Math.Cos(phi);

            // Larger particle sizes (2.8px to 5.6px) for strong visibility on Transparent Case LCD
            double size = (i % 9 == 0) ? 5.6 : (i % 3 == 0 ? 4.0 : 2.8);
            int colorTier = (i % 7 == 0) ? 0 : (i % 3 == 0 ? 1 : 2);

            var dot = new Ellipse
            {
                Width = size,
                Height = size,
                Fill = colorTier == 0 ? _frostWhiteBrush : (colorTier == 1 ? _quantumCyanBrush : _iceBlueBrush),
                IsHitTestVisible = false
            };

            CosmicSphereCanvas.Children.Add(dot);
            _stars.Add(new StarParticle
            {
                BaseX = bx,
                BaseY = by,
                BaseZ = bz,
                BaseSize = size,
                PhaseOffset = rng.NextDouble() * Math.PI * 2.0,
                ColorTier = colorTier,
                Dot = dot
            });
        }
    }

    private void AnimTimer_Tick(object? sender, EventArgs e)
    {
        _phaseTime += 0.036;
        double cx = CosmicSphereCanvas.Width * 0.5;
        double cy = CosmicSphereCanvas.Height * 0.5;

        bool isListening = !_isMuted && _currentState.Contains("LISTEN");
        bool isSpeaking = !_isMuted && _currentState.Contains("SPEAK");
        bool isThinking = !_isMuted && _currentState.Contains("THINK");

        // Orbital rings counter-rotation
        if (_outerOrbitRotate != null) _outerOrbitRotate.Angle = (_outerOrbitRotate.Angle + (isThinking ? 0.45 : 0.12)) % 360.0;
        if (_innerOrbitRotate != null) _innerOrbitRotate.Angle = (_innerOrbitRotate.Angle - (isSpeaking ? 0.55 : 0.18)) % 360.0;

        // Rotation speed & target radius
        double rotSpeed = isThinking ? 0.022 : (isSpeaking ? 0.012 : (isListening ? 0.009 : 0.0045));
        _rotY += rotSpeed;
        _rotX = Math.Sin(_phaseTime * 0.25) * 0.28;

        double targetRadius = 220.0;
        if (isSpeaking)
        {
            double beat = Math.Sin(_phaseTime * 3.4) * 24.0 + Math.Sin(_phaseTime * 6.8) * 10.0;
            double rmsBoost = Math.Clamp(_currentRms * 135.0, 12.0, 75.0);
            targetRadius = 225.0 + beat + rmsBoost;
        }
        else if (isListening)
        {
            targetRadius = 215.0 + Math.Sin(_phaseTime * 2.0) * 8.0 + (_currentRms * 45.0);
        }
        else if (isThinking)
        {
            targetRadius = 205.0 + Math.Sin(_phaseTime * 4.5) * 12.0;
        }

        _currentRadius += (targetRadius - _currentRadius) * 0.18;

        // Update Listening Ripple Waves
        for (int w = 0; w < _listeningWaves.Count; w++)
        {
            var ring = _listeningWaves[w];
            if (isListening || isSpeaking)
            {
                double waveProgress = ((_phaseTime * 0.45) + (w * 0.33)) % 1.0;
                double waveRadius = _currentRadius * (0.85 + waveProgress * 0.75);
                double diameter = waveRadius * 2.0;
                ring.Width = diameter;
                ring.Height = diameter;
                Canvas.SetLeft(ring, cx - waveRadius);
                Canvas.SetTop(ring, cy - waveRadius);
                double baseWaveAlpha = _isLightLcdTheme ? 0.85 : (isListening ? 0.55 : 0.30);
                ring.Opacity = (1.0 - waveProgress) * baseWaveAlpha;
                ring.Stroke = _isLightLcdTheme ? _lcdPitchBlackBrush : (isListening ? _quantumCyanBrush : _iceBlueBrush);
                ring.StrokeThickness = _isLightLcdTheme ? 2.4 : 1.8;
            }
            else
            {
                ring.Opacity = 0.0;
            }
        }

        double cosY = Math.Cos(_rotY), sinY = Math.Sin(_rotY);
        double cosX = Math.Cos(_rotX), sinX = Math.Sin(_rotX);
        double lcdScaleBoost = _isLightLcdTheme ? 1.16 : 1.0;

        // Project 740 3D star particles with surface wave distortion
        foreach (var s in _stars)
        {
            double localR = _currentRadius;

            if (isListening)
            {
                double ripple = Math.Sin(s.BaseY * 7.0 - _phaseTime * 4.2) * (12.0 + _currentRms * 40.0);
                localR += ripple;
            }
            else if (isSpeaking)
            {
                double pulse = Math.Cos(s.BaseX * 5.0 + s.BaseZ * 5.0 + _phaseTime * 5.0) * (10.0 + _currentRms * 50.0);
                localR += pulse;
            }

            double x = s.BaseX * localR;
            double y = s.BaseY * localR;
            double z = s.BaseZ * localR;

            double rx = x * cosY - z * sinY;
            double rz1 = x * sinY + z * cosY;
            double ry = y * cosX - rz1 * sinX;
            double rz = y * sinX + rz1 * cosX;

            double depth = (rz / (localR + 1.0));
            double perspective = 520.0 / (520.0 - rz * 0.65);

            double sx = cx + rx * perspective;
            double sy = cy + ry * perspective;

            s.ScreenX = sx;
            s.ScreenY = sy;
            s.Depth = depth;

            double twinkle = 0.88 + 0.12 * Math.Sin(_phaseTime * 2.2 + s.PhaseOffset);
            double minAlpha = _isLightLcdTheme ? 0.25 : 0.08;
            double alphaBoost = _isLightLcdTheme ? 0.62 : 0.46;
            double alpha = Math.Clamp(((depth + 1.18) * alphaBoost) * twinkle, minAlpha, 1.0);

            double dotScale = Math.Clamp((depth + 1.55) * 0.58, 0.50, 1.55) * lcdScaleBoost;
            if (isSpeaking)
            {
                dotScale *= 1.0 + Math.Clamp(_currentRms * 0.45, 0.05, 0.35);
            }

            s.Dot.Width = s.BaseSize * dotScale;
            s.Dot.Height = s.BaseSize * dotScale;
            s.Dot.Opacity = alpha;

            Canvas.SetLeft(s.Dot, sx - s.Dot.Width * 0.5);
            Canvas.SetTop(s.Dot, sy - s.Dot.Height * 0.5);
        }

        // Update Neural Synapse Web between neighboring front-facing stars
        for (int i = 0; i < SynapseCount; i++)
        {
            var line = _synapseLines[i];
            var a = _stars[(i * 8) % StarCount];
            var b = _stars[(i * 8 + 1) % StarCount];

            if (a.Depth > 0.12 && b.Depth > 0.12)
            {
                line.X1 = a.ScreenX;
                line.Y1 = a.ScreenY;
                line.X2 = b.ScreenX;
                line.Y2 = b.ScreenY;
                double baseOpacity = _isLightLcdTheme
                    ? ((isThinking || isSpeaking) ? 0.65 : 0.35)
                    : ((isThinking || isSpeaking) ? 0.32 : 0.14);
                line.Opacity = Math.Min(a.Depth, b.Depth) * baseOpacity;
            }
            else
            {
                line.Opacity = 0.0;
            }
        }

        TxtClock.Text = DateTime.Now.ToString("HH:mm:ss");
    }

    // ==================== SLIDE-OUT DRAWER ANIMATIONS ====================

    private void AnimateDrawer(TranslateTransform transform, DependencyProperty prop, double toValue)
    {
        var anim = new DoubleAnimation
        {
            To = toValue,
            Duration = TimeSpan.FromMilliseconds(300),
            EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut }
        };
        transform.BeginAnimation(prop, anim);
    }

    private void BtnToggleLeftDrawer_Click(object sender, RoutedEventArgs e)
    {
        _leftOpen = !_leftOpen;
        AnimateDrawer(LeftDrawerTransform, TranslateTransform.XProperty, _leftOpen ? 0 : -410);
        BtnLeftHandle.Visibility = _leftOpen ? Visibility.Collapsed : Visibility.Visible;
    }

    private void BtnToggleRightDrawer_Click(object sender, RoutedEventArgs e)
    {
        _rightOpen = !_rightOpen;
        AnimateDrawer(RightDrawerTransform, TranslateTransform.XProperty, _rightOpen ? 0 : 540);
        BtnRightHandle.Visibility = _rightOpen ? Visibility.Collapsed : Visibility.Visible;
    }

    private void BtnToggleBottomDrawer_Click(object sender, RoutedEventArgs e)
    {
        _bottomOpen = !_bottomOpen;
        AnimateDrawer(BottomDrawerTransform, TranslateTransform.YProperty, _bottomOpen ? 0 : 400);
        BtnBottomHandle.Visibility = _bottomOpen ? Visibility.Collapsed : Visibility.Visible;
    }

    private void CosmicBackdrop_MouseDown(object sender, MouseButtonEventArgs e)
    {
        if (_leftOpen) BtnToggleLeftDrawer_Click(sender, e);
        if (_rightOpen) BtnToggleRightDrawer_Click(sender, e);
        if (_bottomOpen) BtnToggleBottomDrawer_Click(sender, e);
    }

    // ==================== LIFECYCLE & BACKEND SYNC ====================

    private async void MainWindow_Loaded(object sender, RoutedEventArgs e)
    {
        RefreshStartupButtonState();
        _client.StartBackgroundConnection();

        _chatMessages.Add(new ChatDisplayItem
        {
            SenderLabel = "C.E.L.E.S.T.E.",
            Content = "سازه سایبرنتیک سلست آماده به کار است. کالبد پردازشی و ارتباط شبکه در وضعیت پایدار قرار دارند.",
            Timestamp = DateTime.Now.ToString("HH:mm:ss")
        });

        await PollBackendSubsystemsAsync();
        await LoadHistoryAndFactsAsync();
        await RefreshSecurityAuditAsync();
        await RefreshLogsAsync();
    }

    private async Task PollBackendSubsystemsAsync()
    {
        var status = await _client.GetStatusAsync();
        if (status != null)
        {
            ApplySystemStatus(
                status.State,
                status.ModelLoaded,
                status.ModelName,
                status.LumiaConnected,
                status.Muted,
                status.ActiveMic,
                status.PairingPin);
        }

        var telemetry = await _client.GetTelemetryAsync();
        if (telemetry != null)
        {
            ApplyTelemetry(telemetry);
        }

        var pending = await _client.GetPendingConfirmationsAsync();
        UpdatePendingConfirmationsBanner(pending);
    }

    private void HandleWsConnectionState(bool connected)
    {
        Dispatcher.InvokeAsync(() =>
        {
            TxtWsLink.Text = connected ? "ONLINE (WS 8765)" : "RECONNECTING...";
            TxtWsLink.Foreground = connected
                ? (_isLightLcdTheme ? _lcdPitchBlackBrush : _quantumCyanBrush)
                : _amberGlitchBrush;
        });
    }

    private void HandleHudUpdateFromWs(HudUpdateMessage msg)
    {
        Dispatcher.InvokeAsync(() =>
        {
            _currentRms = Math.Clamp(msg.Rms, 0.0, 1.0);
            TxtRmsValue.Text = _currentRms.ToString("0.000");

            ApplySystemStatus(
                msg.State,
                msg.ModelLoaded,
                "Qwen 3.5 9B",
                msg.LumiaConnected,
                msg.Muted,
                msg.ActiveMic,
                _client.PairingPin);

            var lastText = msg.ExtractLastMessageText();
            if (!string.IsNullOrWhiteSpace(lastText) && lastText != _lastSeenWsMessage && lastText != "wake_trigger")
            {
                _lastSeenWsMessage = lastText;
                AppendChatMessage("C.E.L.E.S.T.E.", lastText);
            }
        });
    }

    private void ApplySystemStatus(
        string state,
        bool modelLoaded,
        string modelName,
        bool lumiaConnected,
        bool muted,
        string activeMic,
        string pairingPin)
    {
        _currentState = string.IsNullOrWhiteSpace(state) ? "IDLE" : state.ToUpperInvariant();
        _isMuted = muted;

        TxtSystemState.Text = _isMuted ? "MUTED" : _currentState;
        TxtPairingPin.Text = string.IsNullOrWhiteSpace(pairingPin) ? "------" : pairingPin;

        Brush activeAccent = _isLightLcdTheme ? _lcdPitchBlackBrush : _quantumCyanBrush;
        Brush idleAccent = _isLightLcdTheme ? _lcdPitchBlackBrush : _iceBlueBrush;

        Brush stateBrush = idleAccent;
        if (_isMuted)
        {
            stateBrush = _crimsonCoreBrush;
            HaloInnerStop.Color = (Color)ColorConverter.ConvertFromString("#22F43F5E");
            TxtReactorSubtitle.Text = "AUDIO INPUT MUTED // ورودی صوتی غیرفعال است";
        }
        else if (_currentState.Contains("LISTEN"))
        {
            stateBrush = activeAccent;
            HaloInnerStop.Color = (Color)ColorConverter.ConvertFromString("#3800F0FF");
            TxtReactorSubtitle.Text = "LISTENING // در حال دریافت امواج صوتی...";
        }
        else if (_currentState.Contains("THINK"))
        {
            stateBrush = _isLightLcdTheme ? _lcdDarkCobaltBrush : _amberGlitchBrush;
            HaloInnerStop.Color = (Color)ColorConverter.ConvertFromString("#2DF59E0B");
            TxtReactorSubtitle.Text = "TACTICAL ANALYSIS // در حال پردازش منطقی در هسته Qwen 3.5...";
        }
        else if (_currentState.Contains("SPEAK"))
        {
            stateBrush = activeAccent;
            HaloInnerStop.Color = (Color)ColorConverter.ConvertFromString("#4238BDF8");
            TxtReactorSubtitle.Text = "VOCAL SYNTHESIS // سلست در حال پاسخگویی...";
        }
        else
        {
            HaloInnerStop.Color = (Color)ColorConverter.ConvertFromString("#2238BDF8");
            TxtReactorSubtitle.Text = "CYBERNETIC ENGINE ONLINE";
        }

        StateLed.Fill = stateBrush;
        TxtSystemState.Foreground = stateBrush;
        StateBadgeBorder.BorderBrush = stateBrush;
        TxtCenterStateTitle.Foreground = stateBrush;

        TxtModelStatus.Text = modelLoaded
            ? $"{modelName} // CUDA ONLINE"
            : $"{modelName} // STANDBY";
        TxtModelStatus.Foreground = modelLoaded ? activeAccent : _amberGlitchBrush;

        LumiaLed.Fill = lumiaConnected ? activeAccent : (Brush)FindResource("TextMutedBrush");
        TxtLumiaStatus.Text = lumiaConnected ? "LUMIA 950 XL: ONLINE" : "LUMIA: STANDBY";
        TxtLumiaStatus.Foreground = lumiaConnected ? activeAccent : (Brush)FindResource("TextMutedBrush");

        bool isNetMic = string.Equals(activeMic, "network", StringComparison.OrdinalIgnoreCase);
        TxtActiveMic.Text = isNetMic ? "LUMIA 950 XL STREAM" : "LOCAL WORKSTATION MIC";

        BtnMasterMute.Content = _isMuted ? "MIC MUTED" : "MIC ACTIVE";
        BtnMasterMute.Style = (Style)FindResource(_isMuted ? "HoloDangerButtonStyle" : "HoloSuccessButtonStyle");
    }

    private void ApplyTelemetry(TelemetryModel t)
    {
        BarCpuLoad.Value = Math.Clamp(t.CpuPercent, 0, 100);
        TxtCpuPercent.Text = $"{t.CpuPercent:0.0}%";

        BarGpuLoad.Value = Math.Clamp(t.GpuPercent, 0, 100);
        TxtGpuPercent.Text = $"{t.GpuPercent:0.0}% | {t.GpuTempC:0}°C";

        double vramTotal = t.VramTotalMb > 0 ? t.VramTotalMb : 10240;
        BarVramUsage.Maximum = vramTotal;
        BarVramUsage.Value = Math.Clamp(t.VramUsedMb, 0, vramTotal);
        TxtVramUsage.Text = $"{t.VramUsedMb:0} / {vramTotal:0} MB";

        double ramTotal = t.RamTotalGb > 0 ? t.RamTotalGb : 32.0;
        BarRamUsage.Maximum = ramTotal;
        BarRamUsage.Value = Math.Clamp(t.RamUsedGb, 0, ramTotal);
        TxtRamUsage.Text = $"{t.RamUsedGb:0.0} / {ramTotal:0.0} GB";
    }

    private void UpdatePendingConfirmationsBanner(List<PendingConfirmationItem> pending)
    {
        if (pending.Count > 0)
        {
            var first = pending[0];
            _activePendingActionId = first.ActionId;
            TxtPendingActionDetails.Text = $"[{first.RiskLevel}] {first.CommandType} — {first.PayloadDisplay}";
            SecurityBanner.Visibility = Visibility.Visible;
        }
        else
        {
            _activePendingActionId = null;
            SecurityBanner.Visibility = Visibility.Collapsed;
        }
    }

    private void AppendChatMessage(string sender, string content)
    {
        _chatMessages.Add(new ChatDisplayItem
        {
            SenderLabel = sender,
            Content = content,
            Timestamp = DateTime.Now.ToString("HH:mm:ss")
        });
        if (LstConversation.Items.Count > 0)
        {
            LstConversation.ScrollIntoView(LstConversation.Items[^1]);
        }
    }

    private async Task LoadHistoryAndFactsAsync()
    {
        var history = await _client.GetMemoryHistoryAsync();
        if (history.Count > 0)
        {
            _chatMessages.Clear();
            foreach (var item in history.TakeLast(30))
            {
                string label = string.Equals(item.Role, "user", StringComparison.OrdinalIgnoreCase)
                    ? "USER"
                    : "C.E.L.E.S.T.E.";
                _chatMessages.Add(new ChatDisplayItem
                {
                    SenderLabel = label,
                    Content = item.Content,
                    Timestamp = item.Timestamp
                });
            }
            if (LstConversation.Items.Count > 0)
                LstConversation.ScrollIntoView(LstConversation.Items[^1]);
        }

        GridFacts.ItemsSource = await _client.GetFactsAsync();
    }

    // ==================== COMMAND & UI HANDLERS ====================

    private async void BtnMasterMute_Click(object sender, RoutedEventArgs e)
    {
        bool targetMute = !_isMuted;
        var res = await _client.SetMuteAsync(targetMute);
        _isMuted = res ?? targetMute;
        await _client.SendWsCommandAsync("mute_toggle", new { muted = _isMuted });
        await PollBackendSubsystemsAsync();
    }

    private async void BtnWakeTrigger_Click(object sender, RoutedEventArgs e)
    {
        await _client.SendWsCommandAsync("wake_trigger", new { source = "desktop" });
        TxtStatusBar.Text = "C.E.L.E.S.T.E. Wake Trigger Activated.";
        await PollBackendSubsystemsAsync();
    }

    private async void BtnRefreshTelemetry_Click(object sender, RoutedEventArgs e)
    {
        await PollBackendSubsystemsAsync();
        TxtStatusBar.Text = "Workstation telemetry synchronized.";
    }

    private async void BtnSendChat_Click(object sender, RoutedEventArgs e)
    {
        await SubmitChatInputAsync();
    }

    private async void TxtChatInput_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter)
        {
            e.Handled = true;
            await SubmitChatInputAsync();
        }
    }

    private async Task SubmitChatInputAsync()
    {
        string text = TxtChatInput.Text.Trim();
        if (string.IsNullOrWhiteSpace(text)) return;

        TxtChatInput.Text = string.Empty;
        AppendChatMessage("USER", text);
        TxtChatStatus.Text = "PROCESSING...";

        var reply = await _client.SendChatAsync(text, "desktop");
        if (reply != null && !string.IsNullOrWhiteSpace(reply.EffectiveText))
        {
            _lastSeenWsMessage = reply.EffectiveText;
            AppendChatMessage("C.E.L.E.S.T.E.", reply.EffectiveText);
            TxtChatStatus.Text = "ONLINE";
        }
        else
        {
            await _client.SendWsCommandAsync("text_command", new { text });
            TxtChatStatus.Text = "SENT VIA WS";
        }

        await PollBackendSubsystemsAsync();
    }

    private async void BtnApproveAction_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_activePendingActionId)) return;
        string id = _activePendingActionId;
        await _client.ConfirmSecurityActionAsync(id, true);
        await _client.SendWsCommandAsync("confirm_action", new { action_id = id, approved = true });
        await PollBackendSubsystemsAsync();
        await RefreshSecurityAuditAsync();
    }

    private async void BtnRejectAction_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_activePendingActionId)) return;
        string id = _activePendingActionId;
        await _client.ConfirmSecurityActionAsync(id, false);
        await PollBackendSubsystemsAsync();
        await RefreshSecurityAuditAsync();
    }

    private void VoiceSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (!IsLoaded) return;
        LblPitchVal.Text = SldPitch.Value.ToString("0.0");
        LblMetallicVal.Text = SldMetallic.Value.ToString("0.00");
        LblReverbVal.Text = SldReverb.Value.ToString("0.00");
        LblWakeVal.Text = SldWakeThreshold.Value.ToString("0.00");
    }

    private async void BtnApplyVoiceConfig_Click(object sender, RoutedEventArgs e)
    {
        var cfg = new VoiceConfigModel
        {
            PitchSemitones = SldPitch.Value,
            MetallicMix = SldMetallic.Value,
            ReverbDecay = SldReverb.Value,
            WakeThreshold = SldWakeThreshold.Value
        };
        bool ok = await _client.UpdateVoiceConfigAsync(cfg);
        TxtVoiceConfigResult.Text = ok
            ? $"[OK] Cyber-Ice Vocal Chain applied ({DateTime.Now:HH:mm:ss})"
            : "[WARN] Failed to update vocal parameters";
    }

    private async void BtnTestVoice_Click(object sender, RoutedEventArgs e)
    {
        var res = await _client.SendChatAsync("سلست، وضعیت کالبد پردازشی و سیستم صوتی خودت را گزارش بده.", "desktop");
        if (res != null && !string.IsNullOrWhiteSpace(res.EffectiveText))
        {
            AppendChatMessage("C.E.L.E.S.T.E.", res.EffectiveText);
        }
    }

    private async void BtnRefreshFacts_Click(object sender, RoutedEventArgs e)
    {
        GridFacts.ItemsSource = await _client.GetFactsAsync();
    }

    private async void BtnAddFact_Click(object sender, RoutedEventArgs e)
    {
        string category = string.IsNullOrWhiteSpace(TxtFactCategory.Text) ? "user" : TxtFactCategory.Text.Trim();
        string key = TxtFactKey.Text.Trim();
        string val = TxtFactValue.Text.Trim();
        if (string.IsNullOrWhiteSpace(key) || string.IsNullOrWhiteSpace(val)) return;

        int imp = int.TryParse(TxtFactImportance.Text.Trim(), out int parsed) ? Math.Clamp(parsed, 1, 10) : 5;
        if (await _client.SaveFactAsync(category, key, val, imp))
        {
            TxtFactKey.Text = string.Empty;
            TxtFactValue.Text = string.Empty;
            GridFacts.ItemsSource = await _client.GetFactsAsync();
        }
    }

    private async void BtnDeleteFact_Click(object sender, RoutedEventArgs e)
    {
        if (GridFacts.SelectedItem is FactItem selected && await _client.DeleteFactAsync(selected.Id))
        {
            GridFacts.ItemsSource = await _client.GetFactsAsync();
        }
    }

    private async void BtnClearHistory_Click(object sender, RoutedEventArgs e)
    {
        if (await _client.ClearMemoryHistoryAsync())
        {
            _chatMessages.Clear();
        }
    }

    private void RefreshStartupButtonState()
    {
        bool enabled = _client.IsWindowsStartupEnabled();
        TxtStartupState.Text = enabled ? "REGISTERED IN LOGON" : "NOT REGISTERED";
        BtnToggleStartup.Content = enabled ? "DISABLE" : "ENABLE";
        BtnToggleStartup.Style = (Style)FindResource(enabled ? "HoloDangerButtonStyle" : "HoloSuccessButtonStyle");
    }

    private void BtnToggleStartup_Click(object sender, RoutedEventArgs e)
    {
        _client.SetWindowsStartupEnabled(!_client.IsWindowsStartupEnabled());
        RefreshStartupButtonState();
    }

    private async void BtnRunDiagnostics_Click(object sender, RoutedEventArgs e)
    {
        bool healthOk = await _client.GetHealthAsync();
        var status = await _client.GetStatusAsync();
        var telemetry = await _client.GetTelemetryAsync();

        TxtDiagnosticsOutput.Text =
            $"[REST API]: {(healthOk ? "ONLINE" : "OFFLINE")} | [WS]: {(_client.IsWsConnected ? "CONNECTED" : "DISCONNECTED")}\n" +
            $"[CORE]: {(status?.ModelLoaded == true ? status.ModelName : "STANDBY")} | [GPU]: {(telemetry != null ? $"{telemetry.GpuPercent:0.0}% ({telemetry.VramUsedMb:0}MB)" : "N/A")}";
    }

    private async void BtnRefreshAudit_Click(object sender, RoutedEventArgs e) => await RefreshSecurityAuditAsync();

    private async Task RefreshSecurityAuditAsync() => GridSecurityAudit.ItemsSource = await _client.GetSecurityAuditAsync();

    private async void BtnRefreshLogs_Click(object sender, RoutedEventArgs e) => await RefreshLogsAsync();

    private void CmbLogSeverity_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (IsLoaded) ApplyLogFilter();
    }

    private async Task RefreshLogsAsync()
    {
        _cachedLogs = await _client.GetLogsAsync();
        ApplyLogFilter();
    }

    private void ApplyLogFilter()
    {
        string filter = (CmbLogSeverity.SelectedItem as ComboBoxItem)?.Content?.ToString() ?? "ALL";
        var filtered = filter == "ALL"
            ? _cachedLogs
            : _cachedLogs.Where(l => string.Equals(l.Level, filter, StringComparison.OrdinalIgnoreCase));
        LstLogs.ItemsSource = filtered.Select(l => $"[{l.Timestamp}] [{l.Level}] ({l.Module}) {l.Message}").ToList();
    }
}