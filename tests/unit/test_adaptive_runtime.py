import hashlib
import json

import pytest
from powerbi_rag_runtime import guards

from ai_powerbi_analyst.llm import adaptive
from ai_powerbi_analyst.llm.artifact_contracts import RelationshipAnswer, dax_problems
from ai_powerbi_analyst.llm.factory import create_model_runtime
from ai_powerbi_analyst.llm.hardware import Hardware
from ai_powerbi_analyst.llm.resource_profiles import PROFILES, configured_profile
from ai_powerbi_analyst.llm.runtime import LocalModelError, ModelHealth
from ai_powerbi_analyst.settings import ModelSettings

GIB = 1024**3


def sample(free=7, disk=20, ac=True, paging=0):
    return dict(
        available=int(free * GIB), disk_free=disk * GIB, ac=ac, native_swapouts=paging, swap_used=0
    )


def test_profile_budget_cannot_be_assigned_to_a_different_model():
    with pytest.raises(ValueError, match="pinned model"):
        configured_profile(
            {
                "resource_profile": "standard",
                "tag": "qwen2.5-coder:1.5b-instruct-q4_K_M",
                "package_sha256": PROFILES["standard"].digest,
            }
        )


def make_runtime(tmp_path, monkeypatch, available, total=16):
    catalog = {"adaptive_profiles": 1, "profiles": {}}
    for key, profile in PROFILES.items():
        cfg = {
            "backend": "managed-llama-text",
            "resource_profile": key,
            "tag": profile.tag,
            "package_sha256": profile.digest,
            "prompt_cache_mib": profile.cache_mib,
            "hashes": {},
            "context_tokens": 4096,
        }
        path = tmp_path / (key + ".json")
        path.write_text(json.dumps(cfg))
        catalog["profiles"][key] = {
            "config": path.name,
            "qualified": True,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog))
    reading = [Hardware(int(total * GIB), int(available * GIB), 4)]
    monkeypatch.setattr(Hardware, "detect", lambda: reading[0])
    model = adaptive.AdaptiveQwenRuntime(
        ModelSettings(runtime_config_path=str(path)), tmp_path / "evidence"
    )
    return model, reading


def test_qwen_4b_is_the_only_local_model(monkeypatch, tmp_path):
    runtime, _ = make_runtime(tmp_path, monkeypatch, 8)
    assert set(PROFILES) == {"standard"}
    assert PROFILES["standard"].tag == "qwen3.5:4b-q4_K_M"
    assert set(runtime._entries) == {"standard"}


def test_a_stale_catalog_entry_for_a_retired_profile_is_ignored(monkeypatch, tmp_path):
    runtime, _ = make_runtime(tmp_path, monkeypatch, 8)
    catalog = json.loads((tmp_path / "catalog.json").read_text())
    catalog["profiles"]["coder"] = {"config": "coder.json", "qualified": True, "sha256": "0" * 64}
    (tmp_path / "catalog.json").write_text(json.dumps(catalog))
    assert set(runtime._read_catalog()) == {"standard"}


def test_a_retired_only_payload_does_not_take_the_studio_down(monkeypatch, tmp_path):
    # Desktop payloads built before 2026-09-25 qualified only the retired Light profile.
    catalog = {
        "adaptive_profiles": 1,
        "profiles": {
            "coder": {"config": "profiles/coder.json", "qualified": True, "sha256": "0" * 64},
            "standard": {"qualified": False},
        },
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog))
    monkeypatch.setattr(Hardware, "detect", lambda: Hardware(16 * GIB, 8 * GIB, 4))
    runtime = create_model_runtime(ModelSettings(runtime_config_path=str(path)), tmp_path)
    assert isinstance(runtime, adaptive.AdaptiveQwenRuntime)
    health = runtime.health()
    assert not health.model_available
    assert health.error == "Qwen 4B is not installed yet. Install it from Settings."
    with pytest.raises(LocalModelError, match="not installed"):
        runtime.generate("system", "hello", object)
    assert not runtime._lock.locked()


@pytest.mark.parametrize(("total", "free"), [(8, 1), (8, 0.25), (4, 0.5), (16, 0), (0, 0)])
def test_qwen_4b_starts_whatever_the_free_memory(monkeypatch, tmp_path, total, free):
    runtime, _ = make_runtime(tmp_path, monkeypatch, free, total=total)
    child = runtime._pick()
    assert child.settings.name == PROFILES["standard"].tag
    assert runtime.prompt_bytes_per_token() == 3.0
    # Battery power, a full disk and paging do not refuse the session either.
    reading = sample(free, disk=0, ac=False, paging=4 * GIB)
    monkeypatch.setattr(guards, "resources", lambda _: dict(reading))
    _, cfg = child._configuration()
    assert child._guard(tmp_path, cfg).check(startup=True) == reading


def test_memory_changes_never_switch_or_stop_the_model(monkeypatch, tmp_path):
    runtime, reading = make_runtime(tmp_path, monkeypatch, 8)
    first = runtime._pick()
    assert first.settings.name == PROFILES["standard"].tag
    first._active = object()
    reading[0] = Hardware(16 * GIB, GIB // 2, 4)
    assert runtime._pick() is first  # the loaded session keeps running
    first._active = None
    reading[0] = Hardware(8 * GIB, GIB // 4, 4)
    assert runtime._pick() is first  # and the next session starts the same model


def test_health_reports_memory_without_a_startup_requirement(monkeypatch, tmp_path):
    runtime, _ = make_runtime(tmp_path, monkeypatch, 1, total=8)
    child = runtime._pick()
    monkeypatch.setattr(
        child,
        "health",
        lambda: ModelHealth(
            service_available=True, model_available=True, endpoint="local", available_memory_gb=1.0
        ),
    )
    health = runtime.health()
    assert health.model_available
    assert (health.available_memory_gb, health.total_memory_gb) == (1.0, 8.0)
    assert health.startup_memory_gb is None
    assert health.memory_profile == PROFILES["standard"].label
    assert not health.automatic_model_selection


def test_unavailable_health_reports_the_cause_not_memory(monkeypatch, tmp_path):
    runtime, _ = make_runtime(tmp_path, monkeypatch, 0.5, total=8)
    (tmp_path / "standard.json").write_text("{}")
    health = runtime.health()
    assert not health.model_available
    assert "hash mismatch" in health.error
    assert (health.available_memory_gb, health.total_memory_gb) == (0.5, 8.0)
    assert health.startup_memory_gb is None
    assert health.memory_profile == PROFILES["standard"].label
    assert runtime._current is None


def test_missing_model_files_are_reported_as_not_installed(monkeypatch, tmp_path):
    runtime, _ = make_runtime(tmp_path, monkeypatch, 8)
    runtime._entries["standard"]["config"]["hashes"] = {str(tmp_path / "gone.gguf"): "0" * 64}
    with pytest.raises(LocalModelError, match="not installed"):
        runtime._pick()
    assert runtime._current is None


def test_changed_profile_configuration_fails_closed(monkeypatch, tmp_path):
    runtime, _ = make_runtime(tmp_path, monkeypatch, 8)
    (tmp_path / "standard.json").write_text("{}")
    with pytest.raises(LocalModelError, match="hash mismatch"):
        runtime._pick()


def test_warm_profile_cannot_ignore_config_changes(monkeypatch, tmp_path):
    runtime, _ = make_runtime(tmp_path, monkeypatch, 8)
    runtime._pick()._active = object()
    (tmp_path / "standard.json").write_text("{}")
    with pytest.raises(LocalModelError, match="hash mismatch"):
        runtime._pick()


def test_relationship_answers_keep_semantic_validation(monkeypatch, tmp_path):
    runtime, _ = make_runtime(tmp_path, monkeypatch, 1)
    child = runtime._pick()
    prompt = (
        "Give one active relationship: Dim[X] is unique and Fact[X] repeats, "
        "single direction from Dim to Fact."
    )
    assert child._relationship_response_model(prompt) is RelationshipAnswer
    good = RelationshipAnswer(
        from_ref="Dim[X]",
        to_ref="Fact[X]",
        cardinality="1:*",
        is_active=True,
        filter_direction="from_to",
    )
    child._validate_response(prompt, good)
    with pytest.raises(ValueError, match="repeating"):
        child._validate_response(prompt, good.model_copy(update={"cardinality": "1:1"}))


def test_cancelled_adaptive_stream_closes_child_and_unlocks(monkeypatch, tmp_path):
    runtime, _ = make_runtime(tmp_path, monkeypatch, 8)
    runtime.busy_wait_seconds = 0  # assert the busy guard itself, without waiting
    child = runtime._pick()
    closed = []

    def stream(*args, **kwargs):
        try:
            yield "part one"
            yield "part two"
        finally:
            closed.append(True)

    monkeypatch.setattr(child, "stream_chat", stream)
    result = runtime.stream_chat("hello", system_prompt="system")
    assert next(result) == "part one"
    with pytest.raises(LocalModelError, match="busy"):
        runtime.generate("system", "hello", object)
    result.close()
    assert closed == [True]
    assert not runtime._lock.locked()


def test_divide_must_preserve_explicit_zero_fallback():
    prompt = (
        "Write a DAX measure named Ratio using DIVIDE(SUM(T[A]), SUM(T[B])), "
        "with alternate result 0."
    )
    assert not dax_problems(prompt, "Ratio = DIVIDE(SUM(T[A]), SUM(T[B]), 0)")
    assert any(
        "alternate result" in error
        for error in dax_problems(prompt, "Ratio = DIVIDE(SUM(T[A]), SUM(T[B]))")
    )


def test_large_repair_keeps_request_and_fits_context(monkeypatch, tmp_path):
    runtime, _ = make_runtime(tmp_path, monkeypatch, 8)
    model = runtime._pick()
    request = "Use supplied schema " + "界" * 750
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": request}]
    repaired = model._repair_messages(messages, "bad" * 2000, "parse error" * 200, 512)
    assert repaired[1]["content"] == request
    assert model._fits(repaired)
    assert "validation error" in repaired[-1]["content"]


def test_optional_history_and_reference_cannot_crowd_out_user(monkeypatch, tmp_path):
    runtime, _ = make_runtime(tmp_path, monkeypatch, 8)
    model = runtime._pick()
    messages = model._messages(
        "system",
        "schema " + "s" * 2500,
        "reference" * 250,
        [{"role": "assistant", "content": "old" * 400}],
    )
    assert len(messages) == 2
    assert messages[-1]["content"] == "schema " + "s" * 2500
    assert model._fits(messages)


@pytest.mark.parametrize(
    "detail",
    [
        "Adaptive memory guard: "
        + json.dumps({"available": GIB, "missing": GIB, "reason": "private path"}),
        "Profile resource guard: "
        + json.dumps({"available": GIB, "minimum_ram_gib": 1.5, "phase": "running"}),
        "AC power is required for local AI",
        "New paging exceeded 512 MiB cap: native_swapouts",
        "One-hour process cap reached",
    ],
)
def test_failure_messages_never_blame_memory_power_or_paging(detail):
    from ai_powerbi_analyst.studio.model_status import model_failure_message

    message = model_failure_message(detail)
    assert "Check model diagnostics in Settings" in message
    for claim in ("paused", "gib", "plug in", "paging", "memory", "private path"):
        assert claim not in message.lower()


def test_optional_examples_do_not_displace_recent_followup(monkeypatch, tmp_path):
    runtime, _ = make_runtime(tmp_path, monkeypatch, 8)
    model = runtime._pick()
    history = [
        {"role": "user", "content": "change this visual"},
        {"role": "assistant", "content": "Which chart type?"},
    ]
    messages = model._messages("system", "you decide", "example" * 700, history)
    assert messages[1:-1] == history
    assert messages[-1]["content"] == "you decide"
    assert model._fits(messages)


@pytest.mark.parametrize("key", ["balanced", "compact", "coder"])
def test_rejected_and_retired_profiles_are_not_runnable(key):
    with pytest.raises(ValueError, match="Unknown resource profile"):
        configured_profile({"resource_profile": key})


def test_second_request_waits_for_the_model_slot_instead_of_failing(monkeypatch, tmp_path):
    import threading
    import time

    runtime, _ = make_runtime(tmp_path, monkeypatch, 8)
    child = runtime._pick()
    monkeypatch.setattr(child, "generate", lambda *args, **kwargs: "narrative")
    runtime.busy_wait_seconds = 5
    runtime._lock.acquire()  # a draft narrative is generating
    threading.Timer(0.3, runtime._lock.release).start()
    started = time.monotonic()
    assert runtime.generate("system", "chat", object) == "narrative"
    assert 0.2 < time.monotonic() - started < 5
    assert not runtime._lock.locked()
