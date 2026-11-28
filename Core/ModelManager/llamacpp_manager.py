"""Llama.cpp CUDA Server Manager & Watchdog for OMEGA.

Manages llama-server.exe lifecycle and Gemma-4-26B-A4B MoE GGUF model,
optimized for RTX 3080 (10GB VRAM) + Ryzen 7 9700X (32GB DDR5),
with non-blocking unified stdout/stderr log bridging.
"""

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from Core.Logger.logger import (
    get_logger,
    pump_subprocess_stream,
    setup_unified_logging,
)

setup_unified_logging()
logger = get_logger('llamacpp_manager')

# ---------------------------------------------------------------------------
# Constants & Defaults (RTX 3080 / Ryzen 7 optimized)
# ---------------------------------------------------------------------------
SERVER_HOST = '127.0.0.1'
DEFAULT_SERVER_PORT = 8080
DEFAULT_CONTEXT_LENGTH = 16384
DEFAULT_OT_PATTERN = r'blk\.([89]|[1-9][0-9])\..*exps=CPU'
HEALTH_URL = f'http://{SERVER_HOST}:{DEFAULT_SERVER_PORT}/health'
BASE_URL = f'http://{SERVER_HOST}:{DEFAULT_SERVER_PORT}/v1'

OPTIMAL_FLAGS = [
    '-np', '1',
    '-ngl', '99',
    '--load-mode', 'none',
    '-fa', 'on',
    '--jinja',
    '--reasoning-budget', '0',
]


# ---------------------------------------------------------------------------
# LlamaCppManager class
# ---------------------------------------------------------------------------
class LlamaCppManager:
    """Manages llama-server.exe CUDA server and Gemma-4-26B-A4B GGUF model."""

    def __init__(self) -> None:
        self._server_process: Optional[subprocess.Popen] = None
        self._log_pump_thread = None
        self._load_config()

    @property
    def _config(self):
        if not hasattr(self, '_loaded_config'):
            self._load_config()
        return getattr(self, '_loaded_config', {})

    def _load_config(self) -> None:
        """Load OMEGA config from Configuration/config.json."""
        import json
        try:
            config_path = _PROJECT_ROOT / 'Configuration' / 'config.json'
            with open(config_path, encoding='utf-8') as f:
                data = json.load(f)
            self._loaded_config = data.get('llama_cpp', {})
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning('Could not read llama_cpp config (%s); using built-in defaults', exc)
            self._loaded_config = {
                'server_binary_path': 'D:/AI/llama.cpp/llama-server.exe',
                'server_exe_path': 'D:/AI/llama.cpp/llama-server.exe',
                'model_gguf_path': 'D:/AI/Models/Gemma-4-26B-A4B-IQ4_XS/gemma-4-26B-A4B-it-UD-IQ4_XS.gguf',
                'target_model_name': 'Gemma 4 26B-A4B IT (IQ4_XS)',
                'parallel': 1,
                'gpu_layers': 99,
                'load_mode': 'none',
                'context_length': 16384,
                'threads': 8,
                'threads_batch': 12,
                'batch_size': 2048,
                'ubatch_size': 512,
                'flash_attn': True,
                'cache_type_k': 'q8_0',
                'cache_type_v': 'q8_0',
                'override_tensor_pattern': DEFAULT_OT_PATTERN,
                'jinja': True,
                'reasoning_budget': 0,
                'enable_thinking': False,
                'temperature': 0.6,
                'top_p': 0.95,
                'host': SERVER_HOST,
                'port': DEFAULT_SERVER_PORT,
                'log_disable': False,
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
        """Build the full verified command-line arguments for llama-server.exe."""
        parallel = self._config.get('parallel', 1)
        gpu_layers = self._config.get('gpu_layers', self._config.get('n_gpu_layers', 99))
        load_mode = self._config.get('load_mode', 'none')
        ot_pattern = self._config.get('override_tensor_pattern', DEFAULT_OT_PATTERN)
        ctx_len = self._config.get('context_length', DEFAULT_CONTEXT_LENGTH)
        batch = self._config.get('batch_size', 2048)
        ubatch = self._config.get('ubatch_size', 512)
        threads = self._config.get('threads', self._config.get('cpu_threads', 8))
        threads_batch = self._config.get('threads_batch', 12)
        jinja = self._config.get('jinja', True)
        reasoning_budget = self._config.get('reasoning_budget', 0)
        temperature = self._config.get('temperature', 0.6)
        top_p = self._config.get('top_p', 0.95)

        args = [
            '-m', str(model_path),
            '--host', str(self.host),
            '--port', str(self.port),
        ]

        if parallel is not None:
            args += ['-np', str(parallel)]

        args += ['-ngl', str(gpu_layers)]

        if load_mode is not None and str(load_mode).strip():
            args += ['--load-mode', str(load_mode).strip()]

        if ot_pattern:
            args += ['-ot', str(ot_pattern)]

        args += [
            '-c', str(ctx_len),
            '-b', str(batch),
            '-ub', str(ubatch),
        ]

        if self._config.get('flash_attn', True):
            args += ['-fa', 'on']

        cache_k = self._config.get('cache_type_k', self._config.get('kv_cache_type', 'q8_0'))
        cache_v = self._config.get('cache_type_v', self._config.get('kv_cache_type', 'q8_0'))
        if cache_k:
            args += ['-ctk', str(cache_k)]
        if cache_v:
            args += ['-ctv', str(cache_v)]

        args += [
            '-t', str(threads),
            '-tb', str(threads_batch),
        ]

        if jinja:
            args.append('--jinja')

        if reasoning_budget is not None:
            args += ['--reasoning-budget', str(reasoning_budget)]

        if temperature is not None:
            args += ['--temp', str(temperature)]

        if top_p is not None:
            args += ['--top-p', str(top_p)]

        # Keep llama-server logs enabled by default so unified pumper captures CUDA/VRAM/load telemetry
        if bool(self._config.get('log_disable', False)):
            args.append('--log-disable')

        return args

    # -------------------------------------------------------------------
    def is_server_running(self) -> bool:
        """Query health endpoint to check if server is alive."""
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
        """Locate llama-server.exe from config, known paths, or system PATH."""
        exe_name = self._config.get('server_binary_path', self._config.get('server_exe_path', 'D:/AI/llama.cpp/llama-server.exe'))

        if Path(exe_name).is_absolute() and Path(exe_name).exists():
            return str(Path(exe_name))

        local_candidates = [
            Path('D:/AI/llama.cpp/llama-server.exe'),
            _PROJECT_ROOT / 'bin' / Path(exe_name).name,
            _PROJECT_ROOT / 'Models' / 'llama-server.exe',
        ]
        for candidate in local_candidates:
            if candidate and candidate.exists():
                return str(candidate)

        import shutil
        found = shutil.which(exe_name) or shutil.which('llama-server.exe')
        if found:
            return found

        raise FileNotFoundError(
            f'Cannot find llama-server.exe in config or PATH.\n'
            f'Searched: {exe_name}\n'
            f'Or set "server_binary_path" in Configuration/config.json.'
        )

    # -------------------------------------------------------------------
    def find_model_gguf(self) -> str:
        """Locate the target GGUF model file on disk."""
        config_path = self._config.get('model_gguf_path', '')
        if config_path and Path(config_path).is_file():
            return str(Path(config_path))

        search_dirs = [
            _PROJECT_ROOT / 'Models',
            _PROJECT_ROOT / '_models' / 'gguf',
            Path('D:/AI/Models'),
            Path.home() / '.cache' / 'huggingface' / 'hub',
        ]

        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            for gguf_file in search_dir.rglob('*.gguf'):
                name_lower = gguf_file.name.lower()
                if ('gemma' in name_lower and '26b' in name_lower) or ('qwen' in name_lower and ('35b' in name_lower or '9b' in name_lower)):
                    return str(gguf_file)

        if config_path:
            raise FileNotFoundError(
                f'Model file not found at configured path:\n{config_path}\n\n'
                'Please download the target GGUF model and place it there, or update config.json.'
            )

        raise FileNotFoundError(
            'Could not find any matching .gguf file in known model directories.\n'
            f'Searched: {[str(d) for d in search_dirs]}'
        )

    # -------------------------------------------------------------------
    def start_server(self) -> bool:
        """Start llama-server.exe as a hidden background process with unified non-blocking log streaming."""
        if self.is_server_running():
            logger.info('llama-server already running on %s:%s (adopting)', self.host, self.port)
            return True

        try:
            binary_path = self.find_llama_server_binary()
        except FileNotFoundError as e:
            logger.error('%s', e)
            return False

        model_path = ''
        try:
            model_path = self.find_model_gguf()
        except FileNotFoundError:
            logger.warning('Model GGUF not found yet; attempting launch without resolved model path.')

        args = self._build_server_args(model_path)
        cmd = [binary_path] + args

        logger.info('Launching llama-server.exe: %s', ' '.join(cmd))
        t0 = time.perf_counter()

        try:
            self._server_process = subprocess.Popen(
                cmd,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                shell=False,
            )
            logger.info('llama-server spawned (PID=%d)', self._server_process.pid)
            # Start non-blocking stream pumper immediately so Windows pipe can never deadlock
            self._log_pump_thread = pump_subprocess_stream(
                self._server_process.stdout,
                module_name='llama_server',
                default_level=logging.INFO,
                json_prefix='',
                thread_name='LlamaServerLogPump',
            )
        except OSError as e:
            logger.exception('Failed to launch llama-server.exe: %s', e)
            return False

        time.sleep(1.5)

        if self._server_process.poll() is not None:
            logger.error('llama-server exited immediately with returncode=%s (see llama_server logs above)', self._server_process.returncode)
            return False

        if not self.ensure_model_loaded():
            return False

        logger.info('llama-server startup & model load completed in %.2fs', time.perf_counter() - t0)
        return True

    # -------------------------------------------------------------------
    def ensure_model_loaded(self, timeout_seconds: int = 120) -> bool:
        """Poll /health endpoint until model is loaded or timeout."""
        import urllib.request
        url = f'http://{self.host}:{self.port}/health'
        t0 = time.perf_counter()
        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            if self._server_process is not None and self._server_process.poll() is not None:
                logger.error('Managed llama-server crashed while loading model (returncode=%s)', self._server_process.returncode)
                return False
            try:
                req = urllib.request.Request(url, method='GET')
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = resp.read().decode('utf-8', errors='replace').strip()
                    if 'ok' in data.lower():
                        logger.info('llama-server /health OK (ready in %.1fs)', time.perf_counter() - t0)
                        return True
            except Exception:
                pass
            time.sleep(2)

        logger.error('Timeout waiting for llama-server model load (%ss)', timeout_seconds)
        return False

    # -------------------------------------------------------------------
    def verify_model_ready(self, timeout_seconds: int = 30) -> bool:
        """Send a tiny completion ping to confirm the model is responsive."""
        import urllib.request
        import json as _json

        url = f'{self.base_url}/chat/completions'
        payload = _json.dumps({
            'model': self._config.get('target_model_name', ''),
            'messages': [{'role': 'user', 'content': 'Hi'}],
            'max_tokens': 1,
            'temperature': 0.0,
            'chat_template_kwargs': {'enable_thinking': False},
        }).encode('utf-8')

        t0 = time.perf_counter()
        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            try:
                req = urllib.request.Request(url, data=payload, method='POST')
                req.add_header('Content-Type', 'application/json')
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = _json.loads(resp.read().decode('utf-8'))
                    if isinstance(body, dict) and body.get('choices'):
                        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                        logger.info(
                            'Model readiness probe passed in %.1fms (chars=%d)',
                            elapsed_ms,
                            len(body['choices'][0].get('message', {}).get('content') or ''),
                        )
                        return True
            except Exception as exc:
                logger.debug('verify_model_ready probe retry: %s', exc)
            time.sleep(2)

        logger.error('verify_model_ready timed out after %ss', timeout_seconds)
        return False

    # -------------------------------------------------------------------
    def recover_connection(self, max_retries: int = 5) -> bool:
        """Attempt exponential-backoff recovery of a lost server connection."""
        if self._server_process and self._server_process.poll() is not None:
            logger.warning('Managed llama-server died (rc=%s); attempting restart...', self._server_process.returncode)
            return self.start_server()

        import urllib.request
        url = f'http://{self.host}:{self.port}/health'
        delay = 1.0

        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(url, method='GET')
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = resp.read().decode('utf-8', errors='replace').strip()
                    if 'ok' in data.lower():
                        logger.info('llama-server connection recovered (attempt %d/%d)', attempt + 1, max_retries)
                        return True
            except Exception:
                pass

            delay *= 2
            logger.warning('llama-server reconnect retry %d/%d: waiting %.0fs...', attempt + 1, max_retries, delay)
            time.sleep(delay)

        logger.error('llama-server connection recovery failed after %d retries', max_retries)
        return False

    # -------------------------------------------------------------------
    def stop_managed_server(self) -> bool:
        """Clean shutdown of the managed llama-server process."""
        import signal

        if self._server_process is None:
            logger.info('No managed llama-server process to stop.')
            return True

        pid = self._server_process.pid

        try:
            if hasattr(signal, 'SIGTERM'):
                self._server_process.send_signal(signal.SIGTERM)
                logger.info('Sent SIGTERM to managed llama-server PID %d', pid)
            else:
                subprocess.run(
                    ['taskkill', '/F', '/PID', str(pid)],
                    capture_output=True, text=True
                )
        except Exception as e:
            logger.warning('SIGTERM failed for llama-server PID %d: %s', pid, e)

        deadline = time.time() + 10
        while time.time() < deadline:
            if self._server_process.poll() is not None:
                logger.info('Managed llama-server (PID %d) stopped gracefully.', pid)
                break
            time.sleep(0.5)
        else:
            try:
                self._server_process.kill()
                self._server_process.wait(timeout=5)
                logger.warning('Forced kill of llama-server PID %d', pid)
            except Exception as e:
                logger.exception('Kill failed for llama-server PID %d: %s', pid, e)

        self._server_process = None
        return True


ModelManager = LlamaCppManager


if __name__ == '__main__':
    print('=== LlamaCppManager Self-Test ===')

    manager = LlamaCppManager()
    print('\n[TEST] Host:', manager.host)
    print('[TEST] Port:', manager.port)
    print('[TEST] Base URL:', manager.base_url)

    test_model = 'D:/AI/Models/test-model.gguf'
    args = manager._build_server_args(test_model)
    assert '-m' in args and args[args.index('-m') + 1] == test_model
    assert '--host' in args and args[args.index('--host') + 1] == manager.host
    assert '--port' in args and args[args.index('--port') + 1] == str(manager.port)

    for flag in ('-np', '-ngl', '--load-mode', '-ot', '-c', '-b', '-ub', '-fa', '-ctk', '-ctv', '-t', '-tb', '--jinja', '--reasoning-budget', '--temp', '--top-p'):
        assert flag in args, f'FAIL: required flag {flag} missing from command args'

    assert args[args.index('-np') + 1] == '1'
    assert args[args.index('--load-mode') + 1] == 'none'
    assert args[args.index('-fa') + 1] == 'on'
    assert args[args.index('-ctk') + 1] == 'q8_0'
    assert args[args.index('-ctv') + 1] == 'q8_0'
    assert args[args.index('--reasoning-budget') + 1] == '0'
    assert args[args.index('-ot') + 1] == DEFAULT_OT_PATTERN, f'FAIL: -ot mismatch: {args[args.index("-ot") + 1]}'

    print('[TEST] Full args:', ' '.join(args))
    print('[TEST] Server running?', manager.is_server_running())
    print('\n=== All self-tests PASSED ===')