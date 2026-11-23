using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using VesperDesktop.Models;
using Microsoft.Win32;

namespace VesperDesktop.Services;

public class VesperBackendClient : IDisposable
{
    private readonly HttpClient _http;
    private ClientWebSocket? _ws;
    private CancellationTokenSource? _cts;
    private string _authToken = string.Empty;
    private string _pairingPin = "------";
    private readonly string _projectRoot;
    private bool _liveLogsWanted = true;
    private string _liveLogsMinLevel = "DEBUG";

    public string BaseHttpUrl { get; }
    public string BaseWsUrl { get; }
    public bool IsWsConnected => _ws?.State == WebSocketState.Open;
    public string PairingPin => _pairingPin;

    public event Action<HudUpdateMessage>? OnHudUpdate;
    public event Action<LogEntryItem>? OnLiveLogEntry;
    public event Action<bool>? OnConnectionStateChanged;
    public event Action<string>? OnClientLog;

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNameCaseInsensitive = true
    };

    public static string ResolveProjectRoot()
    {
        try
        {
            string? current = AppContext.BaseDirectory;
            while (!string.IsNullOrEmpty(current))
            {
                if (File.Exists(Path.Combine(current, "Configuration", "config.json")) ||
                    File.Exists(Path.Combine(current, "Core", "vesper_main.py")))
                {
                    return current;
                }
                var parent = Directory.GetParent(current);
                if (parent == null || parent.FullName == current) break;
                current = parent.FullName;
            }

            current = Directory.GetCurrentDirectory();
            while (!string.IsNullOrEmpty(current))
            {
                if (File.Exists(Path.Combine(current, "Configuration", "config.json")) ||
                    File.Exists(Path.Combine(current, "Core", "vesper_main.py")))
                {
                    return current;
                }
                var parent = Directory.GetParent(current);
                if (parent == null || parent.FullName == current) break;
                current = parent.FullName;
            }
        }
        catch
        {
            // fallback below
        }
        return @"D:\AI\VESPER";
    }

    public VesperBackendClient(string host = "127.0.0.1", int port = 8765, string? projectRoot = null)
    {
        _projectRoot = projectRoot ?? ResolveProjectRoot();
        BaseHttpUrl = $"http://{host}:{port}";
        BaseWsUrl = $"ws://{host}:{port}";
        _http = new HttpClient
        {
            BaseAddress = new Uri(BaseHttpUrl),
            Timeout = TimeSpan.FromSeconds(15)
        };
        LoadLocalTokenAndPin();
    }

    public void LoadLocalTokenAndPin()
    {
        try
        {
            string authPath = Path.Combine(_projectRoot, "Configuration", "auth_token.json");
            if (File.Exists(authPath))
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(authPath));
                var root = doc.RootElement;

                foreach (var key in new[] { "master_token", "token", "active_token", "auth_token" })
                {
                    if (root.TryGetProperty(key, out var tokEl) && tokEl.ValueKind == JsonValueKind.String)
                    {
                        var val = tokEl.GetString();
                        if (!string.IsNullOrWhiteSpace(val))
                        {
                            _authToken = val;
                            break;
                        }
                    }
                }

                if (string.IsNullOrWhiteSpace(_authToken) && root.TryGetProperty("tokens", out var tokensEl))
                {
                    if (tokensEl.ValueKind == JsonValueKind.Array && tokensEl.GetArrayLength() > 0)
                    {
                        var first = tokensEl[0];
                        if (first.ValueKind == JsonValueKind.String)
                            _authToken = first.GetString() ?? string.Empty;
                        else if (first.ValueKind == JsonValueKind.Object && first.TryGetProperty("token", out var t))
                            _authToken = t.GetString() ?? string.Empty;
                    }
                    else if (tokensEl.ValueKind == JsonValueKind.Object)
                    {
                        foreach (var prop in tokensEl.EnumerateObject())
                        {
                            _authToken = prop.Name;
                            break;
                        }
                    }
                }

                foreach (var pinKey in new[] { "pairing_pin", "pin", "current_pin" })
                {
                    if (root.TryGetProperty(pinKey, out var pinEl))
                    {
                        _pairingPin = pinEl.ToString();
                        break;
                    }
                }
            }

            if (!string.IsNullOrWhiteSpace(_authToken))
            {
                _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", _authToken);
            }
        }
        catch (Exception ex)
        {
            OnClientLog?.Invoke($"[AuthInit] {ex.Message}");
        }
    }

    public async Task<bool> EnsureAuthenticatedAsync()
    {
        LoadLocalTokenAndPin();
        if (!string.IsNullOrWhiteSpace(_authToken))
        {
            try
            {
                var test = await _http.GetAsync("/api/v1/status");
                if (test.IsSuccessStatusCode) return true;
            }
            catch
            {
                return false;
            }
        }

        if (!string.IsNullOrWhiteSpace(_pairingPin) && _pairingPin != "------")
        {
            return await PairWithPinAsync(_pairingPin);
        }
        return false;
    }

    public async Task<bool> PairWithPinAsync(string pin, string deviceName = "VesperDesktopPC")
    {
        try
        {
            var resp = await _http.PostAsJsonAsync("/api/v1/pair", new PairRequest { DeviceName = deviceName, Pin = pin });
            if (!resp.IsSuccessStatusCode) return false;
            var data = await resp.Content.ReadFromJsonAsync<PairResponse>(JsonOpts);
            if (data != null && !string.IsNullOrWhiteSpace(data.Token))
            {
                _authToken = data.Token;
                _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", _authToken);
                return true;
            }
        }
        catch (Exception ex)
        {
            OnClientLog?.Invoke($"[PairError] {ex.Message}");
        }
        return false;
    }

    public void StartBackgroundConnection()
    {
        if (_cts != null) return;
        _cts = new CancellationTokenSource();
        _ = Task.Run(() => WebSocketSupervisorLoopAsync(_cts.Token));
    }

    private async Task WebSocketSupervisorLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await EnsureAuthenticatedAsync();
                if (string.IsNullOrWhiteSpace(_authToken))
                {
                    await Task.Delay(3000, ct);
                    continue;
                }

                _ws?.Dispose();
                _ws = new ClientWebSocket();
                _ws.Options.SetRequestHeader("Authorization", $"Bearer {_authToken}");

                var uri = new Uri($"{BaseWsUrl}/ws/desktop?token={Uri.EscapeDataString(_authToken)}");
                await _ws.ConnectAsync(uri, ct);
                OnConnectionStateChanged?.Invoke(true);
                OnClientLog?.Invoke("Connected to /ws/desktop");

                if (_liveLogsWanted)
                {
                    await SendWsCommandAsync("subscribe_logs", new { enabled = true, min_level = _liveLogsMinLevel }, ct);
                }

                using var hbCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
                var hbTask = HeartbeatLoopAsync(hbCts.Token);
                await ReceiveLoopAsync(ct);
                hbCts.Cancel();
                try { await hbTask; } catch { }
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (Exception ex)
            {
                OnClientLog?.Invoke($"[WS Reconnect] {ex.Message}");
            }

            OnConnectionStateChanged?.Invoke(false);
            try { await Task.Delay(3000, ct); } catch { break; }
        }
    }

    private async Task HeartbeatLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested && _ws?.State == WebSocketState.Open)
        {
            await SendWsCommandAsync("heartbeat", new { }, ct);
            await Task.Delay(10000, ct);
        }
    }

    private async Task ReceiveLoopAsync(CancellationToken ct)
    {
        var buffer = new byte[16384];
        while (!ct.IsCancellationRequested && _ws?.State == WebSocketState.Open)
        {
            using var ms = new MemoryStream();
            WebSocketReceiveResult result;
            do
            {
                result = await _ws.ReceiveAsync(new ArraySegment<byte>(buffer), ct);
                if (result.MessageType == WebSocketMessageType.Close)
                    return;
                ms.Write(buffer, 0, result.Count);
            } while (!result.EndOfMessage);

            if (result.MessageType == WebSocketMessageType.Text)
            {
                var json = Encoding.UTF8.GetString(ms.ToArray());
                try
                {
                    using var doc = JsonDocument.Parse(json);
                    if (!doc.RootElement.TryGetProperty("type", out var typeEl) || typeEl.ValueKind != JsonValueKind.String)
                        continue;

                    string msgType = typeEl.GetString() ?? string.Empty;
                    if (msgType == "hud_update")
                    {
                        var hud = JsonSerializer.Deserialize<HudUpdateMessage>(json, JsonOpts);
                        if (hud != null) OnHudUpdate?.Invoke(hud);
                    }
                    else if (msgType == "log_entry")
                    {
                        var logPush = JsonSerializer.Deserialize<WsLogPushMessage>(json, JsonOpts);
                        if (logPush?.Entry != null) OnLiveLogEntry?.Invoke(logPush.Entry);
                    }
                }
                catch
                {
                    // Ignore malformed frame
                }
            }
        }
    }

    public async Task SetLiveLogsSubscriptionAsync(bool enabled, string minLevel = "DEBUG")
    {
        _liveLogsWanted = enabled;
        _liveLogsMinLevel = string.IsNullOrWhiteSpace(minLevel) ? "DEBUG" : minLevel.ToUpperInvariant();
        if (IsWsConnected)
        {
            await SendWsCommandAsync("subscribe_logs", new { enabled = _liveLogsWanted, min_level = _liveLogsMinLevel });
        }
    }

    public async Task SendWsCommandAsync(string type, object? payload = null, CancellationToken ct = default)
    {
        if (_ws?.State != WebSocketState.Open) return;
        try
        {
            var packet = JsonSerializer.Serialize(new { type, payload = payload ?? new { } });
            var bytes = Encoding.UTF8.GetBytes(packet);
            await _ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, ct);
        }
        catch (Exception ex)
        {
            OnClientLog?.Invoke($"[WS Send] {ex.Message}");
        }
    }

    // REST Endpoints matching Documentation/API_CONTRACT.md
    public async Task<bool> GetHealthAsync()
    {
        try
        {
            var r = await _http.GetAsync("/api/v1/health");
            return r.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    public async Task<SystemStatusModel?> GetStatusAsync()
    {
        try
        {
            var status = await _http.GetFromJsonAsync<SystemStatusModel>("/api/v1/status", JsonOpts);
            if (status != null && !string.IsNullOrWhiteSpace(status.PairingPin))
                _pairingPin = status.PairingPin;
            return status;
        }
        catch { return null; }
    }

    public async Task<TelemetryModel?> GetTelemetryAsync()
    {
        try { return await _http.GetFromJsonAsync<TelemetryModel>("/api/v1/telemetry", JsonOpts); }
        catch { return null; }
    }

    public async Task<ChatResponse?> SendChatAsync(string text, string source = "desktop")
    {
        try
        {
            var r = await _http.PostAsJsonAsync("/api/v1/chat", new ChatRequest { Text = text, Source = source });
            if (!r.IsSuccessStatusCode) return null;
            return await r.Content.ReadFromJsonAsync<ChatResponse>(JsonOpts);
        }
        catch { return null; }
    }

    public async Task<bool?> SetMuteAsync(bool muted)
    {
        try
        {
            var r = await _http.PostAsJsonAsync("/api/v1/voice/mute", new MuteRequest { Muted = muted });
            if (!r.IsSuccessStatusCode) return null;
            var res = await r.Content.ReadFromJsonAsync<MuteResponse>(JsonOpts);
            return res?.Muted ?? muted;
        }
        catch { return null; }
    }

    public async Task<VoiceConfigModel?> GetVoiceConfigAsync()
    {
        try
        {
            return await _http.GetFromJsonAsync<VoiceConfigModel>("/api/v1/voice/config", JsonOpts);
        }
        catch { return null; }
    }

    public async Task<bool> UpdateVoiceConfigAsync(VoiceConfigModel config)
    {
        try
        {
            var r = await _http.PostAsJsonAsync("/api/v1/voice/config", config);
            return r.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    public async Task<List<ConversationItem>> GetMemoryHistoryAsync()
    {
        try
        {
            var res = await _http.GetFromJsonAsync<MemoryHistoryResponse>("/api/v1/memory/history", JsonOpts);
            return res?.Messages ?? new();
        }
        catch { return new(); }
    }

    public async Task<bool> ClearMemoryHistoryAsync()
    {
        try
        {
            var r = await _http.DeleteAsync("/api/v1/memory/history");
            return r.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    public async Task<List<FactItem>> GetFactsAsync()
    {
        try
        {
            var res = await _http.GetFromJsonAsync<FactsListResponse>("/api/v1/memory/facts", JsonOpts);
            return res?.Facts ?? new();
        }
        catch { return new(); }
    }

    public async Task<bool> SaveFactAsync(string category, string key, string value, int importance = 5)
    {
        try
        {
            var r = await _http.PostAsJsonAsync("/api/v1/memory/facts", new FactCreateRequest
            {
                Category = category,
                Key = key,
                Value = value,
                Importance = importance
            });
            return r.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    public async Task<bool> DeleteFactAsync(int factId)
    {
        try
        {
            var r = await _http.DeleteAsync($"/api/v1/memory/facts/{factId}");
            return r.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    public async Task<List<PendingConfirmationItem>> GetPendingConfirmationsAsync()
    {
        try
        {
            var res = await _http.GetFromJsonAsync<PendingConfirmationsResponse>("/api/v1/security/pending", JsonOpts);
            return res?.Pending ?? new();
        }
        catch { return new(); }
    }

    public async Task<bool> ConfirmSecurityActionAsync(string actionId, bool approved)
    {
        try
        {
            var r = await _http.PostAsJsonAsync("/api/v1/security/confirm", new ConfirmActionRequest
            {
                ActionId = actionId,
                Approved = approved
            });
            return r.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    public async Task<List<AuditLogItem>> GetSecurityAuditAsync()
    {
        try
        {
            var res = await _http.GetFromJsonAsync<AuditLogResponse>("/api/v1/security/audit", JsonOpts);
            return res?.AuditLogs ?? new();
        }
        catch { return new(); }
    }

    public async Task<List<LogEntryItem>> GetLogsAsync()
    {
        var res = await GetLogsStructuredAsync(limit: 400);
        return res?.Logs ?? new();
    }

    public async Task<LogsResponse?> GetLogsStructuredAsync(
        string? level = null,
        string? module = null,
        string? search = null,
        long sinceId = 0,
        int limit = 400)
    {
        try
        {
            var qs = new List<string> { $"limit={Math.Clamp(limit, 1, 2000)}" };
            if (!string.IsNullOrWhiteSpace(level) && !string.Equals(level, "ALL", StringComparison.OrdinalIgnoreCase))
                qs.Add($"level={Uri.EscapeDataString(level)}");
            if (!string.IsNullOrWhiteSpace(module) && !string.Equals(module, "ALL", StringComparison.OrdinalIgnoreCase))
                qs.Add($"module={Uri.EscapeDataString(module)}");
            if (!string.IsNullOrWhiteSpace(search))
                qs.Add($"search={Uri.EscapeDataString(search)}");
            if (sinceId > 0)
                qs.Add($"since_id={sinceId}");

            string url = "/api/v1/logs?" + string.Join("&", qs);
            return await _http.GetFromJsonAsync<LogsResponse>(url, JsonOpts);
        }
        catch
        {
            return null;
        }
    }

    public async Task<List<string>> GetLogModulesAsync()
    {
        try
        {
            var res = await _http.GetFromJsonAsync<LogModulesResponse>("/api/v1/logs/modules", JsonOpts);
            return res?.Modules ?? new();
        }
        catch
        {
            return new();
        }
    }

    // Windows Startup helper matching Stage 6.2 (HKCU Run Registry)
    private const string RunRegPath = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string RunValueName = "VESPER_AI_System";

    public bool IsWindowsStartupEnabled()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunRegPath, false);
            var val = key?.GetValue(RunValueName) as string;
            return !string.IsNullOrWhiteSpace(val);
        }
        catch { return false; }
    }

    public bool SetWindowsStartupEnabled(bool enabled)
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunRegPath, true);
            if (key == null) return false;
            if (enabled)
            {
                string vbsPath = Path.Combine(_projectRoot, "Installer", "start_vesper_hidden.vbs");
                key.SetValue(RunValueName, $"wscript.exe \"{vbsPath}\"");
            }
            else
            {
                key.DeleteValue(RunValueName, false);
            }
            return true;
        }
        catch { return false; }
    }

    public void Dispose()
    {
        _cts?.Cancel();
        _ws?.Dispose();
        _http.Dispose();
    }
}