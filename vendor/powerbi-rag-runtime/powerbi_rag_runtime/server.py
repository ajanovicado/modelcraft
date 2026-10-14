"""Owned loopback process, streaming watchdog and hard resource guards."""
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.request

import psutil
from .guards import GIB


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class GuardedServer:
    """Process ownership and watchdog shared by the managed Ollama backend."""
    def __init__(self, config, check, evidence):
        self.config = config
        self.check = check
        self.evidence = Path(evidence)
        self.process = None
        self.stopped = threading.Event()
        self.failure = None
        self.deadline = None
        self.log = None
        self.tokenizer = None
        self.job = None
        self.response_socket = None
        self.timed_out = False


    def _watch(self):
        next_resources = 0
        while not self.stopped.wait(.25):
            try:
                if self.deadline and time.monotonic() > self.deadline:
                    if self.response_socket is not None:
                        self.timed_out = True
                        self.deadline = None
                        self.response_socket.shutdown(socket.SHUT_RDWR)
                    else:
                        raise TimeoutError('120-second generation/load cap reached')
                if time.monotonic() < next_resources:
                    continue
                next_resources = time.monotonic() + 2
                self.check()
                if self.process and self.process.poll() is None:
                    memory = psutil.Process(self.process.pid).memory_info()
                    if max(memory.rss, getattr(memory, 'private', memory.rss)) > 6 * GIB:
                        raise RuntimeError('llama-server exceeded 6 GiB model memory cap')
            except BaseException as error:
                self.failure = error
                self.stop()
                return


    def request(self, path, data=None, timeout=10):
        request = urllib.request.Request(self.endpoint + path,
                    data=json.dumps(data).encode() if data is not None else None,
                    headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)


    def raise_failure(self):
        if self.failure:
            raise self.failure


    def stop(self):
        self.stopped.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=5)
        watcher = getattr(self, 'watcher', None)
        if watcher and watcher is not threading.current_thread():
            watcher.join(timeout=7)
        if self.log:
            self.log.close(); self.log = None
        if self.job:
            self.job.close(); self.job = None

