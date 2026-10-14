import pytest

from ai_powerbi_analyst.llm.managed_ollama_backend import OllamaServer


@pytest.mark.parametrize("cap", [0, 512])
def test_owned_process_receives_bounded_cache_without_changing_guards(tmp_path, monkeypatch, cap):
    calls = []

    def capture(args, **kwargs):
        calls.append(kwargs)
        raise OSError("test does not start a model")

    monkeypatch.setenv("LLAMA_ARG_CACHE_RAM", "8192")
    monkeypatch.setattr("subprocess.Popen", capture)
    server = OllamaServer(
        dict(context_tokens=4096, threads=4, hashes={}, server="unused", prompt_cache_mib=cap),
        lambda: {},
        tmp_path,
    )
    with pytest.raises(OSError, match="test does not start"):
        server.start()
    assert calls[0]["env"]["LLAMA_ARG_CACHE_RAM"] == str(cap)
    assert calls[0]["env"]["OLLAMA_CONTEXT_LENGTH"] == "4096"
    assert calls[0]["env"]["OLLAMA_NUM_PARALLEL"] == "1"


@pytest.mark.parametrize("cap", [-1, 513, True, "512"])
def test_invalid_cache_budget_fails_before_process_start(tmp_path, monkeypatch, cap):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("must not launch"))
    server = OllamaServer(
        dict(context_tokens=4096, threads=4, hashes={}, prompt_cache_mib=cap), lambda: {}, tmp_path
    )
    with pytest.raises(ValueError, match="Prompt cache"):
        server.start()
