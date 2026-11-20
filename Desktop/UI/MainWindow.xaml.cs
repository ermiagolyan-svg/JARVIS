using System.Collections.ObjectModel;
using System.Diagnostics;
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
    public bool IsUser { get; set; }

    public string DisplaySender => IsUser ? "USER" : "C.E.L.E.S.T.E.";
    public HorizontalAlignment BubbleAlignment => IsUser ? HorizontalAlignment.Right : HorizontalAlignment.Left;
    public Thickness AccentBorderThickness => IsUser ? new Thickness(1, 1, 3, 1) : new Thickness(3, 1, 1, 1);

    public string ShortTimestamp
    {
        get
        {
            if (string.IsNullOrWhiteSpace(Timestamp)) return string.Empty;
            int sp = Timestamp.IndexOf(' ');
            return sp >= 0 && sp + 1 < Timestamp.Length ? Timestamp[(sp + 1)..] : Timestamp;
        }
    }
}

public class LogDisplayItem
{
    public LogEntryItem Raw { get; init; } = new();
    public string ShortTimestamp => Raw.ShortTimestamp;
    public string Level => Raw.Level;
    public string ModuleTag => $"[{Raw.Module}]";
    public string TurnId => Raw.TurnId;
    public string Message => Raw.Message;
    public string Traceback => Raw.Traceback;
    public Visibility TurnVisibility => Raw.HasTurnId ? Visibility.Visible : Visibility.Collapsed;
    public Visibility TracebackVisibility => Raw.HasTraceback ? Visibility.Visible : Visibility.Collapsed;
    public Brush LevelBrush { get; init; } = Brushes.DeepSkyBlue;
    public Brush LevelBadgeBg { get; init; } = Brushes.Transparent;
}

public partial class MainWindow : Window
{
    private const string ConfigFilePath = @"D:\AI\JARVIS\Configuration\config.json";

    private readonly JarvisBackendClient _client;
    private readonly WasapiAudioMeter _audioMeter = new();
    private readonly Stopwatch _frameWatch = new();
    private double _lastRenderSec;
    private bool _renderLoopAttached;
    private readonly DispatcherTimer _pollTimer;
    private readonly ObservableCollection<ChatDisplayItem> _chatMessages = new();
    private List<LogEntryItem> _cachedLogs = new();
    private readonly ObservableCollection<LogDisplayItem> _visibleLogs = new();
    private readonly HashSet<string> _knownLogModules = new(StringComparer.OrdinalIgnoreCase);
    private long _lastSeenLogId;
    private const int MaxClientCachedLogs = 1500;

    // Window Maximize (WorkArea) & True FullScreen States
    private Rect _restoreBounds = new(120, 80, 1640, 920);
    private bool _isWorkAreaMaximized;
    private bool _isFullScreen;
    private bool _isLightLcdTheme;
    private readonly WindowChrome _defaultWindowChrome;

    // Dynamic HUD & Sphere State
    private int _lastClockSecond = -1;
    private double _currentRms;
    private bool _isMuted;
    private string _currentState = "IDLE";
    private string _currentModelName = "Gemma-4 26B-A4B";
    private bool _currentModelLoaded = true;
    private bool _currentLumiaConnected;
    private string _currentActiveMic = "local";
    private string? _activePendingActionId;
    private bool _testRedAlertMode;
    private string _lastSeenWsMessage = string.Empty;
    private DateTime _lastWsHudUpdateUtc = DateTime.MinValue;

    private static readonly JsonSerializerOptions TelemetryJsonOpts = new() { PropertyNameCaseInsensitive = true };

    private bool IsRedAlertActive => _testRedAlertMode || !string.IsNullOrWhiteSpace(_activePendingActionId);

    // Slide-out drawer states
    private bool _leftOpen;
    private bool _rightOpen;
    private bool _bottomOpen;

    // Theme 1 (Gruvbox Material Dark) Brushes
    private readonly SolidColorBrush _iceBlueBrush = new((Color)ColorConverter.ConvertFromString("#D8A657"));
    private readonly SolidColorBrush _quantumCyanBrush = new((Color)ColorConverter.ConvertFromString("#E78A4E"));
    private readonly SolidColorBrush _frostWhiteBrush = new((Color)ColorConverter.ConvertFromString("#D4BE98"));
    private readonly SolidColorBrush _amberGlitchBrush = new((Color)ColorConverter.ConvertFromString("#D8A657"));
    private readonly SolidColorBrush _crimsonCoreBrush = new((Color)ColorConverter.ConvertFromString("#EA6962"));
    private readonly SolidColorBrush _crimsonBrightBrush = new((Color)ColorConverter.ConvertFromString("#F2594B"));
    private readonly SolidColorBrush _crimsonDeepBrush = new((Color)ColorConverter.ConvertFromString("#C14A4A"));

    // Theme 2 (Transparent LCD White/Black High-Contrast) Brushes
    private readonly SolidColorBrush _lcdPitchBlackBrush = new((Color)ColorConverter.ConvertFromString("#000000"));
    private readonly SolidColorBrush _lcdDeepCarbonBrush = new((Color)ColorConverter.ConvertFromString("#0F172A"));
    private readonly SolidColorBrush _lcdDarkCobaltBrush = new((Color)ColorConverter.ConvertFromString("#0369A1"));

    public MainWindow()
    {
        InitializeComponent();
        FreezeBrushes();

        _defaultWindowChrome = WindowChrome.GetWindowChrome(this);
        LstConversation.ItemsSource = _chatMessages;
        LstLogs.ItemsSource = _visibleLogs;

        _client = new JarvisBackendClient();
        _client.OnHudUpdate += HandleHudUpdateFromWs;
        _client.OnLiveLogEntry += HandleLiveLogFromWs;
        _client.OnConnectionStateChanged += HandleWsConnectionState;
        _client.OnClientLog += msg => Dispatcher.InvokeAsync(() => TxtStatusBar.Text = msg);

        // VSync-locked 60Hz Hardware Renderer (zero DispatcherTimer judder/ghosting)
        _frameWatch.Start();
        AttachRenderLoop();

        // 2.5s Telemetry & Status sync
        _pollTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(2500) };
        _pollTimer.Tick += async (_, _) => await PollBackendSubsystemsAsync();
        _pollTimer.Start();

        StateChanged += (_, _) =>
        {
            if (WindowState == WindowState.Minimized)
            {
                DetachRenderLoop();
            }
            else
            {
                AttachRenderLoop();
            }
        };

        Loaded += MainWindow_Loaded;
        Closed += (_, _) =>
        {
            DetachRenderLoop();
            _pollTimer.Stop();
            _audioMeter.Dispose();
            _client.Dispose();
        };
    }

    private void FreezeBrushes()
    {
        _iceBlueBrush.Freeze();
        _quantumCyanBrush.Freeze();
        _frostWhiteBrush.Freeze();
        _amberGlitchBrush.Freeze();
        _crimsonCoreBrush.Freeze();
        _crimsonBrightBrush.Freeze();
        _crimsonDeepBrush.Freeze();
        _lcdPitchBlackBrush.Freeze();
        _lcdDeepCarbonBrush.Freeze();
        _lcdDarkCobaltBrush.Freeze();
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

    private void BtnGlobeMode_Click(object sender, RoutedEventArgs e) => ToggleGlobeMode();

    private void ToggleGlobeMode()
    {
        CosmicSphere.IsEarthMode = !CosmicSphere.IsEarthMode;
        BtnGlobeMode.Content = CosmicSphere.IsEarthMode ? "EARTH GLOBE" : "COSMIC SPHERE";
        TxtStatusBar.Text = CosmicSphere.IsEarthMode
            ? "HUD Projection: Tactical Earth Globe (Natural Earth + Capital Synapses)"
            : "HUD Projection: Cosmic Star Sphere (740 Nodes)";
        CosmicSphere.InvalidateVisual();
    }

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
            res["UserBubbleBgBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#120369A1"));

            AmbientHalo.Visibility = Visibility.Collapsed;
            BtnThemeSwitch.Content = "GRUVBOX DARK";
        }
        else
        {
            res["BgDeepNavyBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#141617"));

            var radial = new RadialGradientBrush { Center = new Point(0.5, 0.5), RadiusX = 0.75, RadiusY = 0.75 };
            radial.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#1F2223"), 0.0));
            radial.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#17191A"), 0.55));
            radial.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#111314"), 1.0));
            res["BackdropRadialBrush"] = radial;

            var panelGrad = new LinearGradientBrush { StartPoint = new Point(0, 0), EndPoint = new Point(1, 1) };
            panelGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#F21C1F20"), 0.0));
            panelGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#F7141617"), 1.0));
            res["BgPanelBrush"] = panelGrad;

            var cardGrad = new LinearGradientBrush { StartPoint = new Point(0, 0), EndPoint = new Point(0, 1) };
            cardGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#CC242728"), 0.0));
            cardGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#CC1B1E1F"), 1.0));
            res["BgCardBrush"] = cardGrad;

            var borderGrad = new LinearGradientBrush { StartPoint = new Point(0, 0), EndPoint = new Point(1, 1) };
            borderGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#55E78A4E"), 0.0));
            borderGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#22D4BE98"), 0.5));
            borderGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#44D8A657"), 1.0));
            res["GlassBorderBrush"] = borderGrad;

            var cyanGrad = new LinearGradientBrush { StartPoint = new Point(0, 0), EndPoint = new Point(1, 0) };
            cyanGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#C96A32"), 0.0));
            cyanGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#E78A4E"), 0.5));
            cyanGrad.GradientStops.Add(new GradientStop((Color)ColorConverter.ConvertFromString("#D8A657"), 1.0));
            res["CyanGradientBrush"] = cyanGrad;

            res["BgInputBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#191B1C"));
            res["BgSubtlePillBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#1C1F20"));
            res["BgDockPillBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#D8191B1C"));
            res["GridBgBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#121415"));
            res["GridRowBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#181A1B"));
            res["GridAltRowBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#1E2122"));
            res["GridHeaderBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#242729"));

            res["CyanPrimaryBrush"] = _iceBlueBrush;
            res["CyanSecondaryBrush"] = _quantumCyanBrush;
            res["TextPrimaryBrush"] = _frostWhiteBrush;
            res["TextMutedBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#928374"));
            res["CyanBorderDimBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#33D4BE98"));

            res["BtnDefaultBgBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#1CE78A4E"));
            res["BtnDefaultBorderBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#52E78A4E"));
            res["BtnHoverBgBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#33E78A4E"));
            res["UserBubbleBgBrush"] = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#26E78A4E"));

            AmbientHalo.Visibility = Visibility.Visible;
            BtnThemeSwitch.Content = "\u2600 LCD THEME";
        }

        HandleWsConnectionState(_client.IsWsConnected);
        UpdateSphereThemeBrushes();
        ApplySystemStatus(_currentState, _currentModelLoaded, _currentModelName, _currentLumiaConnected, _isMuted, _currentActiveMic, TxtPairingPin.Text);
    }

    private void UpdateSphereThemeBrushes()
    {
        if (CosmicSphere == null) return;
        CosmicSphere.IsLightLcdTheme = _isLightLcdTheme;
        CosmicSphere.IsRedAlert = IsRedAlertActive;
        CosmicSphere.InvalidateVisual();
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
        else if (e.Key == Key.F8)
        {
            e.Handled = true;
            ToggleGlobeMode();
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
            HandleWsConnectionState(_client.IsWsConnected);
        UpdateSphereThemeBrushes();
            ApplySystemStatus(_currentState, _currentModelLoaded, _currentModelName, _currentLumiaConnected, _isMuted, _currentActiveMic, TxtPairingPin.Text);
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

    // ==================== 3D COSMIC SPHERE & ORBITAL HUD ENGINE (VSYNC 60HZ + LIVE WASAPI AUDIO) ====================

    private void AttachRenderLoop()
    {
        if (_renderLoopAttached) return;
        _lastRenderSec = _frameWatch.Elapsed.TotalSeconds;
        CompositionTarget.Rendering += OnCompositionTargetRendering;
        _renderLoopAttached = true;
    }

    private void DetachRenderLoop()
    {
        if (!_renderLoopAttached) return;
        CompositionTarget.Rendering -= OnCompositionTargetRendering;
        _renderLoopAttached = false;
    }

    private void OnCompositionTargetRendering(object? sender, EventArgs e)
    {
        double nowSec = _frameWatch.Elapsed.TotalSeconds;
        double dt = nowSec - _lastRenderSec;
        if (dt < 0.0155) return;
        _lastRenderSec = nowSec;

        double liveVoicePeak = _isMuted ? 0.0 : _audioMeter.GetPythonPlaybackPeak();

        CosmicSphere.IsLightLcdTheme = _isLightLcdTheme;
        CosmicSphere.IsRedAlert = IsRedAlertActive;
        CosmicSphere.IsMuted = _isMuted;
        CosmicSphere.SystemState = _currentState;
        CosmicSphere.TargetRms = _currentRms;
        CosmicSphere.LiveVoicePeak = liveVoicePeak;

        CosmicSphere.Step(dt);

        if (_leftOpen && liveVoicePeak > 0.005)
        {
            TxtRmsValue.Text = liveVoicePeak.ToString("0.000");
        }

        var nowLocal = DateTime.Now;
        if (nowLocal.Second != _lastClockSecond)
        {
            _lastClockSecond = nowLocal.Second;
            TxtClock.Text = nowLocal.ToString("HH:mm:ss");
        }
    }

    // ==================== SLIDE-OUT DRAWER ANIMATIONS ====================

    private void AnimateDrawer(Border drawer, TranslateTransform transform, DependencyProperty prop, double toValue, bool opening)
    {
        if (opening)
        {
            drawer.Visibility = Visibility.Visible;
        }

        var anim = new DoubleAnimation
        {
            To = toValue,
            Duration = TimeSpan.FromMilliseconds(280),
            EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut }
        };

        if (!opening)
        {
            anim.Completed += (_, _) =>
            {
                if ((drawer == LeftDrawer && !_leftOpen) ||
                    (drawer == RightDrawer && !_rightOpen) ||
                    (drawer == BottomDrawer && !_bottomOpen))
                {
                    drawer.Visibility = Visibility.Collapsed;
                }
            };
        }

        transform.BeginAnimation(prop, anim);
    }

    private void BtnToggleLeftDrawer_Click(object sender, RoutedEventArgs e)
    {
        _leftOpen = !_leftOpen;
        AnimateDrawer(LeftDrawer, LeftDrawerTransform, TranslateTransform.XProperty, _leftOpen ? 0 : -440, _leftOpen);
        BtnLeftHandle.Visibility = _leftOpen ? Visibility.Collapsed : Visibility.Visible;
    }

    private void BtnToggleRightDrawer_Click(object sender, RoutedEventArgs e)
    {
        _rightOpen = !_rightOpen;
        AnimateDrawer(RightDrawer, RightDrawerTransform, TranslateTransform.XProperty, _rightOpen ? 0 : 560, _rightOpen);
        BtnRightHandle.Visibility = _rightOpen ? Visibility.Collapsed : Visibility.Visible;
    }

    private void BtnToggleBottomDrawer_Click(object sender, RoutedEventArgs e)
    {
        _bottomOpen = !_bottomOpen;
        AnimateDrawer(BottomDrawer, BottomDrawerTransform, TranslateTransform.YProperty, _bottomOpen ? 0 : 400, _bottomOpen);
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
            bool wsRecentlyActive = _client.IsWsConnected && (DateTime.UtcNow - _lastWsHudUpdateUtc).TotalSeconds < 2.2;
            string effectiveState = wsRecentlyActive ? _currentState : status.State;
            ApplySystemStatus(
                effectiveState,
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
            _lastWsHudUpdateUtc = DateTime.UtcNow;
            _currentRms = Math.Clamp(msg.Rms, 0.0, 1.0);
            TxtRmsValue.Text = _currentRms.ToString("0.000");

            ApplySystemStatus(
                msg.State,
                msg.ModelLoaded,
                _currentModelName,
                msg.LumiaConnected,
                msg.Muted,
                msg.ActiveMic,
                _client.PairingPin);



            if (msg.Telemetry.HasValue && msg.Telemetry.Value.ValueKind == JsonValueKind.Object)
            {
                try
                {
                    var wsTelemetry = JsonSerializer.Deserialize<TelemetryModel>(msg.Telemetry.Value.GetRawText(), TelemetryJsonOpts);
                    if (wsTelemetry != null) ApplyTelemetry(wsTelemetry);
                }
                catch { }
            }

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
        _currentModelLoaded = modelLoaded;
        if (!string.IsNullOrWhiteSpace(modelName)) _currentModelName = modelName;
        _currentLumiaConnected = lumiaConnected;
        if (!string.IsNullOrWhiteSpace(activeMic)) _currentActiveMic = activeMic;

        bool isRedAlert = IsRedAlertActive;
        TxtSystemState.Text = isRedAlert ? "AWAITING AUTH" : (_isMuted ? "MUTED" : _currentState);
        TxtPairingPin.Text = string.IsNullOrWhiteSpace(pairingPin) ? "------" : pairingPin;

        Brush activeAccent = _isLightLcdTheme ? _lcdPitchBlackBrush : _quantumCyanBrush;
        Brush idleAccent = _isLightLcdTheme ? _lcdPitchBlackBrush : _iceBlueBrush;

        Brush stateBrush = idleAccent;
        if (isRedAlert)
        {
            stateBrush = _crimsonCoreBrush;
            HaloInnerStop.Color = (Color)ColorConverter.ConvertFromString("#48EA6962");
            TxtReactorSubtitle.Text = "⚠ CRITICAL COMMAND PENDING // منتظر تایید دستور حساس توسط شما...";
        }
        else if (_isMuted)
        {
            stateBrush = _crimsonCoreBrush;
            HaloInnerStop.Color = (Color)ColorConverter.ConvertFromString("#22EA6962");
            TxtReactorSubtitle.Text = "AUDIO INPUT MUTED // ورودی صوتی غیرفعال است";
        }
        else if (_currentState.Contains("LISTEN"))
        {
            stateBrush = activeAccent;
            HaloInnerStop.Color = (Color)ColorConverter.ConvertFromString("#34E78A4E");
            TxtReactorSubtitle.Text = "LISTENING // در حال دریافت امواج صوتی...";
        }
        else if (_currentState.Contains("THINK"))
        {
            stateBrush = _isLightLcdTheme ? _lcdDarkCobaltBrush : _amberGlitchBrush;
            HaloInnerStop.Color = (Color)ColorConverter.ConvertFromString("#2DD8A657");
            TxtReactorSubtitle.Text = "TACTICAL ANALYSIS // در حال پردازش منطقی در هسته...";
        }
        else if (_currentState.Contains("SPEAK"))
        {
            stateBrush = activeAccent;
            HaloInnerStop.Color = (Color)ColorConverter.ConvertFromString("#38E78A4E");
            TxtReactorSubtitle.Text = "VOCAL SYNTHESIS // سلست در حال پاسخگویی...";
        }
        else
        {
            HaloInnerStop.Color = (Color)ColorConverter.ConvertFromString("#1EE78A4E");
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
        TxtGpuPercent.Text = $"{t.GpuPercent:0.0}% | {t.GpuTempC:0}\u00B0C";

        // Thermal Matrix Card
        double gpuTemp = Math.Clamp(t.GpuTempC, 0, 110);
        TxtGpuTempBig.Text = $"{gpuTemp:0}\u00B0C";
        BarGpuTemp.Value = Math.Clamp(gpuTemp, 20, 95);

        if (t.CpuTempC.HasValue && t.CpuTempC.Value > 0)
        {
            TxtSecondTempLabel.Text = "CPU PACKAGE TEMP";
            TxtSecondTempValue.Text = $"{t.CpuTempC.Value:0}\u00B0C";
        }
        else
        {
            TxtSecondTempLabel.Text = "THROTTLE HEADROOM";
            double headroom = Math.Max(0, 83.0 - gpuTemp);
            TxtSecondTempValue.Text = $"+{headroom:0}\u00B0C";
        }

        if (gpuTemp >= 78)
        {
            TxtThermalStatus.Text = "HOT";
            TxtThermalStatus.Foreground = _crimsonCoreBrush;
            TxtGpuTempBig.Foreground = _crimsonCoreBrush;
        }
        else if (gpuTemp >= 65)
        {
            TxtThermalStatus.Text = "WARM";
            TxtThermalStatus.Foreground = _amberGlitchBrush;
            TxtGpuTempBig.Foreground = _amberGlitchBrush;
        }
        else
        {
            TxtThermalStatus.Text = "OPTIMAL";
            TxtThermalStatus.Foreground = (Brush)FindResource("ActiveGreenBrush");
            TxtGpuTempBig.Foreground = _isLightLcdTheme ? _lcdPitchBlackBrush : _quantumCyanBrush;
        }

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
            HandleWsConnectionState(_client.IsWsConnected);
        UpdateSphereThemeBrushes();
            ApplySystemStatus(_currentState, _currentModelLoaded, _currentModelName, _currentLumiaConnected, _isMuted, _currentActiveMic, TxtPairingPin.Text);
        }
    }

    private void AppendChatMessage(string sender, string content)
    {
        bool isUser = string.Equals(sender, "USER", StringComparison.OrdinalIgnoreCase);
        _chatMessages.Add(new ChatDisplayItem
        {
            SenderLabel = sender,
            Content = content,
            Timestamp = DateTime.Now.ToString("HH:mm:ss"),
            IsUser = isUser
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
            foreach (var item in history.OrderBy(h => h.Id).TakeLast(30))
            {
                bool isUser = string.Equals(item.Role, "user", StringComparison.OrdinalIgnoreCase);
                _chatMessages.Add(new ChatDisplayItem
                {
                    SenderLabel = isUser ? "USER" : "C.E.L.E.S.T.E.",
                    Content = item.Content,
                    Timestamp = item.Timestamp,
                    IsUser = isUser
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
            HandleWsConnectionState(_client.IsWsConnected);
        UpdateSphereThemeBrushes();
            ApplySystemStatus(_currentState, _currentModelLoaded, _currentModelName, _currentLumiaConnected, _isMuted, _currentActiveMic, TxtPairingPin.Text);
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
            HandleWsConnectionState(_client.IsWsConnected);
        UpdateSphereThemeBrushes();
            ApplySystemStatus(_currentState, _currentModelLoaded, _currentModelName, _currentLumiaConnected, _isMuted, _currentActiveMic, TxtPairingPin.Text);
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

    private LogDisplayItem CreateLogDisplayItem(LogEntryItem entry)
    {
        string lvl = (entry.Level ?? "INFO").ToUpperInvariant();
        Brush fg;
        Brush bg;

        if (lvl is "ERROR" or "CRITICAL" or "FATAL")
        {
            fg = _crimsonCoreBrush;
            bg = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#28EA6962"));
        }
        else if (lvl is "WARNING" or "WARN")
        {
            fg = _amberGlitchBrush;
            bg = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#26D8A657"));
        }
        else if (lvl == "DEBUG")
        {
            fg = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#94A3B8"));
            bg = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#1E94A3B8"));
        }
        else
        {
            fg = _isLightLcdTheme ? _lcdDarkCobaltBrush : _quantumCyanBrush;
            bg = new SolidColorBrush((Color)ColorConverter.ConvertFromString("#22E78A4E"));
        }

        return new LogDisplayItem
        {
            Raw = entry,
            LevelBrush = fg,
            LevelBadgeBg = bg
        };
    }

    private void EnsureLogModuleListed(string? moduleName)
    {
        if (string.IsNullOrWhiteSpace(moduleName)) return;
        if (_knownLogModules.Add(moduleName))
        {
            CmbLogModule.Items.Add(new ComboBoxItem { Content = moduleName });
        }
    }

    private bool MatchesCurrentLogFilters(LogEntryItem entry)
    {
        string levelFilter = (CmbLogSeverity.SelectedItem as ComboBoxItem)?.Content?.ToString() ?? "ALL";
        if (!string.Equals(levelFilter, "ALL", StringComparison.OrdinalIgnoreCase) &&
            !string.Equals(entry.Level, levelFilter, StringComparison.OrdinalIgnoreCase))
        {
            return false;
        }

        string moduleFilter = (CmbLogModule.SelectedItem as ComboBoxItem)?.Content?.ToString() ?? "ALL";
        if (!string.Equals(moduleFilter, "ALL", StringComparison.OrdinalIgnoreCase) &&
            !string.Equals(entry.Module, moduleFilter, StringComparison.OrdinalIgnoreCase))
        {
            return false;
        }

        string query = (TxtLogSearch?.Text ?? string.Empty).Trim();
        if (!string.IsNullOrWhiteSpace(query))
        {
            bool hit =
                (entry.Message?.Contains(query, StringComparison.OrdinalIgnoreCase) ?? false) ||
                (entry.Module?.Contains(query, StringComparison.OrdinalIgnoreCase) ?? false) ||
                (entry.TurnId?.Contains(query, StringComparison.OrdinalIgnoreCase) ?? false) ||
                (entry.Traceback?.Contains(query, StringComparison.OrdinalIgnoreCase) ?? false);
            if (!hit) return false;
        }

        return true;
    }

    private void UpdateLogCounter()
    {
        if (TxtLogCount != null)
        {
            TxtLogCount.Text = $"{_visibleLogs.Count} shown / {_cachedLogs.Count} buffered";
        }
    }

    private void HandleLiveLogFromWs(LogEntryItem entry)
    {
        Dispatcher.InvokeAsync(() =>
        {
            if (ChkLogLive?.IsChecked != true) return;
            if (entry.Id > 0 && entry.Id <= _lastSeenLogId) return;
            if (entry.Id > _lastSeenLogId) _lastSeenLogId = entry.Id;

            _cachedLogs.Add(entry);
            if (_cachedLogs.Count > MaxClientCachedLogs)
            {
                _cachedLogs.RemoveRange(0, _cachedLogs.Count - MaxClientCachedLogs);
            }

            EnsureLogModuleListed(entry.Module);

            if (MatchesCurrentLogFilters(entry))
            {
                _visibleLogs.Add(CreateLogDisplayItem(entry));
                if (_visibleLogs.Count > MaxClientCachedLogs)
                {
                    _visibleLogs.RemoveAt(0);
                }

                if (ChkLogAutoScroll?.IsChecked == true && _visibleLogs.Count > 0)
                {
                    LstLogs.ScrollIntoView(_visibleLogs[^1]);
                }
            }

            UpdateLogCounter();
        });
    }

    private async void BtnRefreshLogs_Click(object sender, RoutedEventArgs e) => await RefreshLogsAsync();

    private void CmbLogSeverity_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (IsLoaded) ApplyLogFilter();
    }

    private void CmbLogModule_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (IsLoaded) ApplyLogFilter();
    }

    private void TxtLogSearch_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (IsLoaded) ApplyLogFilter();
    }

    private async void ChkLogLive_Changed(object sender, RoutedEventArgs e)
    {
        if (!IsLoaded) return;
        bool live = ChkLogLive.IsChecked == true;
        await _client.SetLiveLogsSubscriptionAsync(live, "DEBUG");
        if (live)
        {
            await RefreshLogsAsync();
        }
    }

    private void BtnClearLogsView_Click(object sender, RoutedEventArgs e)
    {
        _cachedLogs.Clear();
        _visibleLogs.Clear();
        TxtLogSearch.Text = string.Empty;
        UpdateLogCounter();
    }

    private void BtnCopyLogs_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            if (LstLogs.SelectedItem is LogDisplayItem sel)
            {
                Clipboard.SetText(sel.Raw.FullFormattedLine);
                TxtStatusBar.Text = "Copied selected log entry to clipboard.";
                return;
            }
            if (_visibleLogs.Count > 0)
            {
                var sb = new StringBuilder();
                foreach (var item in _visibleLogs)
                {
                    sb.AppendLine(item.Raw.FullFormattedLine);
                }
                Clipboard.SetText(sb.ToString());
                TxtStatusBar.Text = $"Copied {_visibleLogs.Count} visible log entries to clipboard.";
            }
        }
        catch (Exception ex)
        {
            TxtStatusBar.Text = $"Clipboard copy failed: {ex.Message}";
        }
    }

    private void MenuCopySelectedLog_Click(object sender, RoutedEventArgs e)
    {
        if (LstLogs.SelectedItem is LogDisplayItem sel)
        {
            try
            {
                Clipboard.SetText(sel.Raw.FullFormattedLine);
                TxtStatusBar.Text = "Copied selected log entry (+ traceback) to clipboard.";
            }
            catch { }
        }
    }

    private void MenuFilterTurn_Click(object sender, RoutedEventArgs e)
    {
        if (LstLogs.SelectedItem is LogDisplayItem sel && sel.Raw.HasTurnId)
        {
            TxtLogSearch.Text = sel.Raw.TurnId;
        }
    }

    private void LstLogs_MouseDoubleClick(object sender, MouseButtonEventArgs e)
    {
        if (LstLogs.SelectedItem is LogDisplayItem sel && sel.Raw.HasTurnId)
        {
            TxtLogSearch.Text = string.Equals(TxtLogSearch.Text.Trim(), sel.Raw.TurnId, StringComparison.OrdinalIgnoreCase)
                ? string.Empty
                : sel.Raw.TurnId;
        }
    }

    private async Task RefreshLogsAsync()
    {
        var resp = await _client.GetLogsStructuredAsync(limit: 500);
        if (resp != null)
        {
            _cachedLogs = resp.Logs ?? new();
            _lastSeenLogId = Math.Max(_lastSeenLogId, resp.LastId);
            foreach (var l in _cachedLogs)
            {
                EnsureLogModuleListed(l.Module);
            }
        }

        var mods = await _client.GetLogModulesAsync();
        foreach (var m in mods)
        {
            EnsureLogModuleListed(m);
        }

        ApplyLogFilter();
    }

    private void ApplyLogFilter()
    {
        _visibleLogs.Clear();
        foreach (var entry in _cachedLogs)
        {
            if (MatchesCurrentLogFilters(entry))
            {
                _visibleLogs.Add(CreateLogDisplayItem(entry));
            }
        }
        if (ChkLogAutoScroll?.IsChecked == true && _visibleLogs.Count > 0)
        {
            LstLogs.ScrollIntoView(_visibleLogs[^1]);
        }
        UpdateLogCounter();
    }
}