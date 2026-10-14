"""Check the frozen engine's model identity without loading the model."""
import json
from pathlib import Path
import queue
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

import psutil

root = Path(__file__).resolve().parents[1]
target = root / '.runtime' / ('qualified-package-smoke-' + uuid.uuid4().hex)
target.mkdir()
engine = root / 'apps/desktop/src-tauri/binaries/ai-powerbi-studio-engine-x86_64-pc-windows-msvc.exe'
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
with (target/'stderr.log').open('x', encoding='utf-8') as log:
    process = subprocess.Popen([str(engine), '--port', str(port), '--emit-token',
                                '--state-root', str(target/'state')],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=log, text=True, encoding='utf-8',
                               creationflags=subprocess.CREATE_NO_WINDOW)
    lines = queue.Queue()
    def read():
        for line in process.stdout:
            lines.put(line)
    threading.Thread(target=read, daemon=True).start()
    try:
        started = json.loads(lines.get(timeout=90))
        request = urllib.request.Request(f'http://127.0.0.1:{port}/v1/health',
                    headers={'Authorization': 'Bearer ' + started['token']})
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    health = json.load(response)
                break
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.25)
        assert health['model_name'] == 'qwen3.5:4b-q4_K_M', health
        assert health['model_runtime'] == 'ollama', health
        with (target/'health.json').open('x', encoding='utf-8') as out:
            json.dump(health, out, indent=2)
        print(json.dumps({'identity_verified': True, 'inference_executed': False,
                          'health_receipt': str(target/'health.json'),
                          'model_available': health['model_available'],
                          'model_error': health.get('model_error')}))
    finally:
        owned = psutil.Process(process.pid).children(recursive=True)
        for child in owned:
            child.terminate()
        process.terminate()
        process.wait(timeout=15)
