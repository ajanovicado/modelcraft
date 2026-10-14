"""Owned Ollama CPU runtime; production copy of the frozen paired benchmark backend."""

import ctypes
import hashlib
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Self

import psutil
from powerbi_rag_runtime.guards import GIB
from powerbi_rag_runtime.server import GuardedServer, sha
from powerbi_rag_runtime.windows_job import Extended, MemoryJob

from .resource_profiles import configured_profile

OPTIONS = dict(
    num_ctx=4096,
    num_predict=512,
    num_thread=4,
    num_gpu=0,
    temperature=0,
    top_k=0,
    top_p=1.0,
    min_p=0.0,
    repeat_penalty=1.0,
    presence_penalty=0.0,
    frequency_penalty=0.0,
    seed=9162026,
)


class OllamaServer(GuardedServer):
    def stop(self) -> None:
        children = []
        if self.process:
            with suppress(psutil.NoSuchProcess):
                children = psutil.Process(self.process.pid).children(recursive=True)
        super().stop()
        if children:
            _, alive = psutil.wait_procs(children, timeout=10)
            if alive:
                raise RuntimeError("Owned Ollama workers did not exit")

    def __init__(
        self,
        config: dict[str, Any],
        check: Callable[..., dict[str, Any]],
        evidence: Path,
    ) -> None:
        profile = configured_profile(config)
        if config["context_tokens"] != 4096 or (
            not 1 <= config["threads"] <= 4 if profile else config["threads"] != 4
        ):
            raise ValueError(
                "This qualified runtime requires 4096 context tokens and four CPU threads"
            )
        super().__init__(config, check, evidence)
        self.peak_tree_rss = 0
        self.peak_tree_private = 0
        self.memory_cap = int((profile.process_gib if profile else 6) * GIB)
        self.options = {**OPTIONS, "num_thread": config["threads"]}

        def guarded() -> dict[str, Any]:
            sample = check()
            if self.process and self.process.poll() is None:
                root = psutil.Process(self.process.pid)
                memory = []
                for child in [root] + root.children(recursive=True):
                    with suppress(psutil.NoSuchProcess):
                        memory.append(child.memory_info())
                rss = sum(m.rss for m in memory)
                private = sum(getattr(m, "private", m.rss) for m in memory)
                self.peak_tree_rss = max(self.peak_tree_rss, rss)
                self.peak_tree_private = max(self.peak_tree_private, private)
                if max(rss, private) > self.memory_cap:
                    raise RuntimeError("Owned model process tree exceeded its profile memory cap")
            return sample

        self.check = guarded

    def start(self, load: bool = True) -> Self:
        self.check()
        for file, expected in self.config["hashes"].items():
            if sha(file) != expected:
                raise ValueError("Runtime/model hash mismatch: " + file)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.endpoint = f"http://127.0.0.1:{port}"
        self.env = dict(
            os.environ,
            OLLAMA_HOST=f"127.0.0.1:{port}",
            OLLAMA_NUM_PARALLEL="1",
            OLLAMA_MAX_LOADED_MODELS="1",
            OLLAMA_CONTEXT_LENGTH="4096",
            OLLAMA_NO_CLOUD="1",
            OLLAMA_KEEP_ALIVE="5m",
            OLLAMA_FLASH_ATTENTION="false",
            OLLAMA_KV_CACHE_TYPE="f16",
        )
        if configured_profile(self.config):
            # Qwen recurrent-state checkpoints are separate from saved prompts.
            # The native default retains 32 checkpoints per slot, which can grow
            # until a small model hits its job limit across different requests.
            self.env["LLAMA_ARG_CTX_CHECKPOINTS"] = "2"
        cache_mib = self.config.get("prompt_cache_mib")
        if cache_mib is not None:
            if type(cache_mib) is not int or not 0 <= cache_mib <= 512:
                raise ValueError("Prompt cache must be between 0 and 512 MiB")
            # llama-server otherwise retains up to 8 GiB of old prompt states,
            # exceeding our unchanged 6 GiB process-tree limit over a session.
            self.env["LLAMA_ARG_CACHE_RAM"] = str(cache_mib)
        if self.config.get("model_store"):
            self.env["OLLAMA_MODELS"] = self.config["model_store"]
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.log = (self.evidence / f"ollama-{time.time_ns()}.log").open("x", encoding="utf-8")
        self.deadline = time.monotonic() + 120
        try:
            self.process = subprocess.Popen(  # noqa: S603 - executable hash checked above
                [self.config["server"], "serve"],
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.job = MemoryJob(self.process, self.memory_cap)
            info = Extended()
            info.basic.flags = 0x2000 | 0x100 | 0x200
            info.process_memory = self.memory_cap
            info.job_memory = self.memory_cap
            if not self.job.kernel.SetInformationJobObject(
                self.job.handle, 9, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            self.watcher = threading.Thread(target=self._watch, daemon=True)
            self.watcher.start()
            while self.process.poll() is None:
                self.raise_failure()
                try:
                    version = self.request("/api/version", timeout=2)
                    break
                except (OSError, ValueError):
                    time.sleep(0.2)
            else:
                raise RuntimeError("Ollama exited; inspect log")
            if load:
                tag = self.config["tag"]
                tag_row = next(m for m in self.request("/api/tags")["models"] if m["name"] == tag)
                if tag_row["digest"] != self.config["package_sha256"]:
                    raise ValueError("Ollama package identity changed")
                show = self.request("/api/show", {"model": tag})
                with (self.evidence / "model-show.json").open("x") as f:
                    json.dump(show, f, indent=2)
                self.request(
                    "/api/generate",
                    {
                        "model": tag,
                        "prompt": "",
                        "stream": False,
                        "keep_alive": "5m",
                        "options": self.options,
                    },
                    timeout=125,
                )
                self.raise_failure()
                loaded = self.request("/api/ps")["models"]
                if (
                    len(loaded) != 1
                    or loaded[0]["name"] != tag
                    or loaded[0].get("size_vram", 0) != 0
                ):
                    raise RuntimeError("Expected exactly the owned CPU model")
                if loaded[0].get("context_length") != 4096:
                    raise RuntimeError("Unexpected context allocation")
                with (self.evidence / "loaded.json").open("x") as f:
                    json.dump({"version": version, "models": loaded}, f, indent=2)
            self.deadline = None
            return self
        except BaseException:
            self.stop()
            raise

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        thinking: bool = False,
        max_tokens: int = 512,
        seed: int = 9162026,
        json_schema: dict[str, Any] | None = None,
        seconds: float = 60,
        on_text: Callable[[str], object] | None = None,
    ) -> dict[str, Any]:
        if thinking:
            raise ValueError("Thinking disabled for both models")
        if not 1 <= max_tokens <= 1024 or not 0 < seconds <= 120:
            raise ValueError("Invalid bounded generation budget")
        if self.stopped.is_set():
            raise RuntimeError("Model session is closed")
        self.raise_failure()
        self.check()
        self._check_context(messages, max_tokens)
        payload = dict(
            model=self.config["tag"],
            messages=messages,
            stream=True,
            think=False,
            keep_alive="5m",
            format=json_schema,
            options={**self.options, "num_predict": max_tokens, "seed": seed},
        )
        request = self._completion_request(payload)
        start = time.monotonic()
        self.deadline = start + min(seconds, 120)
        self.timed_out = False
        raw = ""
        native: dict[str, Any] = {}
        finish = None
        try:
            with urllib.request.urlopen(request, timeout=min(seconds, 120) + 5) as response:  # noqa: S310
                self.response_socket = response.fp.raw._sock
                for line in response:
                    self.raise_failure()
                    part = self._completion_part(line)
                    if part is None:
                        continue
                    if part.get("error"):
                        raise RuntimeError(part["error"])
                    message = part.get("message", {})
                    if message.get("thinking"):
                        raise RuntimeError("Unexpected thinking output")
                    chunk = message.get("content", "")
                    raw += chunk
                    if on_text and chunk:
                        on_text(chunk)
                    if part.get("done"):
                        native = part
                        finish = part.get("done_reason")
                        break
            self.raise_failure()
            if self.timed_out:
                finish = "timeout"
            if finish is None:
                raise RuntimeError("Stream ended without completion")
        except BaseException:
            with (self.evidence / f"partial-{time.time_ns()}.json").open("x") as f:
                json.dump(
                    dict(raw=raw, seconds=time.monotonic() - start, timed_out=self.timed_out),
                    f,
                    indent=2,
                )
            self.raise_failure()
            if self.timed_out:
                finish = "timeout"
            else:
                raise
        finally:
            self.deadline = None
            self.response_socket = None
        prompt_tokens = native.get("prompt_eval_count", 0)
        if prompt_tokens + max_tokens > 4096:
            raise RuntimeError("Context budget exceeded")
        duration = native.get("eval_duration", 0) / 1e6
        tokens = native.get("eval_count", 0)
        return dict(
            raw=raw,
            output=raw.strip(),
            complete=finish == "stop" and bool(raw.strip()),
            error=None if finish == "stop" else "Generation ended " + str(finish),
            finish_reason=finish,
            tokens=tokens,
            prompt_tokens=prompt_tokens,
            seconds=time.monotonic() - start,
            prompt_sha256=hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest(),
            native_timings=dict(
                predicted_ms=duration,
                predicted_per_second=tokens / (duration / 1000) if duration else 0,
            ),
            peak_tree_rss=self.peak_tree_rss,
            peak_tree_private=self.peak_tree_private,
        )

    def _check_context(self, messages: list[dict[str, str]], max_tokens: int) -> None:
        # Ollama does not expose its tokenizer; retain the conservative bound.
        if (
            sum(len(m["content"].encode("utf-8")) for m in messages)
            + max_tokens
            + max(128, 64 * len(messages))
            > 4096
        ):
            raise ValueError("Conservative context bound exceeded; no silent truncation")

    def _completion_request(self, payload: dict[str, Any]) -> urllib.request.Request:
        return urllib.request.Request(  # noqa: S310 - owned literal loopback endpoint
            self.endpoint + "/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

    @staticmethod
    def _completion_part(line: bytes) -> dict[str, Any] | None:
        part = json.loads(line)
        if not isinstance(part, dict):
            raise ValueError("Invalid Ollama completion payload")
        return part
