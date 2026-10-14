import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from ai_powerbi_analyst.llm.runtime import LocalModelError
from ai_powerbi_analyst.llm.warm_ollama import WarmOllamaModelRuntime
from ai_powerbi_analyst.settings import ModelSettings


@pytest.fixture
def warm(tmp_path, monkeypatch):
    from powerbi_rag_runtime import guards

    events = []

    class Guard:
        def __init__(self, root):
            self.calls = 0
            events.append(("guard", self))

        def check(self, startup=False):
            self.calls += 1
            events.append(("check", startup))
            return {"startup": startup}

    @contextmanager
    def locked(*args):
        events.append(("lock", threading.get_ident()))
        try:
            yield
        finally:
            events.append(("unlock", threading.get_ident()))

    monkeypatch.setattr(guards, "Guard", Guard)
    monkeypatch.setattr(guards, "exclusive_lock", locked)
    monkeypatch.setattr(guards, "keep_awake", locked)
    model = WarmOllamaModelRuntime(ModelSettings(), tmp_path / "evidence")
    cfg = {"tag": "qwen"}
    monkeypatch.setattr(model, "_configuration", lambda: (tmp_path / "runtime/config.json", cfg))

    class Server:
        def __init__(self, config, check, target):
            self.check = check
            self.stopped = threading.Event()
            self.process = SimpleNamespace(pid=123)
            self.failure = None
            events.append(("server", self))

        def start(self):
            self.check()
            return self

        def raise_failure(self):
            if self.failure:
                raise self.failure

        def stop(self):
            self.stopped.set()
            events.append(("stop", threading.get_ident()))

        def complete(self, *args, **kwargs):
            return {"output": "ok"}

    monkeypatch.setattr(model, "_server", Server)
    yield model, events, cfg
    model.unload()


def test_reuse_keeps_one_guard_lock_and_distinct_evidence(warm):
    model, events, _ = warm
    with model._session() as (one, first, _):
        assert one.complete()["output"] == "ok"
        with pytest.raises(LocalModelError, match="busy"), model._session():
            pass
    with model._session() as (two, second, _):
        assert two.server is one.server
    assert first != second
    assert sum(e[0] == "guard" for e in events) == 1
    assert [e for e in events if e == ("check", True)] == [("check", True)]
    assert not any(e[0] == "unlock" for e in events)
    model.unload()
    assert one.server.stopped.is_set()
    assert [e[1] for e in events if e[0] == "lock"] == [e[1] for e in events if e[0] == "unlock"]


def test_idle_closes_process_before_global_lock(warm):
    model, events, _ = warm
    model.idle_seconds = 0.05
    with model._session() as (lease, _, _):
        pass
    model._owner.thread.join(timeout=2)
    assert lease.server.stopped.is_set()
    assert next(i for i, e in enumerate(events) if e[0] == "stop") < next(
        i for i, e in enumerate(events) if e[0] == "unlock"
    )


def test_resource_pause_stops_owned_warm_session_before_external_work(warm):
    model, _events, _cfg = warm
    with model._session() as (lease, _target, _root):
        pass
    owner = model._owner
    assert owner is not None and owner.thread.is_alive()
    with model.pause_inference():
        assert lease.server.stopped.is_set() and not owner.thread.is_alive()
        assert model._owner is None and model._mutex.locked()
        with pytest.raises(LocalModelError, match="busy"), model._session():
            pytest.fail("Generation must not start during external work")
    assert not model._mutex.locked()
    with model._session() as (next_lease, _target, _root):
        assert next_lease.server is not lease.server


def test_guard_failure_is_not_reused(warm):
    model, events, _ = warm
    with model._session() as (lease, _, _):
        pass
    lease.server.failure = RuntimeError("New paging exceeded 512 MiB cap")
    model._owner.thread.join(timeout=2)
    assert lease.server.stopped.is_set()
    with model._session() as (next_lease, _, _):
        assert next_lease.server is not lease.server
    assert sum(e[0] == "guard" for e in events) == 2


def test_normal_chat_cleanup_preserves_warm_session_but_cancel_stops(warm):
    model, _, _ = warm
    with model._session() as (lease, _, _):
        pass
    lease.stop()
    assert not model._owner.stop.is_set()
    with model._session() as (active, _, _):
        active.stop()
    model._owner.thread.join(timeout=2)
    assert active.server.stopped.is_set()


def test_changed_configuration_requires_new_session(warm):
    model, _, cfg = warm
    with model._session() as (old, _, _):
        pass
    cfg["tag"] = "changed"
    with model._session() as (new, _, _):
        assert old.server.stopped.is_set()
        assert new.server is not old.server


def test_startup_failure_releases_lock_and_returns_error(warm, monkeypatch):
    model, events, _ = warm

    def broken(*args):
        raise RuntimeError("hash mismatch")

    monkeypatch.setattr(model, "_server", broken)
    with pytest.raises(LocalModelError, match="hash mismatch"), model._session():
        pass
    model.unload()
    assert sum(e[0] == "unlock" for e in events) == 2
    assert model._active is None


def test_completed_chat_reuses_owner_between_turns(warm, monkeypatch):
    model, events, _ = warm
    monkeypatch.setattr(model, "_references", lambda *args: ("", []))

    def complete(server, messages, **kwargs):
        kwargs["on_text"]("Ready.")
        return dict(
            output="Ready.",
            complete=True,
            finish_reason="stop",
            prompt_tokens=5,
            tokens=1,
            seconds=0.01,
            native_timings={},
        )

    monkeypatch.setattr(model._server, "complete", complete)
    assert list(model.stream_chat("Hello", system_prompt="Be concise")) == ["Ready."]
    assert list(model.stream_chat("Again", system_prompt="Be concise")) == ["Ready."]
    assert sum(e[0] == "server" for e in events) == 1
    assert not model._owner.stop.is_set()


def test_cancelled_chat_stops_worker_and_releases_mutex(warm, monkeypatch):
    model, _, _ = warm
    monkeypatch.setattr(model, "_references", lambda *args: ("", []))

    def complete(server, messages, **kwargs):
        kwargs["on_text"]("First")
        if not server.stopped.wait(timeout=3):
            raise AssertionError("Cancellation did not terminate the server")
        raise LocalModelError("Cancelled")

    monkeypatch.setattr(model._server, "complete", complete)
    stream = model.stream_chat("Hello", system_prompt="Be concise")
    assert next(stream) == "First"
    stream.close()
    model._owner.thread.join(timeout=2)
    assert not model._owner.thread.is_alive()
    assert not model._mutex.locked()


def test_current_request_follows_same_examples_and_preserves_history(warm):
    model, _, _ = warm
    history = [{"role": "assistant", "content": "Prior reply"}]
    refs = "\n\nWorked examples with DIFFERENT data.\nReference example"
    actual = model._messages("System instructions", "Current DAX task", refs, history)
    assert actual == [
        {"role": "system", "content": "System instructions"},
        *history,
        {
            "role": "user",
            "content": refs.lstrip("\n")
            + "\n\nCURRENT REQUEST - answer this only:\nCurrent DAX task",
        },
    ]
    assert model._messages("System", "Current task", "")[-1]["content"] == "Current task"


@pytest.mark.parametrize("prompt", ["Write Power Query M using #table", "Write SQL", "Hello"])
def test_m_sql_and_unclassified_requests_keep_original_order(warm, prompt):
    model, _, _ = warm
    refs = "\n\nWorked examples with DIFFERENT data. Example"
    assert model._messages("System", prompt, refs)[-1]["content"] == prompt + refs
