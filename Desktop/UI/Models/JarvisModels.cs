using System.Text.Json;
using System.Text.Json.Serialization;

namespace JarvisDesktop.Models;

public class PairRequest
{
    [JsonPropertyName("device_name")]
    public string DeviceName { get; set; } = "JarvisDesktopPC";

    [JsonPropertyName("pin")]
    public string Pin { get; set; } = string.Empty;
}

public class PairResponse
{
    [JsonPropertyName("status")]
    public string Status { get; set; } = string.Empty;

    [JsonPropertyName("token")]
    public string Token { get; set; } = string.Empty;

    [JsonPropertyName("expires_in")]
    public int ExpiresIn { get; set; }
}

public class SystemStatusModel
{
    [JsonPropertyName("state")]
    public string State { get; set; } = "IDLE";

    [JsonPropertyName("model_loaded")]
    public bool ModelLoaded { get; set; }

    [JsonPropertyName("model_name")]
    public string ModelName { get; set; } = "Qwen 3.5 9B";

    [JsonPropertyName("lumia_connected")]
    public bool LumiaConnected { get; set; }

    [JsonPropertyName("muted")]
    public bool Muted { get; set; }

    [JsonPropertyName("active_mic")]
    public string ActiveMic { get; set; } = "mic";

    [JsonPropertyName("pairing_pin")]
    public string PairingPin { get; set; } = "------";
}

public class TelemetryModel
{
    [JsonPropertyName("cpu_percent")]
    public double CpuPercent { get; set; }

    [JsonPropertyName("gpu_percent")]
    public double GpuPercent { get; set; }

    [JsonPropertyName("vram_used_mb")]
    public double VramUsedMb { get; set; }

    [JsonPropertyName("vram_total_mb")]
    public double VramTotalMb { get; set; } = 10240;

    [JsonPropertyName("gpu_temp_c")]
    public double GpuTempC { get; set; }

    [JsonPropertyName("ram_used_gb")]
    public double RamUsedGb { get; set; }

    [JsonPropertyName("ram_total_gb")]
    public double RamTotalGb { get; set; } = 32.0;
}

public class ChatRequest
{
    [JsonPropertyName("text")]
    public string Text { get; set; } = string.Empty;

    [JsonPropertyName("source")]
    public string Source { get; set; } = "desktop";
}

public class ChatResponse
{
    [JsonPropertyName("spoken_response")]
    public string SpokenResponse { get; set; } = string.Empty;

    [JsonPropertyName("response")]
    public string? FallbackResponse { get; set; }

    [JsonPropertyName("action")]
    public JsonElement? Action { get; set; }

    [JsonPropertyName("language")]
    public string Language { get; set; } = "fa";

    [JsonPropertyName("session_id")]
    public string SessionId { get; set; } = "default";

    [JsonIgnore]
    public string EffectiveText => !string.IsNullOrWhiteSpace(SpokenResponse)
        ? SpokenResponse
        : (FallbackResponse ?? string.Empty);
}

public class ConversationItem
{
    [JsonPropertyName("id")]
    public int Id { get; set; }

    [JsonPropertyName("role")]
    public string Role { get; set; } = "user";

    [JsonPropertyName("content")]
    public string Content { get; set; } = string.Empty;

    [JsonPropertyName("timestamp")]
    public string Timestamp { get; set; } = string.Empty;
}

public class MemoryHistoryResponse
{
    [JsonPropertyName("messages")]
    public List<ConversationItem> Messages { get; set; } = new();
}

public class FactItem
{
    [JsonPropertyName("id")]
    public int Id { get; set; }

    [JsonPropertyName("category")]
    public string Category { get; set; } = "user";

    [JsonPropertyName("key")]
    public string Key { get; set; } = string.Empty;

    [JsonPropertyName("value")]
    public string Value { get; set; } = string.Empty;

    [JsonPropertyName("importance")]
    public int Importance { get; set; } = 5;
}

public class FactsListResponse
{
    [JsonPropertyName("facts")]
    public List<FactItem> Facts { get; set; } = new();
}

public class FactCreateRequest
{
    [JsonPropertyName("category")]
    public string Category { get; set; } = "user";

    [JsonPropertyName("key")]
    public string Key { get; set; } = string.Empty;

    [JsonPropertyName("value")]
    public string Value { get; set; } = string.Empty;

    [JsonPropertyName("importance")]
    public int Importance { get; set; } = 5;
}

public class PendingConfirmationItem
{
    [JsonPropertyName("action_id")]
    public string ActionId { get; set; } = string.Empty;

    [JsonPropertyName("command_type")]
    public string CommandType { get; set; } = string.Empty;

    [JsonPropertyName("risk_level")]
    public string RiskLevel { get; set; } = "SENSITIVE";

    [JsonPropertyName("payload")]
    public JsonElement? Payload { get; set; }

    [JsonIgnore]
    public string PayloadDisplay => Payload.HasValue ? Payload.Value.ToString() : string.Empty;
}

public class PendingConfirmationsResponse
{
    [JsonPropertyName("pending")]
    public List<PendingConfirmationItem> Pending { get; set; } = new();
}

public class ConfirmActionRequest
{
    [JsonPropertyName("action_id")]
    public string ActionId { get; set; } = string.Empty;

    [JsonPropertyName("approved")]
    public bool Approved { get; set; }
}

public class AuditLogItem
{
    [JsonPropertyName("id")]
    public int Id { get; set; }

    [JsonPropertyName("command_type")]
    public string CommandType { get; set; } = string.Empty;

    [JsonPropertyName("risk_level")]
    public string RiskLevel { get; set; } = "SAFE";

    [JsonPropertyName("status")]
    public string Status { get; set; } = "GRANTED";

    [JsonPropertyName("timestamp")]
    public string Timestamp { get; set; } = string.Empty;
}

public class AuditLogResponse
{
    [JsonPropertyName("audit_logs")]
    public List<AuditLogItem> AuditLogs { get; set; } = new();
}

public class LogEntryItem
{
    [JsonPropertyName("timestamp")]
    public string Timestamp { get; set; } = string.Empty;

    [JsonPropertyName("level")]
    public string Level { get; set; } = "INFO";

    [JsonPropertyName("module")]
    public string Module { get; set; } = "core";

    [JsonPropertyName("message")]
    public string Message { get; set; } = string.Empty;
}

public class LogsResponse
{
    [JsonPropertyName("logs")]
    public List<LogEntryItem> Logs { get; set; } = new();
}

public class VoiceConfigModel
{
    [JsonPropertyName("pitch_semitones")]
    public double PitchSemitones { get; set; } = -2.5;

    [JsonPropertyName("metallic_mix")]
    public double MetallicMix { get; set; } = 0.22;

    [JsonPropertyName("reverb_decay")]
    public double ReverbDecay { get; set; } = 0.18;

    [JsonPropertyName("wake_threshold")]
    public double WakeThreshold { get; set; } = 0.50;
}

public class MuteRequest
{
    [JsonPropertyName("muted")]
    public bool Muted { get; set; }
}

public class MuteResponse
{
    [JsonPropertyName("muted")]
    public bool Muted { get; set; }
}

public class HudUpdateMessage
{
    [JsonPropertyName("type")]
    public string Type { get; set; } = "hud_update";

    [JsonPropertyName("state")]
    public string State { get; set; } = "IDLE";

    [JsonPropertyName("rms")]
    public double Rms { get; set; }

    [JsonPropertyName("lumia_connected")]
    public bool LumiaConnected { get; set; }

    [JsonPropertyName("model_loaded")]
    public bool ModelLoaded { get; set; } = true;

    [JsonPropertyName("muted")]
    public bool Muted { get; set; }

    [JsonPropertyName("active_mic")]
    public string ActiveMic { get; set; } = "mic";

    [JsonPropertyName("telemetry")]
    public JsonElement? Telemetry { get; set; }

    [JsonPropertyName("pending_confirmations")]
    public JsonElement? PendingConfirmations { get; set; }

    [JsonPropertyName("last_message")]
    public JsonElement? LastMessage { get; set; }

    public string ExtractLastMessageText()
    {
        if (!LastMessage.HasValue) return string.Empty;
        var el = LastMessage.Value;
        if (el.ValueKind == JsonValueKind.String)
            return el.GetString() ?? string.Empty;
        if (el.ValueKind == JsonValueKind.Object)
        {
            if (el.TryGetProperty("text", out var t) && t.ValueKind == JsonValueKind.String)
                return t.GetString() ?? string.Empty;
            if (el.TryGetProperty("content", out var c) && c.ValueKind == JsonValueKind.String)
                return c.GetString() ?? string.Empty;
        }
        return string.Empty;
    }
}