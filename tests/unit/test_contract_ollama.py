import json
from contextlib import contextmanager

import pytest

from ai_powerbi_analyst.llm.contract_ollama import ArtifactAnswer, ContractOllamaModelRuntime
from ai_powerbi_analyst.llm.factory import create_model_runtime
from ai_powerbi_analyst.llm.runtime import LocalModelError
from ai_powerbi_analyst.settings import ModelSettings


def test_factory_selects_validated_warm_runtime(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"warm_session": True, "validated_artifacts": True}))
    model = create_model_runtime(ModelSettings(runtime_config_path=str(path)))
    assert type(model) is ContractOllamaModelRuntime


def configured(tmp_path, monkeypatch, replies):
    model = ContractOllamaModelRuntime(ModelSettings(), tmp_path / "evidence")
    calls = []

    class Server:
        def complete(self, messages, **kwargs):
            calls.append({"messages": list(messages), **kwargs})
            return dict(
                output=json.dumps(replies.pop(0)),
                complete=True,
                tokens=10,
                seconds=0.01,
                prompt_tokens=30,
                native_timings={},
            )

    @contextmanager
    def session():
        target = tmp_path / str(len(calls))
        target.mkdir()
        yield Server(), target, tmp_path

    monkeypatch.setattr(model, "_session", session)
    monkeypatch.setattr(model, "_references", lambda *args: ("", []))
    return model, calls


def test_wrong_measure_gets_one_actionable_repair(tmp_path, monkeypatch):
    prompt = (
        "Write a DAX measure named FreightTotal summing Loads[Freight]. Loads has columns Freight."
    )
    model, calls = configured(
        tmp_path,
        monkeypatch,
        [
            {"artifact": "Wrong = SUM(Examples[Cost])"},
            {"artifact": "FreightTotal = SUM(Loads[Freight])"},
        ],
    )
    answer = model.generate("Return an artifact", prompt, ArtifactAnswer)
    assert answer.artifact.startswith("FreightTotal =")
    assert len(calls) == 2
    assert "requested measure name" in calls[1]["messages"][-1]["content"]


def test_zero_repairs_is_honored(tmp_path, monkeypatch):
    model, calls = configured(tmp_path, monkeypatch, [{"artifact": "Other = SUM(Loads[Freight])"}])
    with pytest.raises(LocalModelError):
        model.generate(
            "Return artifact",
            "Write a DAX measure named FreightTotal summing Loads[Freight].",
            ArtifactAnswer,
            repair_attempts=0,
        )
    assert len(calls) == 1


def test_relationship_is_typed_validated_and_rendered(tmp_path, monkeypatch):
    model, calls = configured(
        tmp_path,
        monkeypatch,
        [
            dict(
                from_ref="Keys[Id]",
                to_ref="Details[Id]",
                cardinality="1:1",
                is_active=True,
                filter_direction="both",
            ),
        ],
    )
    result = model.generate(
        "Return artifact",
        "Give the active one-to-one relationship for Keys[Id] and Details[Id].",
        ArtifactAnswer,
    )
    assert "Active status: Yes" in result.artifact
    assert "complete Table[Column]" in calls[0]["messages"][0]["content"]
    assert set(calls[0]["json_schema"]["required"]) == {
        "from_ref",
        "to_ref",
        "cardinality",
        "is_active",
        "filter_direction",
    }


def test_explicit_chat_with_history_buffers_validation(tmp_path, monkeypatch):
    model, calls = configured(
        tmp_path,
        monkeypatch,
        [
            {"artifact": "Wrong = SUM(Weights[Kg])"},
            {"artifact": "NetKg = SUM(Weights[Kg])"},
        ],
    )
    chunks = list(
        model.stream_chat(
            "Write a DAX measure named NetKg summing Weights[Kg].",
            system_prompt="Be helpful.",
            history=[{"role": "assistant", "content": "Prior discussion"}],
        )
    )
    assert chunks == ["NetKg = SUM(Weights[Kg])"]
    assert calls[0]["messages"][1]["content"] == "Prior discussion"
    assert all(c["max_tokens"] == model.settings.chat_output_tokens for c in calls)
    assert model._messages("System", "Next", "") == [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "Next"},
    ]


def test_relationship_explanation_keeps_original_response(tmp_path, monkeypatch):
    model, calls = configured(tmp_path, monkeypatch, [{"artifact": "The keys identify rows."}])
    answer = model.generate(
        "Explain clearly",
        "Give the relationship between Keys[Id] and Details[Id] and explain why it works.",
        ArtifactAnswer,
    )
    assert answer.artifact == "The keys identify rows."
    assert calls[0]["json_schema"]["required"] == ["artifact"]


def test_sql_chat_repairs_undefined_alias_before_yielding(tmp_path, monkeypatch):
    model, calls = configured(
        tmp_path,
        monkeypatch,
        [{"artifact": "SELECT x.Id FROM Records r"}, {"artifact": "SELECT r.Id FROM Records r"}],
    )
    chunks = list(
        model.stream_chat(
            "DuckDB Records(Id INTEGER) contains (2),(5). "
            "Return only a SQL SELECT of all Id values.",
            system_prompt="Use the supplied schema.",
        )
    )
    assert chunks == ["SELECT r.Id FROM Records r"]
    assert len(calls) == 2
    assert "does not bind" in calls[1]["messages"][-1]["content"]


def test_m_chat_buffers_syntax_failure_and_bounded_repair(tmp_path, monkeypatch):
    valid = 'let S=#table({"Q"},{{3}}), A=Table.AddColumn(S,"Twice",each [Q]*2) in A'
    model, calls = configured(
        tmp_path,
        monkeypatch,
        [
            {"artifact": 'let S=#table({"Q"},{{3}}), A=S each with columns(A) in A'},
            {"artifact": valid},
        ],
    )
    chunks = list(
        model.stream_chat(
            'Write a complete Power Query M expression using #table({"Q"},{{3}}). '
            "Add Twice = Q * 2.",
            system_prompt="Return the artifact.",
        )
    )
    assert chunks == [valid]
    assert len(calls) == 2
    assert "Invalid M syntax" in calls[1]["messages"][-1]["content"]
    assert "M syntax example" not in calls[1]["messages"][-1]["content"]
