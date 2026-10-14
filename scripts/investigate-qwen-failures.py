"""Read-only reference ablation: retry remaining failures with existing focused sources."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from ai_powerbi_analyst.knowledge import KnowledgeLibrary, RetrievalReceipt
from ai_powerbi_analyst.llm.runtime import OllamaModelRuntime
from ai_powerbi_analyst.settings import ModelSettings

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".runtime/qwen-reference-tasks-20260907-031335"


def main() -> None:
    output = (
        ROOT
        / ".runtime"
        / ("qwen-error-investigation-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    )
    output.mkdir(parents=True)
    original_design = json.loads((SOURCE / "design.json").read_text(encoding="utf-8"))
    library = KnowledgeLibrary(ROOT / ".runtime/studio-dev-state/knowledge/index.sqlite3")
    runtime = OllamaModelRuntime(ModelSettings(name="qwen3.5:4b-q4_K_M", chat_output_tokens=320))

    def section(title: str, heading: str):
        matches = [r for r in library.entries.values() if r.title == title and r.section == heading]
        if len(matches) != 1:
            raise ValueError(f"Ambiguous source section: {title}/{heading}")
        return matches[0]

    selections = {
        "fanout": [library.entries["recipe-join-grain"], library.entries["recipe-keys"]],
        "duplicate_keys": [library.entries["recipe-keys"]],
        "nested_join": [
            section("Table.NestedJoin", "Quick reference"),
            section("Table.ExpandTableColumn", "Quick reference"),
        ],
        "locale": [
            section("Table.TransformColumnTypes", "Syntax"),
            section("Table.TransformColumnTypes", "Example 3"),
        ],
    }
    runs = []
    for key, references in selections.items():
        original = next(r for r in original_design["runs"] if r["id"] == key)
        receipt = RetrievalReceipt.model_validate(original["receipt"]).model_copy(
            update={
                "references": references,
                "captured_at": datetime.now(UTC),
                "context_characters": 0,
            }
        )
        receipt = receipt.model_copy(update={"context_characters": len(receipt.context())})
        if receipt.context_characters > 2400:
            raise ValueError(f"Context budget exceeded: {key}: {receipt.context_characters}")
        system = original["system"].split("\nQuoted reference material:\n")[0]
        runs.append(
            {
                "id": key,
                "question": original["question"],
                "selection": "manual diagnostic selection from unchanged existing corpus",
                "receipt": receipt.model_dump(mode="json"),
                "system": system + "\nQuoted reference material:\n" + receipt.context(),
            }
        )
    try:
        identity = runtime.capture_attestation().model_dump(mode="json")
        (output / "design.json").write_text(
            json.dumps(
                {
                    "original_run": str(SOURCE),
                    "identity": identity,
                    "effective_chat_temperature": 0.2,
                    "runs": runs,
                    "changes": "Only the supplied reference selection changes; no new docs.",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(str(output), flush=True)
        for run in runs:
            started = time.perf_counter()
            answer = "".join(runtime.stream_chat(run["question"], system_prompt=run["system"]))
            result = {
                "id": run["id"],
                "answer": answer,
                "seconds": time.perf_counter() - started,
                "generation": runtime.last_generation.model_dump()
                if runtime.last_generation
                else None,
            }
            (output / (run["id"] + ".json")).write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
            print(json.dumps(result), flush=True)
    finally:
        runtime.unload()
        library.close()


if __name__ == "__main__":
    main()
