# JARVIS REST API Contract — `/api/v1/*`

**Version:** 1.0.0 (Stage 5.2)  
**Base URL:** `http://<server-ip>:8765/api/v1/`  
**Auth Scheme:** Bearer token (`Authorization: Bearer <token>`)  
**Network Scope:** LAN-only (RFC 1918 private ranges + loopback)

---

## Authentication

All `/api/v1/*` endpoints require a valid Bearer token in the `Authorization` header:

```
Authorization: Bearer <32+ character hex token>
```

The token is managed by `NetworkAuthManager` (stored in `Configuration/auth_token.json`).  
Short tokens (< 32 chars) or missing headers return **401 Unauthorized**.

### Token Lifecycle

| Operation | Endpoint | Auth Required | Notes |
|-----------|----------|:---:|-------|
| Get master token | N/A (internal) | No | `get_master_token()` reads from config file |
| Validate token | `validate_request_token(request)` | — | Delegates to `NetworkAuthManager.validate_token()` |

### Rate Limiting (PIN-based pairing)

- **5 failures per 60 seconds** per device IP before lockout.
- Applied during `/api/v1/pair` POST with PIN verification.
- Lockout duration: 60 seconds, configurable via `auth_manager.py`.

---

## Endpoints

### Health & Status

| Method | Path | Auth | Description |
|--------|------|:---:|-------------|
| `GET` | `/ping` | ❌ | Liveness check. Returns `{"status": "ok", "service": "JARVIS REST API", "token_auth": true}` |
| `GET` | `/api/v1/status` | ✅ | Service status, memory count, uptime |
| `GET` | `/api/v1/telemetry` | ✅ | Request counts, pairing history, voice state |

### Pairing

| Method | Path | Auth | Description |
|--------|------|:---:|-------------|
| `POST` | `/api/v1/pair` | ✅ | Generate device PIN. Body: `{"device_id": "...", "pin": "..."}` |
| `GET` | `/api/v1/pair/{device_id}` | ✅ | Check pairing status for a device |

### Chat

| Method | Path | Auth | Description |
|--------|------|:---:|-------------|
| `POST` | `/api/v1/chat` | ✅ | Send message. Body: `{"message": "...", "context": "..."}`. Returns `{"reply": "...", "intent": "...", "confidence": 0.95}` |

### Voice Control

| Method | Path | Auth | Description |
|--------|------|:---:|-------------|
| `POST` | `/api/v1/voice/mute` | ✅ | Toggle mute. Body: `{"mute": true/false}` |
| `GET` | `/api/v1/voice/config` | ✅ | Get current voice config (engine, language, volume) |
| `PUT` | `/api/v1/voice/config` | ✅ | Update voice config. Body: `{"mute": bool, "volume": int, "engine": str, "language": str}` |

### Memory CRUD

| Method | Path | Auth | Description |
|--------|------|:---:|-------------|
| `GET` | `/api/v1/memory/{key}` | ✅ | Get memory item by key. Returns 404 if not found |
| `POST` | `/api/v1/memory` | ✅ | Create memory item. Body: `{"key": "...", "value": ..., "metadata": {...}}` |
| `DELETE` | `/api/v1/memory/{key}` | ✅ | Delete memory item. Returns deleted item |

### Security Audit

| Method | Path | Auth | Description |
|--------|------|:---:|-------------|
| `POST` | `/api/v1/security/audit/pending` | ✅ | Submit pending audit item. Body: `{"action": "...", "requester": "...", "details": "..."}` |
| `GET` | `/api/v1/security/audit/pending` | ✅ | List all pending audit items |
| `POST` | `/api/v1/security/audit/{item_id}/confirm` | ✅ | Confirm an audit item (status → "confirmed") |
| `GET` | `/api/v1/security/audit/log` | ✅ | Get full audit log entries |

---

## Middleware Summary

| Layer | Check | Effect on non-compliant requests |
|-------|-------|----------------------------------|
| LAN restriction | `_is_lan_ip(client_ip)` | **403 Forbidden** — only RFC 1918 + loopback allowed |
| Bearer token | `validate_request_token(request)` on `/api/v1/*` | **401 Unauthorized** — missing/invalid token |

### RFC 1918 Ranges Enforced

- `127.0.0.0/8` — Loopback
- `10.0.0.0/8` — Class A private
- `172.16.0.0/12` — Class B private
- `192.168.0.0/16` — Class C private

---

## Error Responses

| Status | Meaning |
|--------|---------|
| 401 | Missing or invalid Bearer token |
| 403 | Non-LAN IP address blocked by middleware |
| 404 | Resource not found (memory key, audit item) |
| 422 | Pydantic validation error on request body |

---

## Testability

- `rest_server.py` includes a built-in `TestClient` self-test (`python -m Network.API.rest_server`).
- All 13 assertions must pass before deployment.
- Self-test uses `get_master_token()` fallback to config file for token retrieval.

