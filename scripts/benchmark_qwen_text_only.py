"""Owned text-only / converted-GGUF experiment, without production changes."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import psutil
from benchmark_qwen_memory import TASKS
from powerbi_rag_runtime.guards import GIB, exclusive_lock, keep_awake, resources
from powerbi_rag_runtime.server import sha
from powerbi_rag_runtime.windows_job import Extended, MemoryJob

from ai_powerbi_analyst.llm.managed_ollama_backend import OllamaServer

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = {
    "type": "object",
    "properties": {"expression": {"type": "string"}},
    "required": ["expression"],
    "additionalProperties": False,
}


class NativeExperiment(OllamaServer):
    """Reuse the owned-process memory watchdog; expose native completion only."""

    def launch(self, command, env, deadline):
        self.check()
        self.deadline = time.monotonic() + deadline
        self.log = (self.evidence / "native.log").open("x", encoding="utf-8")
        self.process = subprocess.Popen(  # noqa: S603 - attested local executable
            command,
            env=env,
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["convert", "original-text", "converted-text"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--converted", type=Path)
    parser.add_argument("--no-repack", action="store_true")
    args = parser.parse_args()
    folder = args.output.resolve() / args.mode
    folder.mkdir(parents=True, exist_ok=False)
    config = json.loads((ROOT / "runtime/profiles/standard.json").read_text())
    baseline = resources(folder)
    started = time.monotonic()
    samples = []
    report = {"mode": args.mode, "baseline": baseline, "no_repack": args.no_repack, "tasks": []}

    def check():
        sample = resources(folder)
        samples.append({"seconds": time.monotonic() - started, **sample})
        if not sample["ac"] or sample["available"] < 1.5 * GIB:
            raise RuntimeError("AC / 1.5 GiB free-memory floor reached")
        if sample["disk_free"] < 8 * GIB:
            raise RuntimeError("8 GiB disk reserve reached")
        if any(sample[k] - baseline[k] > 512 * 1024**2 for k in ("native_swapouts", "swap_used")):
            raise RuntimeError("512 MiB additional paging budget reached")
        if time.monotonic() - started > 600 or (folder / "STOP_REQUESTED").exists():
            raise RuntimeError("Experiment deadline / stop requested")
        return sample

    def save():
        (folder / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (folder / "memory.json").write_text(json.dumps(samples), encoding="utf-8")

    server = None
    try:
        with exclusive_lock(ROOT / "runtime/training/.active-training.lock"), keep_awake():
            if not any(
                (p.info["name"] or "").lower() == "pbidesktop.exe"
                for p in psutil.process_iter(["name"])
            ):
                raise RuntimeError("Power BI must stay open for this comparison")
            check()
            for file, expected in config["hashes"].items():
                if sha(file) != expected:
                    raise RuntimeError("Runtime/model hash mismatch: " + file)
            binaries = Path(config["server"]).parent / "lib/ollama"
            model = Path(config["model"])
            if args.mode == "converted-text":
                if not args.converted:
                    raise ValueError("--converted is required")
                model = args.converted.resolve(strict=True)
                conversion = json.loads((model.parent / "result.json").read_text())
                if conversion.get("error") or sha(model) != conversion["converted_sha256"]:
                    raise ValueError("Converted artifact identity mismatch")
            report["model_sha256"] = sha(model)
            env = dict(os.environ, LLAMA_ARG_CTX_CHECKPOINTS="2", LLAMA_ARG_CACHE_RAM="128")
            # Do not inherit a projector or remote model selection from user environment.
            for key in (
                "LLAMA_ARG_MMPROJ",
                "LLAMA_ARG_MMPROJ_URL",
                "LLAMA_ARG_HF_REPO",
                "LLAMA_ARG_MODEL_URL",
            ):
                env.pop(key, None)
            server = NativeExperiment(config, check, folder)
            if args.mode == "convert":
                converted = folder / "qwen35-4b-text-copy.gguf"
                command = [
                    str(binaries / "llama-quantize.exe"),
                    "--max-buffer-size",
                    "64",
                    str(model),
                    str(converted),
                    "COPY",
                    "4",
                ]
                report["command"] = command
                server.launch(command, env, 180)
                while server.process.poll() is None:
                    server.raise_failure()
                    time.sleep(0.2)
                server.raise_failure()
                if server.process.returncode:
                    raise RuntimeError(f"Conversion exited {server.process.returncode}")
                report["converted_sha256"] = sha(converted)
                report["converted_bytes"] = converted.stat().st_size
                report["conversion_seconds"] = time.monotonic() - started
                print(json.dumps({"event": "converted", **report}), flush=True)
            else:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                server.endpoint = f"http://127.0.0.1:{port}"
                command = [
                    str(binaries / "llama-server.exe"),
                    "--model",
                    str(model),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--no-webui",
                    "--offline",
                    "--no-mmproj",
                    "--log-verbosity",
                    "4",
                    "--load-mode",
                    "mmap",
                    "-c",
                    "4096",
                    "-np",
                    "1",
                    "-ngl",
                    "0",
                    "-t",
                    "4",
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
                ]
                if args.no_repack:
                    command.append("--no-repack")
                report["command"] = command
                load_start = time.monotonic()
                server.launch(command, env, 120)
                while True:
                    server.raise_failure()
                    if server.process.poll() is not None:
                        raise RuntimeError("Native server exited during loading")
                    try:
                        health = server.request("/health", timeout=2)
                        if health.get("status") == "ok":
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.2)
                report["load_seconds"] = time.monotonic() - load_start
                server.deadline = None
                print(
                    json.dumps(
                        {"event": "loaded", "mode": args.mode, "seconds": report["load_seconds"]}
                    ),
                    flush=True,
                )
                for key, instruction, expected in TASKS:
                    check()
                    system = (
                        "Return JSON with one expression string containing only a scalar DAX "
                        "expression. No measure assignment, Markdown, explanations or query "
                        "statements."
                    )
                    user = (
                        "Schema: FactSales[SalesAmount], FactSales[DiscountAmount], "
                        "FactSales[TotalCost] are numeric. DimChannel[ChannelName] filters "
                        "FactSales through an active relationship. Request: " + instruction
                    )
                    # Matches the simple text-only, think=false Qwen35 renderer branch.
                    prompt = (
                        f"<|im_start|>system\n{system}<|im_end|>\n"
                        f"<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n"
                    )
                    server.deadline = time.monotonic() + 90
                    task_start = time.monotonic()
                    answer = server.request(
                        "/completion",
                        {
                            "prompt": prompt,
                            "n_predict": 160,
                            "temperature": 0,
                            "top_k": 0,
                            "top_p": 1,
                            "min_p": 0,
                            "repeat_penalty": 1,
                            "seed": 9162026,
                            "stream": False,
                            "json_schema": SCHEMA,
                            "stop": ["<|im_end|>", "<|endoftext|>"],
                        },
                        timeout=95,
                    )
                    server.raise_failure()
                    server.deadline = None
                    row = {
                        "id": key,
                        "expected": expected,
                        "prompt": prompt,
                        "seconds": time.monotonic() - task_start,
                        "response": answer,
                    }
                    report["tasks"].append(row)
                    save()
                    print(
                        json.dumps(
                            {
                                "event": "task",
                                "id": key,
                                "seconds": row["seconds"],
                                "output": answer.get("content"),
                            }
                        ),
                        flush=True,
                    )
    except Exception as error:  # noqa: BLE001 - experiment records all failures
        report["error"] = str(error)
        print(json.dumps({"event": "failure", "error": str(error)}), flush=True)
    finally:
        if server:
            report["guard_failure"] = str(server.failure) if server.failure else None
            report["peak_rss_gib"] = server.peak_tree_rss / GIB
            report["peak_private_gib"] = server.peak_tree_private / GIB
            server.stop()
        report["minimum_free_gib"] = (
            min([s["available"] for s in samples] or [baseline["available"]]) / GIB
        )
        report["new_page_output_mib"] = (
            max([s["native_swapouts"] - baseline["native_swapouts"] for s in samples] or [0])
            / 1024**2
        )
        report["after"] = resources(folder)
        save()
        print(
            json.dumps(
                {
                    k: v
                    for k, v in report.items()
                    if k not in {"tasks", "baseline", "after", "command"}
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
