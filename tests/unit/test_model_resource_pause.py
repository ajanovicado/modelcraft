"""Owned model memory can be released without truncating active generation."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from test_adaptive_runtime import make_runtime

from ai_powerbi_analyst.llm.managed_ollama import ManagedOllamaModelRuntime
from ai_powerbi_analyst.llm.runtime import LocalModelError, PausableModelRuntime
from ai_powerbi_analyst.settings import ModelSettings


@pytest.fixture(params=["adaptive", "guarded"])
def owned_runtime(request, tmp_path, monkeypatch):
    if request.param == "adaptive":
        model, _ = make_runtime(tmp_path, monkeypatch, 8)
        model.busy_wait_seconds = 1
        lock = model._lock
    else:
        model = ManagedOllamaModelRuntime(ModelSettings(request_timeout_seconds=1))
        lock = model._mutex
    unloaded = []
    monkeypatch.setattr(model, "unload", lambda: unloaded.append(True))
    return model, lock, unloaded


def test_resource_pause_waits_for_active_slot_without_unloading(owned_runtime):
    model, lock, unloaded = owned_runtime
    assert isinstance(model, PausableModelRuntime)
    started, entered = threading.Event(), threading.Event()
    errors = []
    lock.acquire()

    def external_work():
        started.set()
        try:
            with model.pause_inference():
                entered.set()
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=external_work)
    worker.start()
    try:
        assert started.wait(1)
        assert not entered.wait(0.05)
        assert unloaded == []
    finally:
        lock.release()
        worker.join(2)
    assert not worker.is_alive() and not errors
    assert entered.is_set() and unloaded == [True] and not lock.locked()


@pytest.mark.parametrize("failure", ["unload", "external_work"])
def test_resource_pause_releases_slot_on_failure(owned_runtime, monkeypatch, failure):
    model, lock, unloaded = owned_runtime

    def fail_unload():
        raise LocalModelError("Owned worker failed to stop")

    if failure == "unload":
        monkeypatch.setattr(model, "unload", fail_unload)
    with pytest.raises((LocalModelError, ValueError)), model.pause_inference():
        assert unloaded == [True] and lock.locked()
        raise ValueError("External validation failed")
    assert not lock.locked()


def test_resource_pause_timeout_does_not_unload_active_response(owned_runtime):
    model, lock, unloaded = owned_runtime
    if hasattr(model, "busy_wait_seconds"):
        model.busy_wait_seconds = 0
    lock.acquire()
    try:
        with pytest.raises(LocalModelError, match="busy"), model.pause_inference():
            pytest.fail("A busy model cannot enter external work")
        assert unloaded == [] and lock.locked()
    finally:
        lock.release()


def test_adaptive_pause_lets_active_response_finish_and_defers_next_generation(
    tmp_path, monkeypatch
):
    model, _ = make_runtime(tmp_path, monkeypatch, 8)
    model.busy_wait_seconds = 2
    child = model._pick()
    first_started, first_finish = threading.Event(), threading.Event()
    pause_started, pause_entered, pause_finish = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    next_started = threading.Event()
    events, replies, errors = [], [], []

    def generate(_system, message, *_args, **_kwargs):
        events.append(message + "-start")
        if message == "first":
            first_started.set()
            assert first_finish.wait(2)
        else:
            next_started.set()
        events.append(message + "-finish")
        return message + "-reply"

    monkeypatch.setattr(child, "generate", generate)
    monkeypatch.setattr(child, "unload", lambda: events.append("unload"))

    def chat(message):
        try:
            replies.append(model.generate("system", message, object))
        except Exception as error:
            errors.append(error)

    def pause():
        pause_started.set()
        try:
            with model.pause_inference():
                events.append("build-start")
                pause_entered.set()
                assert pause_finish.wait(2)
                events.append("build-finish")
        except Exception as error:
            errors.append(error)

    first = threading.Thread(target=chat, args=("first",))
    external = threading.Thread(target=pause)
    following = threading.Thread(target=chat, args=("next",))
    first.start()
    try:
        assert first_started.wait(1)
        external.start()
        assert pause_started.wait(1) and not pause_entered.wait(0.05)
        assert "unload" not in events
        first_finish.set()
        assert pause_entered.wait(1)
        following.start()
        assert not next_started.wait(0.05)
        pause_finish.set()
    finally:
        first_finish.set()
        pause_finish.set()
        for worker in (first, external, following):
            if worker.ident is not None:
                worker.join(3)
    assert not errors and not any(worker.is_alive() for worker in (first, external, following))
    assert set(replies) == {"first-reply", "next-reply"}
    assert events == [
        "first-start",
        "first-finish",
        "unload",
        "build-start",
        "build-finish",
        "next-start",
        "next-finish",
    ]
    assert not model._lock.locked()


@pytest.mark.parametrize("kind", ["adaptive", "guarded"])
def test_cancellation_unload_does_not_wait_for_the_generation_slot(tmp_path, monkeypatch, kind):
    stopped = threading.Event()
    if kind == "adaptive":
        model, _ = make_runtime(tmp_path, monkeypatch, 8)
        model._current = SimpleNamespace(unload=stopped.set)
        lock = model._lock
    else:
        model = ManagedOllamaModelRuntime(ModelSettings())
        model._active = SimpleNamespace(stop=stopped.set)
        lock = model._mutex
    lock.acquire()
    worker = threading.Thread(target=model.unload)
    worker.start()
    try:
        assert stopped.wait(1)
        worker.join(1)
        assert not worker.is_alive() and lock.locked()
    finally:
        lock.release()
        worker.join(1)
