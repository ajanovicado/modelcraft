"""Opt-in local model smoke test using retrieved docs; never writes a report."""

import argparse
import json
import time
from pathlib import Path

from ai_powerbi_analyst.knowledge import KnowledgeLibrary
from ai_powerbi_analyst.knowledge.library import REFERENCE_RULES
from ai_powerbi_analyst.llm.runtime import OllamaModelRuntime
from ai_powerbi_analyst.settings import ModelSettings

QUESTIONS = [
    "In Power Query Table.NestedJoin, what join kind is used when joinKind is omitted? "
    "Is keyEqualityComparers intended for normal public use? Answer in under 60 words.",
    "In DAX DIVIDE, can alternateresult be a dynamic measure? What is returned on zero "
    "denominator if alternateresult is omitted? Answer in under 60 words.",
    "Does Power Query Table.Buffer always make a query faster, and what can it do to "
    "downstream query folding? Answer in under 60 words.",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    library = KnowledgeLibrary(args.state_root / "knowledge/index.sqlite3")
    model = OllamaModelRuntime(ModelSettings(max_output_tokens=240))
    runs = []
    try:
        identity = model.capture_attestation().model_dump(mode="json")
        for question in QUESTIONS:
            receipt = library.retrieve(question, max_characters=2400)
            system = (
                "Answer the reference question accurately. Do not claim to execute code or "
                "change a report. Say if the supplied reference is insufficient."
                + REFERENCE_RULES
                + "\nQuoted documentation:\n"
                + receipt.context()
            )
            start = time.perf_counter()
            answer = "".join(model.stream_chat(question, system_prompt=system))
            runs.append(
                {
                    "question": question,
                    "system": system,
                    "answer": answer,
                    "receipt": receipt.model_dump(mode="json"),
                    "elapsed_seconds": time.perf_counter() - start,
                    "model": identity,
                    "generation": model.last_generation.model_dump()
                    if model.last_generation
                    else None,
                }
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(runs, indent=2), encoding="utf-8")
            print(json.dumps({"question": question, "answer": answer}), flush=True)
    finally:
        model.unload()
        library.close()


if __name__ == "__main__":
    main()
