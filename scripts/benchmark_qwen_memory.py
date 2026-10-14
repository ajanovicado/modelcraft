"""Isolated, user-requested low-memory experiment; never edits production profiles."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import psutil
from powerbi_rag_runtime.guards import GIB, exclusive_lock, keep_awake, resources

from ai_powerbi_analyst.llm.managed_ollama_backend import OllamaServer

ROOT = Path(__file__).resolve().parents[1]
TASKS = [
    ("sales", "Total sales, retaining current filters.", "SUM(FactSales[SalesAmount])"),
    (
        "discount_rate",
        "Discount amount as a fraction of sales, using totals, returning zero when sales is zero.",
        "DIVIDE(SUM(FactSales[DiscountAmount]), SUM(FactSales[SalesAmount]), 0)",
    ),
    (
        "online",
        "Total sales for the Online channel only.",
        'CALCULATE(SUM(FactSales[SalesAmount]), DimChannel[ChannelName] = "Online")',
    ),
    (
        "profit",
        "Total sales minus total cost, retaining current filters.",
        "SUM(FactSales[SalesAmount]) - SUM(FactSales[TotalCost])",
    ),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("variant", choices=["4b-f16", "4b-q8", "4b-ssd"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    target = args.output.resolve() / args.variant
    target.mkdir(parents=True, exist_ok=False)
    config_path = ROOT / "runtime/profiles/standard.json"
    config = json.loads(config_path.read_text())
    samples = []
    baseline = resources(target)
    started = time.monotonic()
    report = dict(
        variant=args.variant,
        model=config["tag"],
        baseline=baseline,
        experimental_limits=dict(startup_free_gib=1.5, running_free_gib=1.5, new_paging_mib=512),
        tasks=[],
    )

    def save():
        (target / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (target / "memory.json").write_text(json.dumps(samples), encoding="utf-8")

    def check():
        sample = resources(target)
        samples.append(dict(seconds=round(time.monotonic() - started, 3), **sample))
        if not sample["ac"] or sample["available"] < 1.5 * GIB or sample["disk_free"] < 8 * GIB:
            raise RuntimeError("Experimental resource floor reached: " + json.dumps(sample))
        if any(sample[k] - baseline[k] > 512 * 1024**2 for k in ("swap_used", "native_swapouts")):
            raise RuntimeError("Experimental paging budget reached")
        if time.monotonic() - started > 600 or (target / "STOP_REQUESTED").exists():
            raise RuntimeError("Experimental stop requested or deadline reached")
        return sample

    original_popen = subprocess.Popen

    def launch(*a, **kw):
        # Override only this owned benchmark server's environment. Production
        # files, model weights, package pins and process limits remain unchanged.
        env = dict(kw["env"])
        env["OLLAMA_FLASH_ATTENTION"] = "true" if args.variant in {"4b-q8", "4b-ssd"} else "false"
        env["OLLAMA_KV_CACHE_TYPE"] = "q8_0" if args.variant in {"4b-q8", "4b-ssd"} else "f16"
        report["requested_runtime"] = {
            k: env[k] for k in ("OLLAMA_FLASH_ATTENTION", "OLLAMA_KV_CACHE_TYPE")
        }
        kw["env"] = env
        return original_popen(*a, **kw)

    server = None
    try:
        if baseline["available"] < report["experimental_limits"]["startup_free_gib"] * GIB:
            raise RuntimeError("Not enough free RAM for this bounded experiment")
        if not any(
            (p.info["name"] or "").lower() == "pbidesktop.exe"
            for p in psutil.process_iter(["name"])
        ):
            raise RuntimeError("Keep Power BI Desktop open during the comparison")
        with exclusive_lock(ROOT / "runtime/training/.active-training.lock"), keep_awake():
            server = OllamaServer(config, check, target)
            if args.variant == "4b-ssd":
                server.options["use_mmap"] = True
                report["requested_mmap"] = True
            load_start = time.monotonic()
            with patch("ai_powerbi_analyst.llm.managed_ollama_backend.subprocess.Popen", launch):
                server.start()
            report["load_seconds"] = time.monotonic() - load_start
            print(
                json.dumps(
                    dict(event="loaded", variant=args.variant, seconds=report["load_seconds"])
                ),
                flush=True,
            )
            for key, instruction, expected in TASKS:
                messages = [
                    dict(
                        role="system",
                        content=(
                            "Return JSON with one expression string containing only a scalar DAX "
                            "expression. No measure assignment, Markdown, explanations or query "
                            "statements."
                        ),
                    ),
                    dict(
                        role="user",
                        content=(
                            "Schema: FactSales[SalesAmount], FactSales[DiscountAmount], "
                            "FactSales[TotalCost] are numeric. DimChannel[ChannelName] filters "
                            "FactSales through an active relationship. Request: "
                        )
                        + instruction,
                    ),
                ]
                answer = server.complete(
                    messages,
                    max_tokens=160,
                    seconds=90,
                    json_schema={
                        "type": "object",
                        "properties": {"expression": {"type": "string"}},
                        "required": ["expression"],
                        "additionalProperties": False,
                    },
                )
                report["tasks"].append(dict(id=key, prompt=messages, expected=expected, **answer))
                save()
                print(
                    json.dumps(
                        dict(
                            event="task",
                            variant=args.variant,
                            id=key,
                            seconds=answer["seconds"],
                            output=answer["output"],
                        )
                    ),
                    flush=True,
                )
            report["peak_rss_gib"] = server.peak_tree_rss / GIB
            report["peak_private_gib"] = server.peak_tree_private / GIB
    except Exception as error:  # noqa: BLE001 - preserve any isolated experiment failure
        report["error"] = str(error)
        print(json.dumps(dict(event="failure", variant=args.variant, error=str(error))), flush=True)
    finally:
        if server:
            report["guard_failure"] = str(server.failure) if server.failure else None
            report["peak_rss_gib"] = server.peak_tree_rss / GIB
            report["peak_private_gib"] = server.peak_tree_private / GIB
            try:
                server.stop()
            except Exception as error:  # noqa: BLE001 - save evidence even if cleanup fails
                report["cleanup_error"] = str(error)
        report["minimum_free_gib"] = (
            min(s["available"] for s in samples) / GIB if samples else baseline["available"] / GIB
        )
        report["new_page_output_mib"] = (
            max([s["native_swapouts"] - baseline["native_swapouts"] for s in samples] or [0])
            / 1024**2
        )
        report["after"] = resources(target)
        save()
        print(
            json.dumps(
                {k: v for k, v in report.items() if k not in {"tasks", "baseline", "after"}}
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
