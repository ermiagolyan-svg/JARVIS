using System.Collections.ObjectModel;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
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
    private const string ConfigFilePath = @"D:\AI\JARVIS\Configuration\config.json";

    private readonly JarvisBackendClient _client;
    private readonly DispatcherTimer _animTimer;
    private readonly DispatcherTimer _pollTimer;
    private readonly ObservableCollection<ChatDisplayItem> _chatMessages = new();
    private List<LogEntryItem> _cachedLogs = new();

    // Window Maximize (WorkArea) & True FullScreen States
    private Rect _restoreBounds = new(120, 80, 1640, 920);
    private bool _isWorkAreaMaximized;
    private bool _isFullScreen;
    private bool _isLightLcdTheme;
    private readonly WindowChrome _defaultWindowChrome;

    // 3D Cosmic Star Sphere (740 Stars & 85 Neural Synapses)
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
    private bool _testRedAlertMode;
    private string _lastSeenWsMessage = string.Empty;

    private bool IsRedAlertActive => _testRedAlertMode || !string.IsNullOrWhiteSpace(_activePendingActionId);

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
    private readonly SolidColorBrush _crimsonBrightBrush = new((Color)ColorConverter.ConvertFromString("#FF2A55"));
    private readonly SolidColorBrush _crimsonDeepBrush = new((Color)ColorConverter.ConvertFromString("#BE123C"));

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

    // ==================== CLOUD AI & WEB SEARCH CONFIG (config.json) ====================

    private void LoadCloudAndWebConfigFromFile()
    {
        try
        {
            if (!File.Exists(ConfigFilePath)) return;
            var root = JsonNode.Parse(File.ReadAllText(ConfigFilePath, Encoding.UTF8)) as JsonObject;
            if (root == null) return;

            if (root["cloud_ai"] is JsonObject cloud)
            {
                ChkCloudEnabled.IsChecked = cloud["enabled"]?.GetValue<bool>() ?? true;
                string defProv = cloud["default_provider"]?.GetValue<string>() ?? "gemini";
                foreach (ComboBoxItem item in CmbCloudProvider.Items)
                {
                    if (string.Equals(item.Content?.ToString(), defProv, StringComparison.OrdinalIgnoreCase))
                    {
                        CmbCloudProvider.SelectedItem = item;
                        break;
                    }
                }
                TxtProxyUrl.Text = cloud["proxy_url"]?.GetValue<string>() ?? string.Empty;
                TxtGeminiKey.Text = cloud["gemini_api_key"]?.GetValue<string>() ?? string.Empty;
                TxtGeminiModel.Text = cloud["gemini_model"]?.GetValue<string>() ?? "gemini-2.5-flash";
                TxtClaudeKey.Text = cloud["claude_api_key"]?.GetValue<string>() ?? string.Empty;
                TxtClaudeModel.Text = cloud["claude_model"]?.GetValue<string>() ?? "claude-3-7-sonnet-latest";
                TxtOpenAiKey.Text = cloud["openai_api_key"]?.GetValue<string>() ?? string.Empty;
                TxtOpenAiModel.Text = cloud["openai_model"]?.GetValue<string>() ?? "gpt-4o";
            }

            if (root["web_search"] is JsonObject web)
            {
                if (string.IsNullOrWhiteSpace(TxtProxyUrl.Text))
                {
                    TxtProxyUrl.Text = web["proxy_url"]?.GetValue<string>() ?? string.Empty;
                }
                TxtSerperKey.Text = web["serper_api_key"]?.GetValue<string>() ?? string.Empty;
                TxtGoogleCseKey.Text = web["google_api_key"]?.GetValue<string>() ?? string.Empty;
                TxtGoogleCx.Text = web["google_cx"]?.GetValue<string>() ?? string.Empty;
            }
        }
        catch (Exception ex)
        {
            TxtCloudConfigResult.Text = $"[WARN] Could not read config.json: {ex.Message}";
        }
    }

    private void BtnSaveCloudConfig_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            JsonObject root;
            if (File.Exists(ConfigFilePath))
            {
                root = JsonNode.Parse(File.ReadAllText(ConfigFilePath, Encoding.UTF8)) as JsonObject ?? new JsonObject();
            }
            else
            {
                root = new JsonObject();
            }

            string selectedProvider = (CmbCloudProvider.SelectedItem as ComboBoxItem)?.Content?.ToString() ?? "gemini";
            string proxy = TxtProxyUrl.Text.Trim();

            root["cloud_ai"] = new JsonObject
            {
                ["enabled"] = ChkCloudEnabled.IsChecked == true,
                ["default_provider"] = selectedProvider,
                ["proxy_url"] = proxy,
                ["gemini_api_key"] = TxtGeminiKey.Text.Trim(),
                ["gemini_model"] = string.IsNullOrWhiteSpace(TxtGeminiModel.Text) ? "gemini-2.5-flash" : TxtGeminiModel.Text.Trim(),
                ["claude_api_key"] = TxtClaudeKey.Text.Trim(),
                ["claude_model"] = string.IsNullOrWhiteSpace(TxtClaudeModel.Text) ? "claude-3-7-sonnet-latest" : TxtClaudeModel.Text.Trim(),
                ["openai_api_key"] = TxtOpenAiKey.Text.Trim(),
                ["openai_model"] = string.IsNullOrWhiteSpace(TxtOpenAiModel.Text) ? "gpt-4o" : TxtOpenAiModel.Text.Trim()
            };

            root["web_search"] = new JsonObject
            {
                ["enabled"] = true,
                ["provider"] = "auto",
                ["proxy_url"] = proxy,
                ["serper_api_key"] = TxtSerperKey.Text.Trim(),
                ["google_api_key"] = TxtGoogleCseKey.Text.Trim(),
                ["google_cx"] = TxtGoogleCx.Text.Trim(),
                ["max_results"] = 4
            };

            var options = new JsonSerializerOptions { WriteIndented = true };
            File.WriteAllText(ConfigFilePath, root.ToJsonString(options), new UTF8Encoding(false));

            TxtCloudConfigResult.Text = $"[OK] Saved to config.json ({DateTime.Now:HH:mm:ss}) — Active immediately!";
            TxtStatusBar.Text = "Cloud AI & Web Search configuration updated.";
        }
        catch (Exception ex)
        {
            TxtCloudConfigResult.Text = $"[ERROR] Failed to save config.json: {ex.Message}";
        }
    }

    private async void BtnTestWebSearch_Click(object sender, RoutedEventArgs e)
    {
        TxtCloudConfigResult.Text = "Searching live web via C.E.L.E.S.T.E. backend...";
        var res = await _client.SendChatAsync("سلست، در گوگل سرچ کن آخرین اخبار فناوری و هوش مصنوعی چیست و خلاصه بگو.", "desktop");
        if (res != null && !string.IsNullOrWhiteSpace(res.EffectiveText))
        {
            AppendChatMessage("C.E.L.E.S.T.E.", res.EffectiveText);
            TxtCloudConfigResult.Text = $"[OK] Live web search response received ({DateTime.Now:HH:mm:ss}).";
        }
        else
        {
            TxtCloudConfigResult.Text = "[WARN] Backend did not return a response. Make sure jarvis_main.py is running.";
        }
    }

    // ==================== THEME SWITCHER (THEME 1 DARK vs THEME 2 TRANSPARENT LCD LIGHT) ====================

    private void BtnThemeSwitch_Click(object sender, RoutedEventArgs e) => ToggleLcdTheme();

    private void ToggleLcdTheme()
    {
        _isLightLcdTheme = !_isLightLcdTheme;
        var res = Application.Current.Resources;

        if (_isLightLcdTheme)
        {
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
        bool isRedAlert = IsRedAlertActive;

        if (_outerOrbitRing != null)
        {
            _outerOrbitRing.Stroke = isRedAlert
                ? _crimsonCoreBrush
                : (_isLightLcdTheme ? _lcdPitchBlackBrush : _iceBlueBrush);
            _outerOrbitRing.StrokeThickness = isRedAlert ? 1.8 : (_isLightLcdTheme ? 1.6 : 1.0);
            _outerOrbitRing.Opacity = isRedAlert ? 0.75 : (_isLightLcdTheme ? 0.55 : 0.22);
        }

        if (_innerOrbitRing != null)
        {
            _innerOrbitRing.Stroke = isRedAlert
                ? _crimsonDeepBrush
                : (_isLightLcdTheme ? _lcdPitchBlackBrush : _quantumCyanBrush);
            _innerOrbitRing.StrokeThickness = isRedAlert ? 1.6 : (_isLightLcdTheme ? 1.5 : 1.0);
            _innerOrbitRing.Opacity = isRedAlert ? 0.65 : (_isLightLcdTheme ? 0.45 : 0.18);
        }

        foreach (var line in _synapseLines)
        {
            if (isRedAlert)
            {
                line.Stroke = _isLightLcdTheme ? _crimsonDeepBrush : _crimsonCoreBrush;
                line.StrokeThickness = _isLightLcdTheme ? 1.55 : 1.25;
            }
            else
            {
                line.Stroke = _isLightLcdTheme ? _lcdPitchBlackBrush : _quantumCyanBrush;
                line.StrokeThickness = _isLightLcdTheme ? 1.25 : 0.85;
            }
        }

        foreach (var s in _stars)
        {
            if (isRedAlert)
            {
                s.Dot.Fill = s.ColorTier == 0
                    ? _crimsonBrightBrush
                    : (s.ColorTier == 1 ? _crimsonCoreBrush : _crimsonDeepBrush);
            }
            else if (_isLightLcdTheme)
            {
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
        else if (e.Key == Key.F9)
        {
            e.Handled = true;
            _testRedAlertMode = !_testRedAlertMode;
            SecurityBanner.Visibility = IsRedAlertActive ? Visibility.Visible : Visibility.Collapsed;
            if (_testRedAlertMode && string.IsNullOrWhiteSpace(_activePendingActionId))
            {
                TxtPendingActionDetails.Text = "[SENSITIVE] TEST_RED_ALERT — منتظر دستور یا تایید نهایی شما...";
            }
            UpdateSphereThemeBrushes();
            ApplySystemStatus(_currentState, true, "Qwen 3.5 9B", false, _isMuted, "local", TxtPairingPin.Text);
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

        double goldenRatio = (1.0 + Math.Sqrt(5.0)) / 2.0;
        var rng = new Random(42);

        for (int i = 0; i < StarCount; i++)
        {
            double theta = 2.0 * Math.PI * i / goldenRatio;
            double phi = Math.Acos(1.0 - 2.0 * (i + 0.5) / StarCount);

            double bx = Math.Cos(theta) * Math.Sin(phi);
            double by = Math.Sin(theta) * Math.Sin(phi);
            double bz = Math.Cos(phi);

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

        bool isRedAlert = IsRedAlertActive;
        bool isListening = !_isMuted && _currentState.Contains("LISTEN");
        bool isSpeaking = !_isMuted && _currentState.Contains("SPEAK");
        bool isThinking = !_isMuted && _currentState.Contains("THINK");

        if (_outerOrbitRotate != null)
            _outerOrbitRotate.Angle = (_outerOrbitRotate.Angle + (isRedAlert ? 0.65 : (isThinking ? 0.45 : 0.12))) % 360.0;
        if (_innerOrbitRotate != null)
            _innerOrbitRotate.Angle = (_innerOrbitRotate.Angle - (isRedAlert ? 0.75 : (isSpeaking ? 0.55 : 0.18))) % 360.0;

        double rotSpeed = isRedAlert ? 0.016 : (isThinking ? 0.022 : (isSpeaking ? 0.012 : (isListening ? 0.009 : 0.0045)));
        _rotY += rotSpeed;
        _rotX = Math.Sin(_phaseTime * 0.25) * 0.28;

        double targetRadius = 220.0;
        if (isRedAlert)
        {
            targetRadius = 224.0 + Math.Sin(_phaseTime * 5.2) * 15.0 + Math.Cos(_phaseTime * 10.4) * 6.0;
        }
        else if (isSpeaking)
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

        for (int w = 0; w < _listeningWaves.Count; w++)
        {
            var ring = _listeningWaves[w];
            if (isRedAlert || isListening || isSpeaking)
            {
                double waveSpeed = isRedAlert ? 0.60 : 0.45;
                double waveProgress = ((_phaseTime * waveSpeed) + (w * 0.33)) % 1.0;
                double waveRadius = _currentRadius * (0.85 + waveProgress * 0.75);
                double diameter = waveRadius * 2.0;
                ring.Width = diameter;
                ring.Height = diameter;
                Canvas.SetLeft(ring, cx - waveRadius);
                Canvas.SetTop(ring, cy - waveRadius);

                if (isRedAlert)
                {
                    double alertAlpha = _isLightLcdTheme ? 0.90 : 0.70;
                    ring.Opacity = (1.0 - waveProgress) * alertAlpha;
                    ring.Stroke = _isLightLcdTheme ? _crimsonDeepBrush : _crimsonCoreBrush;
                    ring.StrokeThickness = _isLightLcdTheme ? 2.6 : 2.2;
                }
                else
                {
                    double baseWaveAlpha = _isLightLcdTheme ? 0.85 : (isListening ? 0.55 : 0.30);
                    ring.Opacity = (1.0 - waveProgress) * baseWaveAlpha;
                    ring.Stroke = _isLightLcdTheme ? _lcdPitchBlackBrush : (isListening ? _quantumCyanBrush : _iceBlueBrush);
                    ring.StrokeThickness = _isLightLcdTheme ? 2.4 : 1.8;
                }
            }
            else
            {
                ring.Opacity = 0.0;
            }
        }

        double cosY = Math.Cos(_rotY), sinY = Math.Sin(_rotY);
        double cosX = Math.Cos(_rotX), sinX = Math.Sin(_rotX);
        double lcdScaleBoost = _isLightLcdTheme ? 1.16 : 1.0;

        foreach (var s in _stars)
        {
            double localR = _currentRadius;

            if (isRedAlert)
            {
                double alertRipple = Math.Sin(s.BaseX * 6.0 + s.BaseY * 6.0 - _phaseTime * 6.5) * 9.0;
                localR += alertRipple;
            }
            else if (isListening)
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

            double twinkle = 0.88 + 0.12 * Math.Sin(_phaseTime * (isRedAlert ? 5.5 : 2.2) + s.PhaseOffset);
            double minAlpha = isRedAlert ? 0.30 : (_isLightLcdTheme ? 0.25 : 0.08);
            double alphaBoost = isRedAlert ? 0.68 : (_isLightLcdTheme ? 0.62 : 0.46);
            double alpha = Math.Clamp(((depth + 1.18) * alphaBoost) * twinkle, minAlpha, 1.0);

            double dotScale = Math.Clamp((depth + 1.55) * 0.58, 0.50, 1.55) * lcdScaleBoost;
            if (isRedAlert)
            {
                dotScale *= 1.15;
            }
            else if (isSpeaking)
            {
                dotScale *= 1.0 + Math.Clamp(_currentRms * 0.45, 0.05, 0.35);
            }

            s.Dot.Width = s.BaseSize * dotScale;
            s.Dot.Height = s.BaseSize * dotScale;
            s.Dot.Opacity = alpha;

            Canvas.SetLeft(s.Dot, sx - s.Dot.Width * 0.5);
            Canvas.SetTop(s.Dot, sy - s.Dot.Height * 0.5);
        }

        double depthThreshold = isRedAlert ? 0.04 : 0.12;
        double alertPulse = isRedAlert ? (0.78 + 0.22 * Math.Sin(_phaseTime * 6.0)) : 1.0;

        for (int i = 0; i < SynapseCount; i++)
        {
            var line = _synapseLines[i];
            var a = _stars[(i * 8) % StarCount];
            var b = _stars[(i * 8 + 1) % StarCount];

            if (a.Depth > depthThreshold && b.Depth > depthThreshold)
            {
                line.X1 = a.ScreenX;
                line.Y1 = a.ScreenY;
                line.X2 = b.ScreenX;
                line.Y2 = b.ScreenY;

                double baseOpacity;
                if (isRedAlert)
                {
                    baseOpacity = (_isLightLcdTheme ? 0.88 : 0.72) * alertPulse;
                }
                else
                {
                    baseOpacity = _isLightLcdTheme
                        ? ((isThinking || isSpeaking) ? 0.65 : 0.35)
                        : ((isThinking || isSpeaking) ? 0.32 : 0.14);
                }

                line.Opacity = Math.Clamp(Math.Min(a.Depth, b.Depth) * baseOpacity, 0.0, 1.0);
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
        AnimateDrawer(RightDrawerTransform, TranslateTransform.XProperty, _rightOpen ? 0 : 560);
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
        LoadCloudAndWebConfigFromFile();
        RefreshStartupButtonState();
        _client.StartBackgroundConnection();

        _chatMessages.Add(new ChatDisplayItem
        {
            SenderLabel = "C.E.L.E.S.T.E.",
            Content = "سازه سایبرنتیک سلست آماده به کار است. کالبد پردازشی، حافظه SQLite و روتر آنلاین/ابری در وضعیت پایدار قرار دارند.",
            Timestamp = DateTime.Now.ToString("HH:mm:ss")
        });

        await PollBackendSubsystemsAsync();
        await LoadHistoryAndFactsAsync();
        await RefreshSecurityAuditAsync();
        await RefreshLogsAsync();
        _ = LoadVoiceConfigFromBackendAsync();
    }

    private async Task PollBackendSubsystemsAsync()
    {
        var pending = await _client.GetPendingConfirmationsAsync();
        UpdatePendingConfirmationsBanner(pending);

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

        bool isRedAlert = IsRedAlertActive;
        TxtSystemState.Text = isRedAlert ? "AWAITING AUTH" : (_isMuted ? "MUTED" : _currentState);
        TxtPairingPin.Text = string.IsNullOrWhiteSpace(pairingPin) ? "------" : pairingPin;

        Brush activeAccent = _isLightLcdTheme ? _lcdPitchBlackBrush : _quantumCyanBrush;
        Brush idleAccent = _isLightLcdTheme ? _lcdPitchBlackBrush : _iceBlueBrush;

        Brush stateBrush = idleAccent;
        if (isRedAlert)
        {
            stateBrush = _crimsonCoreBrush;
            HaloInnerStop.Color = (Color)ColorConverter.ConvertFromString("#48F43F5E");
            TxtReactorSubtitle.Text = "⚠ CRITICAL COMMAND PENDING // منتظر تایید دستور حساس توسط شما...";
        }
        else if (_isMuted)
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
            TxtReactorSubtitle.Text = "TACTICAL ANALYSIS // در حال پردازش منطقی در هسته...";
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
            ? $"{modelName} // HYBRID ONLINE"
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
        bool wasRedAlert = IsRedAlertActive;

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
            if (!_testRedAlertMode)
            {
                SecurityBanner.Visibility = Visibility.Collapsed;
            }
        }

        if (wasRedAlert != IsRedAlertActive)
        {
            UpdateSphereThemeBrushes();
            ApplySystemStatus(_currentState, true, "Qwen 3.5 9B", false, _isMuted, "local", TxtPairingPin.Text);
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
        GridFacts.ItemsSource = await _client.GetFactsAsync();
    }

    private async void BtnApproveAction_Click(object sender, RoutedEventArgs e)
    {
        if (_testRedAlertMode && string.IsNullOrWhiteSpace(_activePendingActionId))
        {
            _testRedAlertMode = false;
            SecurityBanner.Visibility = Visibility.Collapsed;
            UpdateSphereThemeBrushes();
            ApplySystemStatus(_currentState, true, "Qwen 3.5 9B", false, _isMuted, "local", TxtPairingPin.Text);
            return;
        }
        if (string.IsNullOrWhiteSpace(_activePendingActionId)) return;
        string id = _activePendingActionId;
        await _client.ConfirmSecurityActionAsync(id, true);
        await _client.SendWsCommandAsync("confirm_action", new { action_id = id, approved = true });
        await PollBackendSubsystemsAsync();
        await RefreshSecurityAuditAsync();
    }

    private async void BtnRejectAction_Click(object sender, RoutedEventArgs e)
    {
        if (_testRedAlertMode && string.IsNullOrWhiteSpace(_activePendingActionId))
        {
            _testRedAlertMode = false;
            SecurityBanner.Visibility = Visibility.Collapsed;
            UpdateSphereThemeBrushes();
            ApplySystemStatus(_currentState, true, "Qwen 3.5 9B", false, _isMuted, "local", TxtPairingPin.Text);
            return;
        }
        if (string.IsNullOrWhiteSpace(_activePendingActionId)) return;
        string id = _activePendingActionId;
        await _client.ConfirmSecurityActionAsync(id, false);
        await PollBackendSubsystemsAsync();
        await RefreshSecurityAuditAsync();
    }

    private async Task LoadVoiceConfigFromBackendAsync()
    {
        try
        {
            var cfg = await _client.GetVoiceConfigAsync();
            if (cfg == null) return;

            string mode = (cfg.EngineMode ?? "gemini").Trim().ToLowerInvariant();
            CmbVoiceEngineMode.SelectedIndex = (mode == "gooya" || mode == "local") ? 1 : 0;
            UpdateVoiceEnginePanelsVisibility();

            TxtVoiceOnlineApiKey.Text = cfg.OnlineApiKey ?? "";
            TxtGeminiTtsModel.Text = string.IsNullOrWhiteSpace(cfg.GeminiModel)
                ? "gemini-2.5-flash-preview-tts"
                : cfg.GeminiModel;

            string voiceName = string.IsNullOrWhiteSpace(cfg.GeminiVoiceName) ? "Kore" : cfg.GeminiVoiceName.Trim();
            for (int i = 0; i < CmbGeminiVoiceName.Items.Count; i++)
            {
                if (CmbGeminiVoiceName.Items[i] is ComboBoxItem item &&
                    string.Equals(item.Tag?.ToString(), voiceName, StringComparison.OrdinalIgnoreCase))
                {
                    CmbGeminiVoiceName.SelectedIndex = i;
                    break;
                }
            }

            TxtGooyaRefAudio.Text = string.IsNullOrWhiteSpace(cfg.GooyaReferenceAudio)
                ? "Data/Models/gooya/speaker_reference.wav"
                : cfg.GooyaReferenceAudio;
            SldGooyaExaggeration.Value = Math.Clamp(cfg.GooyaExaggeration, 0.0, 1.0);
            SldGooyaCfgWeight.Value = Math.Clamp(cfg.GooyaCfgWeight, 0.0, 1.0);

            string prec = (cfg.GooyaPrecision ?? "bf16").Trim().ToLowerInvariant();
            CmbGooyaPrecision.SelectedIndex = (prec == "fp32" || prec == "float32") ? 1 : 0;
            ChkGooyaAutoUnload.IsChecked = cfg.GooyaAutoUnload;

            double pitch = cfg.DspPitchSemitones != 0 ? cfg.DspPitchSemitones : cfg.PitchSemitones;
            double metallic = cfg.DspMetallicMix != 0 ? cfg.DspMetallicMix : cfg.MetallicMix;
            double reverb = cfg.DspReverbMix != 0 ? cfg.DspReverbMix : cfg.ReverbDecay;

            SldPitch.Value = Math.Clamp(pitch, -4.0, 2.0);
            SldMetallic.Value = Math.Clamp(metallic, 0.0, 0.50);
            SldReverb.Value = Math.Clamp(reverb, 0.0, 0.50);
            SldWakeThreshold.Value = Math.Clamp(cfg.WakeThreshold, 0.15, 0.90);
        }
        catch { }
    }

    private void CmbVoiceEngineMode_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!IsLoaded) return;
        UpdateVoiceEnginePanelsVisibility();
    }

    private void UpdateVoiceEnginePanelsVisibility()
    {
        if (PnlGeminiVoiceSettings == null || PnlGooyaVoiceSettings == null || CmbVoiceEngineMode == null)
            return;

        bool isGooya = CmbVoiceEngineMode.SelectedIndex == 1;
        PnlGeminiVoiceSettings.Visibility = isGooya ? Visibility.Collapsed : Visibility.Visible;
        PnlGooyaVoiceSettings.Visibility = isGooya ? Visibility.Visible : Visibility.Collapsed;
    }

    private void BtnBrowseGooyaRef_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new Microsoft.Win32.OpenFileDialog
        {
            Title = "Select Speaker Reference Audio (.wav) for Gooya TTS",
            Filter = "WAV Audio Files (*.wav)|*.wav|All Files (*.*)|*.*",
            CheckFileExists = true
        };
        if (dlg.ShowDialog() == true)
        {
            TxtGooyaRefAudio.Text = dlg.FileName;
        }
    }

    private void VoiceSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (!IsLoaded) return;
        if (LblPitchVal != null && SldPitch != null) LblPitchVal.Text = SldPitch.Value.ToString("0.0");
        if (LblMetallicVal != null && SldMetallic != null) LblMetallicVal.Text = SldMetallic.Value.ToString("0.00");
        if (LblReverbVal != null && SldReverb != null) LblReverbVal.Text = SldReverb.Value.ToString("0.00");
        if (LblWakeVal != null && SldWakeThreshold != null) LblWakeVal.Text = SldWakeThreshold.Value.ToString("0.00");
        if (LblGooyaExagVal != null && SldGooyaExaggeration != null) LblGooyaExagVal.Text = SldGooyaExaggeration.Value.ToString("0.00");
        if (LblGooyaCfgVal != null && SldGooyaCfgWeight != null) LblGooyaCfgVal.Text = SldGooyaCfgWeight.Value.ToString("0.00");
    }

    private async void BtnApplyVoiceConfig_Click(object sender, RoutedEventArgs e)
    {
        string engineMode = (CmbVoiceEngineMode.SelectedItem is ComboBoxItem modeItem && modeItem.Tag != null)
            ? modeItem.Tag.ToString()!
            : (CmbVoiceEngineMode.SelectedIndex == 1 ? "gooya" : "gemini");

        string geminiVoice = (CmbGeminiVoiceName.SelectedItem is ComboBoxItem voiceItem && voiceItem.Tag != null)
            ? voiceItem.Tag.ToString()!
            : "Kore";

        string gooyaPrecision = (CmbGooyaPrecision.SelectedItem is ComboBoxItem precItem && precItem.Tag != null)
            ? precItem.Tag.ToString()!
            : "bf16";

        var cfg = new VoiceConfigModel
        {
            EngineMode = engineMode,
            OnlineApiKey = TxtVoiceOnlineApiKey.Text.Trim(),
            GeminiModel = string.IsNullOrWhiteSpace(TxtGeminiTtsModel.Text) ? "gemini-2.5-flash-preview-tts" : TxtGeminiTtsModel.Text.Trim(),
            GeminiVoiceName = geminiVoice,
            GooyaModelDir = "Data/Models/gooya",
            GooyaReferenceAudio = string.IsNullOrWhiteSpace(TxtGooyaRefAudio.Text) ? "Data/Models/gooya/speaker_reference.wav" : TxtGooyaRefAudio.Text.Trim(),
            GooyaExaggeration = SldGooyaExaggeration.Value,
            GooyaCfgWeight = SldGooyaCfgWeight.Value,
            GooyaPrecision = gooyaPrecision,
            GooyaAutoUnload = ChkGooyaAutoUnload.IsChecked == true,
            PitchSemitones = SldPitch.Value,
            DspPitchSemitones = SldPitch.Value,
            MetallicMix = SldMetallic.Value,
            DspMetallicMix = SldMetallic.Value,
            ReverbDecay = SldReverb.Value,
            DspReverbMix = SldReverb.Value,
            WakeThreshold = SldWakeThreshold.Value
        };

        bool ok = await _client.UpdateVoiceConfigAsync(cfg);
        TxtVoiceConfigResult.Text = ok
            ? $"[OK] Active Engine: {engineMode.ToUpperInvariant()} | Vocal & VRAM Profile applied ({DateTime.Now:HH:mm:ss})"
            : "[WARN] Failed to update voice & engine configuration";
    }

    private async void BtnTestVoice_Click(object sender, RoutedEventArgs e)
    {
        var res = await _client.SendChatAsync("سلست، وضعیت کالبد پردازشی و موتور صوتی فعال خودت را گزارش بده.", "desktop");
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