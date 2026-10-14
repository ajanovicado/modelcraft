from types import SimpleNamespace

from fastapi.testclient import TestClient

from ai_powerbi_analyst.studio.api import create_app


def test_sidecar_shutdown_unloads_owned_model():
    calls = []
    service = SimpleNamespace(model=SimpleNamespace(unload=lambda: calls.append("unload")))
    with TestClient(create_app(service, "0123456789abcdef")):
        assert calls == []
    assert calls == ["unload"]
