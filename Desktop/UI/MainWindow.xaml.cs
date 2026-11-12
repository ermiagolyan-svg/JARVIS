using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
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

public partial class MainWindow : Window
{
    private readonly JarvisBackendClient _client;
    private readonly DispatcherTimer _animTimer;
    private readonly DispatcherTimer _pollTimer;
    private readonly ObservableCollection<ChatDisplayItem> _chatMessages = new();
    private readonly List<ProgressBar> _waveBars = new();
    private List<LogEntryItem> _cachedLogs = new();

    private double _reactorSpeed = 1.2;
    private double _currentRms;
    private bool _isMuted;
    private string _currentState = "IDLE";
    private string? _activePendingActionId;
    private string _lastSeenWsMessage = string.Empty;
    private readonly Random _rng = new();

    public MainWindow()
    {
        InitializeComponent();

        LstConversation.ItemsSource = _chatMessages;
        foreach (var child in WaveformGrid.Children)
        {
            if (child is ProgressBar pb)
                _waveBars.Add(pb);
        }

        _client = new JarvisBackendClient();
        _client.OnHudUpdate += HandleHudUpdateFromWs;
        _client.OnConnectionStateChanged += HandleWsConnectionState;
        _client.OnClientLog += msg => Dispatcher.InvokeAsync(() => TxtStatusBar.Text = msg);

        // 30 FPS Arc Reactor & Waveform animation timer
        _animTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(33) };
        _animTimer.Tick += AnimTimer_Tick;
        _animTimer.Start();

        // 2.5s Telemetry & Status synchronization timer
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

    private async void MainWindow_Loaded(object sender, RoutedEventArgs e)
    {
        RefreshStartupButtonState();
        _client.StartBackgroundConnection();

        _chatMessages.Add(new ChatDisplayItem
        {
            SenderLabel = "J.A.R.V.I.S. CORE",
            Content = "ترمینال هولوگرافیک مارک ۷ آماده به کار است. ارتباط با هسته مرکزی جارویس برقرار شد.",
            Timestamp = DateTime.Now.ToString("HH:mm:ss")
        });

        await PollBackendSubsystemsAsync();
        await LoadHistoryAndFactsAsync();
        await RefreshSecurityAuditAsync();
        await RefreshLogsAsync();
    }

    private void AnimTimer_Tick(object? sender, EventArgs e)
    {
        OuterRingRotate.Angle = (OuterRingRotate.Angle + 0.9 * _reactorSpeed) % 360;
        MidRingRotate.Angle = (MidRingRotate.Angle - 1.5 * _reactorSpeed + 360) % 360;
        InnerRingRotate.Angle = (InnerRingRotate.Angle + 2.4 * _reactorSpeed) % 360;

        // Animate 16-bar RMS visualizer
        double baseLevel = Math.Clamp(_currentRms * 100.0, 4.0, 100.0);
        if (_currentState == "SPEAKING" || _currentState == "LISTENING")
            baseLevel = Math.Max(baseLevel, 28.0);

        for (int i = 0; i < _waveBars.Count; i++)
        {
            double centerWeight = 1.0 - Math.Abs(i - 7.5) / 9.0;
            double jitter = (_rng.NextDouble() * 18.0) - 9.0;
            double val = _isMuted ? 3.0 : Math.Clamp((baseLevel * centerWeight) + jitter, 4.0, 100.0);
            _waveBars[i].Value = val;
        }

        TxtClock.Text = DateTime.Now.ToString("HH:mm:ss");
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
            TxtWsLink.Foreground = (Brush)FindResource(connected ? "ActiveGreenBrush" : "WarningAmberBrush");
        });
    }

    private void HandleHudUpdateFromWs(HudUpdateMessage msg)
    {
        Dispatcher.InvokeAsync(() =>
        {
            _currentRms = Math.Clamp(msg.Rms, 0.0, 1.0);
            BarRmsLevel.Value = _currentRms;
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
                AppendChatMessage("JARVIS STREAM", lastText);
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
        TxtReactorCenter.Text = _isMuted ? "MUTED" : _currentState;
        TxtPairingPin.Text = string.IsNullOrWhiteSpace(pairingPin) ? "------" : pairingPin;

        // State-driven Reactor speed & brush
        Brush stateBrush = (Brush)FindResource("CyanPrimaryBrush");
        if (_isMuted)
        {
            _reactorSpeed = 0.25;
            stateBrush = (Brush)FindResource("AlertRedBrush");
            TxtReactorSubtitle.Text = "State: MUTED | Microphone input is currently muted.";
        }
        else if (_currentState.Contains("LISTEN"))
        {
            _reactorSpeed = 2.6;
            stateBrush = (Brush)FindResource("ActiveGreenBrush");
            TxtReactorSubtitle.Text = "State: LISTENING | در حال شنیدن صدای کاربر...";
        }
        else if (_currentState.Contains("THINK"))
        {
            _reactorSpeed = 4.2;
            stateBrush = (Brush)FindResource("WarningAmberBrush");
            TxtReactorSubtitle.Text = "State: THINKING | در حال پردازش عصبی روی مدل Qwen 3.5 9B...";
        }
        else if (_currentState.Contains("SPEAK"))
        {
            _reactorSpeed = 3.0;
            stateBrush = (Brush)FindResource("CyanPrimaryBrush");
            TxtReactorSubtitle.Text = "State: SPEAKING | در حال پاسخگویی صوتی...";
        }
        else
        {
            _reactorSpeed = 1.0;
            TxtReactorSubtitle.Text = "State: IDLE | آماده دریافت فرمان صوتی (Hey Jarvis) یا متنی.";
        }

        StateLed.Fill = stateBrush;
        TxtSystemState.Foreground = stateBrush;
        StateBadgeBorder.BorderBrush = stateBrush;
        RingOuter.Stroke = stateBrush;

        // Model Badge
        TxtModelStatus.Text = modelLoaded
            ? $"{modelName} - llama.cpp CUDA [ONLINE]"
            : $"{modelName} [STANDBY / OFFLINE]";
        TxtModelStatus.Foreground = (Brush)FindResource(modelLoaded ? "ActiveGreenBrush" : "WarningAmberBrush");

        // Lumia Status
        LumiaLed.Fill = (Brush)FindResource(lumiaConnected ? "ActiveGreenBrush" : "TextMutedBrush");
        TxtLumiaStatus.Text = lumiaConnected ? "LUMIA 950 XL: CONNECTED" : "LUMIA 950 XL: STANDBY";
        TxtLumiaStatus.Foreground = (Brush)FindResource(lumiaConnected ? "ActiveGreenBrush" : "TextMutedBrush");

        // Active Mic
        bool isNetMic = string.Equals(activeMic, "network", StringComparison.OrdinalIgnoreCase);
        TxtActiveMic.Text = isNetMic ? "NETWORK (LUMIA 950 XL)" : "LOCAL (PC MICROPHONE)";
        TxtActiveMic.Foreground = (Brush)FindResource(isNetMic ? "CyanPrimaryBrush" : "ActiveGreenBrush");

        // Master Mute Button
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
            TxtPendingActionDetails.Text = $"[{first.RiskLevel}] {first.CommandType} — {first.PayloadDisplay} (ID: {first.ActionId})";
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
                    ? "OPERATOR (شما)"
                    : "J.A.R.V.I.S.";
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

    // ==================== EVENT HANDLERS ====================

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
        TxtStatusBar.Text = "Manual Wake-Word trigger sent to JARVIS VoiceOrchestrator.";
        await PollBackendSubsystemsAsync();
    }

    private async void BtnRefreshTelemetry_Click(object sender, RoutedEventArgs e)
    {
        await PollBackendSubsystemsAsync();
        TxtStatusBar.Text = "Telemetry and status synchronized.";
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
        AppendChatMessage("OPERATOR (شما)", text);
        TxtChatStatus.Text = "THINKING...";

        var reply = await _client.SendChatAsync(text, "desktop");
        if (reply != null && !string.IsNullOrWhiteSpace(reply.EffectiveText))
        {
            _lastSeenWsMessage = reply.EffectiveText;
            AppendChatMessage("J.A.R.V.I.S.", reply.EffectiveText);
            TxtChatStatus.Text = "RESPONSE RECEIVED";
        }
        else
        {
            await _client.SendWsCommandAsync("text_command", new { text });
            TxtChatStatus.Text = "DISPATCHED VIA WS";
        }

        await PollBackendSubsystemsAsync();
    }

    private async void BtnApproveAction_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_activePendingActionId)) return;
        string id = _activePendingActionId;
        await _client.ConfirmSecurityActionAsync(id, true);
        await _client.SendWsCommandAsync("confirm_action", new { action_id = id, approved = true });
        TxtStatusBar.Text = $"Security action {id} APPROVED.";
        await PollBackendSubsystemsAsync();
        await RefreshSecurityAuditAsync();
    }

    private async void BtnRejectAction_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_activePendingActionId)) return;
        string id = _activePendingActionId;
        await _client.ConfirmSecurityActionAsync(id, false);
        TxtStatusBar.Text = $"Security action {id} REJECTED.";
        await PollBackendSubsystemsAsync();
        await RefreshSecurityAuditAsync();
    }

    // ==================== TAB 1: VOICE & DSP ====================

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
            ? $"[OK] Voice & DSP config updated at {DateTime.Now:HH:mm:ss}"
            : "[WARN] Backend did not acknowledge /api/v1/voice/config";
    }

    private async void BtnTestVoice_Click(object sender, RoutedEventArgs e)
    {
        var res = await _client.SendChatAsync("جارویس، وضعیت صوتی خودت را گزارش بده.", "desktop");
        if (res != null && !string.IsNullOrWhiteSpace(res.EffectiveText))
        {
            AppendChatMessage("J.A.R.V.I.S. (VOICE TEST)", res.EffectiveText);
        }
        TxtVoiceConfigResult.Text = "Voice synthesis test triggered.";
    }

    // ==================== TAB 2: MEMORY & FACTS ====================

    private async void BtnRefreshFacts_Click(object sender, RoutedEventArgs e)
    {
        GridFacts.ItemsSource = await _client.GetFactsAsync();
        TxtStatusBar.Text = "User facts reloaded from SQLite memory.";
    }

    private async void BtnAddFact_Click(object sender, RoutedEventArgs e)
    {
        string category = string.IsNullOrWhiteSpace(TxtFactCategory.Text) ? "user" : TxtFactCategory.Text.Trim();
        string key = TxtFactKey.Text.Trim();
        string val = TxtFactValue.Text.Trim();
        if (string.IsNullOrWhiteSpace(key) || string.IsNullOrWhiteSpace(val))
        {
            TxtStatusBar.Text = "Fact Key and Value cannot be empty.";
            return;
        }

        int imp = int.TryParse(TxtFactImportance.Text.Trim(), out int parsed) ? Math.Clamp(parsed, 1, 10) : 5;
        bool saved = await _client.SaveFactAsync(category, key, val, imp);
        if (saved)
        {
            TxtFactKey.Text = string.Empty;
            TxtFactValue.Text = string.Empty;
            GridFacts.ItemsSource = await _client.GetFactsAsync();
            TxtStatusBar.Text = $"Saved fact [{category}.{key}] to long-term memory.";
        }
    }

    private async void BtnDeleteFact_Click(object sender, RoutedEventArgs e)
    {
        if (GridFacts.SelectedItem is not FactItem selected)
        {
            TxtStatusBar.Text = "Select a fact row in the table first.";
            return;
        }

        if (await _client.DeleteFactAsync(selected.Id))
        {
            GridFacts.ItemsSource = await _client.GetFactsAsync();
            TxtStatusBar.Text = $"Deleted fact #{selected.Id} ({selected.Key}).";
        }
    }

    private async void BtnClearHistory_Click(object sender, RoutedEventArgs e)
    {
        if (await _client.ClearMemoryHistoryAsync())
        {
            _chatMessages.Clear();
            TxtStatusBar.Text = "Conversation history cleared.";
        }
    }

    // ==================== TAB 3: SECURITY & STARTUP ====================

    private void RefreshStartupButtonState()
    {
        bool enabled = _client.IsWindowsStartupEnabled();
        TxtStartupState.Text = enabled
            ? "Status: REGISTERED in HKCU\\...\\Run (JARVIS_AI_System)"
            : "Status: NOT REGISTERED in Windows logon startup";
        BtnToggleStartup.Content = enabled ? "DISABLE STARTUP" : "ENABLE STARTUP";
        BtnToggleStartup.Style = (Style)FindResource(enabled ? "HoloDangerButtonStyle" : "HoloSuccessButtonStyle");
    }

    private void BtnToggleStartup_Click(object sender, RoutedEventArgs e)
    {
        bool current = _client.IsWindowsStartupEnabled();
        _client.SetWindowsStartupEnabled(!current);
        RefreshStartupButtonState();
    }

    private async void BtnRunDiagnostics_Click(object sender, RoutedEventArgs e)
    {
        TxtDiagnosticsOutput.Text = "Running subsystem diagnostics...";
        bool healthOk = await _client.GetHealthAsync();
        var status = await _client.GetStatusAsync();
        var telemetry = await _client.GetTelemetryAsync();

        TxtDiagnosticsOutput.Text =
            $"[REST /health]: {(healthOk ? "PASS (200 OK)" : "OFFLINE")}\n" +
            $"[WebSocket /ws/desktop]: {(_client.IsWsConnected ? "CONNECTED" : "DISCONNECTED")}\n" +
            $"[LLM Engine]: {(status?.ModelLoaded == true ? $"ONLINE ({status.ModelName})" : "STANDBY")}\n" +
            $"[Telemetry Sensor]: {(telemetry != null ? $"CPU {telemetry.CpuPercent:0.0}% | GPU {telemetry.GpuPercent:0.0}% | VRAM {telemetry.VramUsedMb:0}MB" : "N/A")}";
    }

    private async void BtnRefreshAudit_Click(object sender, RoutedEventArgs e)
    {
        await RefreshSecurityAuditAsync();
    }

    private async Task RefreshSecurityAuditAsync()
    {
        GridSecurityAudit.ItemsSource = await _client.GetSecurityAuditAsync();
    }

    // ==================== TAB 4: LIVE LOG VIEWER ====================

    private async void BtnRefreshLogs_Click(object sender, RoutedEventArgs e)
    {
        await RefreshLogsAsync();
    }

    private void CmbLogSeverity_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!IsLoaded) return;
        ApplyLogFilter();
    }

    private async Task RefreshLogsAsync()
    {
        _cachedLogs = await _client.GetLogsAsync();
        ApplyLogFilter();
    }

    private void ApplyLogFilter()
    {
        string filter = (CmbLogSeverity.SelectedItem as ComboBoxItem)?.Content?.ToString() ?? "ALL";
        IEnumerable<LogEntryItem> filtered = _cachedLogs;
        if (filter != "ALL")
        {
            filtered = _cachedLogs.Where(l => string.Equals(l.Level, filter, StringComparison.OrdinalIgnoreCase));
        }

        LstLogs.ItemsSource = filtered
            .Select(l => $"[{l.Timestamp}] [{l.Level}] ({l.Module}) {l.Message}")
            .ToList();
    }
}