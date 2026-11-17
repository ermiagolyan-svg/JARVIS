"""Llama.cpp CUDA Server Manager & Watchdog for JARVIS.

Manages llama-server.exe (CUDA 13.5) lifecycle and local Qwen 3.5 9B GGUF model,
optimized for RTX 3080 + Ryzen 7 9700X.
"""

import subprocess
import time
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# Constants & Defaults (RTX 3080 / Ryzen 7 optimized)
# ---------------------------------------------------------------------------
SERVER_HOST = '127.0.0.1'
DEFAULT_SERVER_PORT = 8080
DEFAULT_CONTEXT_LENGTH = 4096
HEALTH_URL = f'http://{SERVER_HOST}:{DEFAULT_SERVER_PORT}/health'
BASE_URL = f'http://{SERVER_HOST}:{DEFAULT_SERVER_PORT}/v1'

OPTIMAL_FLAGS = [
    '-ngl', '99',           # offload all layers to GPU (RTX 3080 10GB)
    '-fa', 'on',            # flash attention for speedup
    '--log-disable',        # reduce noise in background mode
]


# ---------------------------------------------------------------------------
# LlamaCppManager class
# ---------------------------------------------------------------------------

class LlamaCppManager:
    """Manages llama-server.exe CUDA server and Qwen 3.5 9B GGUF model."""

    def __init__(self) -> None:
        self._server_process = None
        # Lazy-load config on first access
        self._load_config()

    @property
    def _config(self):
        if not hasattr(self, '_loaded_config'):
            self._load_config()
        return getattr(self, '_loaded_config', {})

    def _load_config(self) -> None:
        """Load JARVIS config from Configuration/config.json."""
        try:
            import json
            project_root = Path(__file__).resolve().parent.parent.parent
            config_path = project_root / 'Configuration' / 'config.json'
            with open(config_path, encoding='utf-8') as f:
                data = json.load(f)
            self._loaded_config = data.get('llama_cpp', {})
        except (FileNotFoundError, json.JSONDecodeError):
            # Fallback defaults if config file is missing or invalid
            self._loaded_config = {
                'server_exe_path': 'llama-server.exe',
                'model_gguf_path': 'D:/AI/Models/qwen3.5-9b.gguf',
                'n_gpu_layers': 99,
                'context_length': 8192,
                'cpu_threads': 8,
                'flash_attn': True,
                'host': SERVER_HOST,
                'port': DEFAULT_SERVER_PORT,
            }

    # -------------------------------------------------------------------
    @property
    def host(self) -> str:
        return self._config.get('host', SERVER_HOST)

    @property
    def port(self) -> int:
        return self._config.get('port', DEFAULT_SERVER_PORT)

    @property
    def base_url(self) -> str:
        return f'http://{self.host}:{self.port}/v1'

    # -------------------------------------------------------------------
    def _build_server_args(self, model_path: str) -> List[str]:
        """Build the full command-line arguments for llama-server.exe.

        Reads context_length dynamically from config.json.
        Falls back to default if not specified in config.
        """
        args = [
            '-m', str(model_path),
            '--host', str(self.host),
            '--port', str(self.port),
            '-ngl', str(self._config.get('n_gpu_layers', 99)),
            '-c', str(self._config.get('context_length', DEFAULT_CONTEXT_LENGTH)),
            '-fa', 'on' if self._config.get('flash_attn', True) else 'off',
            '--log-disable',
        ]

        # Optional: CPU threads override
        cpu_threads = self._config.get('cpu_threads')
        if cpu_threads is not None:
            args += ['-t', str(cpu_threads)]

        # Optional: kv_cache_type
        kv_cache = self._config.get('kv_cache_type')
        if kv_cache:
            args += ['-ctk', str(kv_cache), '-ctv', str(kv_cache)]

        return args


    # -------------------------------------------------------------------
    def is_server_running(self) -> bool:
        """Query health endpoint to check if server is alive.

        Returns True if the /health endpoint responds within 3 seconds.
        """
        import urllib.request
        try:
            url = f'http://{self.host}:{self.port}/health'
            req = urllib.request.Request(url, method='GET')
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    # -------------------------------------------------------------------
    def find_llama_server_binary(self) -> str:
        """Locate llama-server.exe from config or system PATH.

        Returns the absolute path to the executable, or raises FileNotFoundError.
        """
        exe_name = self._config.get('server_exe_path', 'llama-server.exe')

        # 1. Try config-specified path (absolute)
        if Path(exe_name).is_absolute() and Path(exe_name).exists():
            return str(Path(exe_name))

        # 2. Search project-local bin directories
        project_root = Path(__file__).resolve().parent.parent.parent
        local_candidates = [
            project_root / 'bin' / exe_name,
            project_root / '_models' / exe_name if False else None,
            project_root / 'Models' / 'llama-server.exe',
        ]
        for candidate in local_candidates:
            if candidate and candidate.exists():
                return str(candidate)

        # 3. Search system PATH (Windows)
        import shutil
        found = shutil.which(exe_name)
        if found:
            return found

        raise FileNotFoundError(
            f'Cannot find llama-server.exe in config or PATH.\n'
            f'Searched: {exe_name}\n'
            f'Please install via: choco install llama.cpp\n'
            f'Or set "server_exe_path" in Configuration/config.json.'
        )

    # -------------------------------------------------------------------
    def find_model_gguf(self) -> str:
        """Locate the Qwen 3.5 9B GGUF model file on disk.

        Searches config path first, then common model directories recursively.
        Returns absolute path or raises FileNotFoundError.
        """
        # 1. Try configured path directly
        config_path = self._config.get('model_gguf_path', '')
        if config_path and Path(config_path).is_file():
            return str(Path(config_path))

        # 2. Search common model directories for files matching "qwen" + "9b"
        project_root = Path(__file__).resolve().parent.parent.parent
        search_dirs = [
            project_root / 'Models',
            project_root / '_models' / 'gguf',
            Path('D:/AI/Models'),
            Path.home() / '.cache' / 'huggingface' / 'hub',
        ]

        import fnmatch
        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            for gguf_file in search_dir.rglob('*.gguf'):
                name_lower = gguf_file.name.lower()
                if 'qwen' in name_lower and ('35b' in name_lower or '9b' in name_lower):
                    return str(gguf_file)

        # 3. Last resort: try the config path even if it doesn't exist yet
        if config_path:
            raise FileNotFoundError(
                f'Model file not found at configured path:\n{config_path}\n\n'
                'Please download Qwen 3.5 9B GGUF (e.g., from HuggingFace)\n'
                'and place it there, or update config.json.'
            )

        raise FileNotFoundError(
            'Could not find any Qwen *9b*.gguf file in known model directories.\n'
            f'Searched: {[str(d) for d in search_dirs]}'
        )




    # -------------------------------------------------------------------
    def start_server(self) -> bool:
        """Start llama-server.exe as a hidden background process.

        If an existing server is already running on the target port, adopts it.
        Otherwise launches with optimal flags for RTX 3080 + Ryzen 7 9700X.

        Returns True if server was started or adopted successfully.
        """
        # Try to adopt existing running server first
        if self.is_server_running():
            print('[LlamaCppManager] Server already running on port {}'.format(self.port))
            return True

        try:
            binary_path = self.find_llama_server_binary()
        except FileNotFoundError as e:
            print('[LlamaCppManager ERROR] ' + str(e))
            return False

        model_path = ''
        try:
            model_path = self.find_model_gguf()
        except FileNotFoundError:
            # Warn but still launch (server will error gracefully)
            print('[LlamaCppManager WARN] Model not found yet, launching without it.')

        # Build command line using _build_server_args (reads context_length from config)
        args = self._build_server_args(model_path)
        cmd = [binary_path] + args

        print('[LlamaCppManager] Launching: {} ...'.format(
            ' '.join(cmd[:5]) + ('...' if len(cmd) > 5 else '')
        ))

        # Launch as hidden background process (CREATE_NO_WINDOW flag)
        try:
            self._server_process = subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
            )
            print('[LlamaCppManager] Server PID: {}'.format(self._server_process.pid))
        except OSError as e:
            print('[LlamaCppManager ERROR] Failed to launch server: ' + str(e))
            return False

        # Wait briefly for the process to initialize (give it a chance)
        time.sleep(1.5)

        if self._server_process.poll() is not None:
            stdout = self._server_process.stdout.read().decode('utf-8', errors='replace')
            print('[LlamaCppManager ERROR] Server exited immediately.')
            print(stdout[:500])
            return False

        # Verify it responded to health check after launch
        if not self.ensure_model_loaded():
            return False

        return True



    # -------------------------------------------------------------------
    def ensure_model_loaded(self, timeout_seconds: int = 120) -> bool:
        """Poll /health endpoint until model is loaded or timeout.

        Waits up to `timeout_seconds` for the server to respond with a valid
        health status indicating the model is ready.

        Returns True if model is confirmed loaded, False on timeout/error.
        """
        import urllib.request
        url = f'http://{self.host}:{self.port}/health'
        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            try:
                req = urllib.request.Request(url, method='GET')
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = resp.read().decode('utf-8', errors='replace').strip()
                    # llama.cpp /health returns JSON like {"status":"ok"} or "OK"
                    if 'ok' in data.lower():
                        print('[LlamaCppManager] Model loaded successfully.')
                        return True
            except Exception:
                pass
            time.sleep(2)

        print('[LlamaCppManager ERROR] Timeout waiting for model to load ({}s).'.format(timeout_seconds))
        return False

    # -------------------------------------------------------------------
    def verify_model_ready(self, timeout_seconds: int = 30) -> bool:
        """Send a tiny completion ping to confirm the model is responsive.

        Uses the v1/chat/completions endpoint with an empty/minimal prompt.

        Returns True if the server responds with valid JSON containing choices.
        """
        import urllib.request
        import json as _json

        url = f'{self.base_url}/chat/completions'
        payload = _json.dumps({
            'model': self._config.get('target_model_name', ''),
            'messages': [{'role': 'user', 'content': 'Hi'}],
            'max_tokens': 1,
            'temperature': 0.0,
        }).encode('utf-8')

        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            try:
                req = urllib.request.Request(url, data=payload, method='POST')
                req.add_header('Content-Type', 'application/json')
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = _json.loads(resp.read().decode('utf-8'))
                    if isinstance(body, dict) and body.get('choices'):
                        print('[LlamaCppManager] Model ready! Response tokens: {}'.format(
                            len(body['choices'][0].get('message', {}).get('content', ''))
                        ))
                        return True
            except Exception:
                pass
            time.sleep(2)

        print('[LlamaCppManager ERROR] verify_model_ready timed out.')
        return False




    # -------------------------------------------------------------------
    def recover_connection(self, max_retries: int = 5) -> bool:
        """Attempt exponential-backoff recovery of a lost server connection.

        If the process is dead, tries to restart it. Otherwise polls health.

        Returns True if connection is recovered or was never broken.
        """
        # Check if our managed process is still alive
        if self._server_process and self._server_process.poll() is not None:
            print('[LlamaCppManager] Managed server died, attempting restart...')
            return self.start_server()

        # Poll health with exponential backoff
        import urllib.request
        url = f'http://{self.host}:{self.port}/health'
        delay = 1.0

        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(url, method='GET')
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = resp.read().decode('utf-8', errors='replace').strip()
                    if 'ok' in data.lower():
                        print('[LlamaCppManager] Connection recovered (attempt {}/{}).'.format(
                            attempt + 1, max_retries
                        ))
                        return True
            except Exception:
                pass

            delay *= 2  # exponential backoff
            print('[LlamaCppManager Retry {} of {}: waiting {:.0f}s...'.format(attempt + 1, max_retries, delay))
            time.sleep(delay)

        print('[LlamaCppManager ERROR] Connection recovery failed after {} retries.'.format(max_retries))
        return False

    # -------------------------------------------------------------------
    def stop_managed_server(self) -> bool:
        """Clean shutdown of the managed llama-server process.

        Sends SIGTERM (or kills on Windows), then verifies it stopped.

        Returns True if server was successfully stopped or wasn't running.
        """
        import signal
        import shutil

        if self._server_process is None:
            print('[LlamaCppManager] No managed server process to stop.')
            return True

        pid = self._server_process.pid

        # Try graceful shutdown first (SIGTERM)
        try:
            if hasattr(signal, 'SIGTERM'):
                self._server_process.send_signal(signal.SIGTERM)
                print('[LlamaCppManager] Sent SIGTERM to PID {}.'.format(pid))
            else:
                # Windows: use taskkill
                subprocess.run(
                    ['taskkill', '/F', '/PID', str(pid)],
                    capture_output=True, text=True
                )
        except Exception as e:
            print('[LlamaCppManager WARN] SIGTERM failed: ' + str(e))

        # Wait up to 10 seconds for graceful exit
        deadline = time.time() + 10
        while time.time() < deadline:
            if self._server_process.poll() is not None:
                print('[LlamaCppManager] Server stopped gracefully.')
                return True
            time.sleep(0.5)

        # Force kill if still alive
        try:
            self._server_process.kill()
            self._server_process.wait(timeout=5)
            print('[LlamaCppManager WARN] Forced kill of PID {}.'.format(pid))
        except Exception as e:
            print('[LlamaCppManager ERROR] Kill failed: ' + str(e))

        return True


# ---------------------------------------------------------------------------
# Alias for convenience (as requested in spec)
# ---------------------------------------------------------------------------
ModelManager = LlamaCppManager




# ---------------------------------------------------------------------------
# Self-test: run via `python -m Core.ModelManager.llamacpp_manager`
# Non-blocking: verifies command construction WITHOUT spawning llama-server.exe
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    print('=== LlamaCppManager Self-Test ===')

    manager = LlamaCppManager()

    # Test 1: Config loading
    print('\n[TEST] Host:', manager.host)
    print('[TEST] Port:', manager.port)
    print('[TEST] Base URL:', manager.base_url)

    # Test 2: Verify -m flag is present in _build_server_args output
    test_model = 'D:/AI/Models/test-model.gguf'
    args = manager._build_server_args(test_model)
    assert '-m' in args, 'FAIL: -m flag missing from command args'
    m_idx = args.index('-m')
    assert args[m_idx + 1] == test_model, 'FAIL: model path not after -m flag'
    print('[TEST] _build_server_args OK: -m flag present before model path')
    print('[TEST] Full args:', ' '.join(args[:8]) + ('...' if len(args) > 8 else ''))

    # Test 3: Verify --host and --port are in the args
    assert '--host' in args, 'FAIL: --host flag missing'
    assert '--port' in args, 'FAIL: --port flag missing'
    host_idx = args.index('--host')
    port_idx = args.index('--port')
    assert args[host_idx + 1] == manager.host, 'FAIL: wrong host value'
    assert args[port_idx + 1] == str(manager.port), 'FAIL: wrong port value'
    print('[TEST] --host/--port flags verified')

    # Test 4: is_server_running() probes /health (non-blocking, just checks connectivity)
    running = manager.is_server_running()
    print('\n[TEST] Server running?', running, '(expected False unless llama-server is active)')

    # Test 5: Verify cmd construction matches expected format
    expected_prefix = ['-m', str(test_model)]
    assert args[:2] == expected_prefix, f'FAIL: expected {expected_prefix}, got {args[:2]}'
    print('[TEST] Command prefix matches expected format:', expected_prefix)

    print('\n=== All self-tests PASSED ===')


