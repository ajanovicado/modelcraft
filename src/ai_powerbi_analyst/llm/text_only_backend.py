"""Pinned Qwen 3.5 text-only runner with the shared owned-process watchdog."""

import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Self

from powerbi_rag_runtime.server import sha
from powerbi_rag_runtime.windows_job import Extended, MemoryJob

from .managed_ollama_backend import OllamaServer


class TextOnlyServer(OllamaServer):
    def _verify_files(self) -> None:
        # Frozen source modules live in the signed/pinned sidecar executable.
        code = str(Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve())
        for required in (self.config["server"], self.config["model"], code):
            if required not in self.config["hashes"]:
                raise ValueError("Text-only runtime file is not pinned: " + required)
        for file, expected in self.config["hashes"].items():
            if sha(file) != expected:
                raise ValueError("Runtime/model hash mismatch: " + file)

    def start(self, load: bool = True) -> Self:
        if not load or self.config.get("resource_profile") != "standard":
            raise ValueError("Text-only runner requires the pinned standard profile")
        self.check()
        self._verify_files()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.endpoint = f"http://127.0.0.1:{port}"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("LLAMA_ARG_")}
        self.env.update(OLLAMA_LLAMA_CPP_COMPAT="1")
        command = self._command(port)
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.log = (self.evidence / f"text-only-{time.time_ns()}.log").open("x", encoding="utf-8")
        self.deadline = time.monotonic() + 120
        return self._launch(command)

    def _command(self, port: int) -> list[str]:
        # "none" copies every weight into private RAM and repacks it for faster CPU
        # kernels. "mmap" without repacking was measured on 2026-09-25: this engine
        # converts Qwen's text tensors at load, so they stayed private and prompt
        # reading ran 1.8x slower. It stays selectable for re-testing a future engine.
        load_mode = str(self.config.get("load_mode", "none"))
        if load_mode not in {"none", "mmap"}:
            raise ValueError("Unsupported model load mode: " + load_mode)
        # Speculative decoding drafts tokens and verifies them in one batch, so greedy
        # output is unchanged: "draft-mtp" uses the multi-token-prediction layers
        # Qwen 3.5 files carry, "ngram-mod" drafts from text already in the context.
        spec_type = self.config.get("spec_type")
        if spec_type not in {None, "draft-mtp", "ngram-mod"}:
            raise ValueError(f"Unsupported speculative decoding type: {spec_type}")
        speculative = ["--spec-type", spec_type] if spec_type else []
        return [
            self.config["server"],
            "--model",
            self.config["model"],
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-webui",
            "--offline",
            "--no-mmproj",
            "--load-mode",
            load_mode,
            "--repack" if self.config.get("repack", True) else "--no-repack",
            "--fit",
            "off",
            "-c",
            "4096",
            "-np",
            "1",
            "-ngl",
            "0",
            "-t",
            str(self.config["threads"]),
            "-b",
            "512",
            "-ub",
            "512",
            "--cache-type-k",
            "q8_0",
            "--cache-type-v",
            "q8_0",
            "--flash-attn",
            "on",
            "--no-jinja",
            "--chat-template",
            "chatml",
            "--ctx-checkpoints",
            "2",
            "--cache-ram",
            str(self.config["prompt_cache_mib"]),
            *speculative,
        ]

    def _launch(self, command: list[str]) -> Self:
        try:
            self.process = subprocess.Popen(  # noqa: S603 - every runtime/model file attested above
                command,
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
                    if self.request("/health", timeout=2).get("status") == "ok":
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.2)
            else:
                self.raise_failure()
                raise RuntimeError("Text-only model exited during loading")
            self.check()
            self.deadline = None
            with (self.evidence / "loaded.json").open("x", encoding="utf-8") as stream:
                json.dump(
                    {
                        "backend": "managed-llama-text",
                        "model": self.config["tag"],
                        "vision": False,
                        "context_length": 4096,
                        "command": command,
                    },
                    stream,
                )
            return self
        except BaseException:
            self.stop()
            self.raise_failure()
            raise

    @staticmethod
    def render_messages(messages: list[dict[str, str]]) -> str:
        parts = []
        for message in messages:
            if message.get("role") not in {"system", "user", "assistant"}:
                raise ValueError("Unsupported text-only message role")
            content = message["content"]
            if not isinstance(content, str):
                raise ValueError("Text-only messages must contain text")
            # Keep supplied strings inside their role rather than admitting control tokens.
            for token in ("<|im_start|>", "<|im_end|>", "<|endoftext|>"):
                content = content.replace(token, token.replace("<|", "< |"))
            parts.append(f"<|im_start|>{message['role']}\n{content}<|im_end|>\n")
        # Close the reasoning segment explicitly: native /completion does not
        # apply Ollama's think=false handling for free-form conversation.
        return "".join(parts) + "<|im_start|>assistant\n<think>\n\n</think>\n\n"

    def _check_context(self, messages: list[dict[str, str]], max_tokens: int) -> None:
        prompt = self.render_messages(messages)
        if len(prompt.encode("utf-8")) > 65536:
            raise ValueError("Context input exceeds the bounded tokenizer request")
        request = urllib.request.Request(  # noqa: S310 - owned loopback server
            self.endpoint + "/tokenize",
            data=json.dumps(
                {"content": prompt, "add_special": True, "parse_special": True}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            tokens = json.load(response).get("tokens")
        if not isinstance(tokens, list) or not all(type(token) is int for token in tokens):
            raise ValueError("Native tokenizer returned invalid tokens")
        if len(tokens) + max_tokens > 4096:
            raise ValueError("Context budget exceeded; shorten the conversation or formula")
        self.check()

    def _completion_request(self, payload: dict[str, Any]) -> urllib.request.Request:
        options = payload["options"]
        native = {
            "prompt": self.render_messages(payload["messages"]),
            "stream": True,
            "n_predict": options["num_predict"],
            "temperature": 0,
            "top_k": 0,
            "top_p": 1,
            "min_p": 0,
            "repeat_penalty": 1,
            "seed": options["seed"],
            "stop": ["<|im_end|>", "<|endoftext|>"],
            "cache_prompt": True,
        }
        if payload.get("format") is not None:
            native["json_schema"] = payload["format"]
        return urllib.request.Request(  # noqa: S310 - owned literal loopback endpoint
            self.endpoint + "/completion",
            data=json.dumps(native).encode(),
            headers={"Content-Type": "application/json"},
        )

    @staticmethod
    def _completion_part(line: bytes) -> dict[str, Any] | None:
        line = line.strip()
        # SSE comments keep long prompt-prefill requests alive. They are not
        # completion payloads; event metadata can precede a data line as well.
        if not line or line.startswith((b":", b"event:", b"id:", b"retry:")):
            return None
        if not line.startswith(b"data:"):
            raise ValueError("Invalid native completion stream")
        payload = line[5:].lstrip(b" ")
        if payload == b"[DONE]":
            return None
        part = json.loads(payload)
        if not isinstance(part, dict):
            raise ValueError("Invalid native completion payload")
        if part.get("error"):
            raise RuntimeError(str(part["error"]))
        stopped = bool(part.get("stop"))
        timings = part.get("timings", {})
        return {
            "message": {"content": part.get("content", "")},
            "done": stopped,
            "done_reason": "length" if part.get("stop_type") == "limit" else "stop",
            "eval_count": timings.get("predicted_n", 0),
            "prompt_eval_count": timings.get("prompt_n", 0) + timings.get("cache_n", 0),
            "eval_duration": timings.get("predicted_ms", 0) * 1e6,
        }
