from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_powerbi_analyst.studio import native_report as native
from ai_powerbi_analyst.studio.contracts import DaxEvaluateRequest
from ai_powerbi_analyst.studio.report_import import ImportedPage, ImportedReport


@pytest.fixture
def report(tmp_path):
    return ImportedReport(
        id="report-test",
        workspace_id="workspace-test",
        name="Test",
        source_path=str(tmp_path / "source.pbix"),
        archived_path=str(tmp_path / "original.pbix"),
        source_hash="a" * 64,
        base_hash="a" * 64,
        format="PBIR",
        pages=[ImportedPage(id="page-one", title="Overview")],
    )


def test_instance_never_uses_another_open_report(tmp_path, report, monkeypatch):
    engine = native.NativeReportPreview(tmp_path, "bridge", "pbi")
    monkeypatch.setattr(
        native,
        "command",
        lambda *args: {
            "instances": [
                {
                    "currentFilePath": str(tmp_path / "other.pbix"),
                    "pid": 123,
                    "bridgeStatus": "connected",
                }
            ]
        },
    )
    with pytest.raises(ValueError, match="working copy"):
        engine.instance(report)


@pytest.mark.parametrize(
    "case,reason",
    [
        ("none", "no local calculation engine"),
        ("unrelated", "no local calculation engine"),
        ("multiple", "More than one calculation engine"),
        ("missing_directory_option", "calculation engine is unavailable"),
        ("missing_directory_value", "calculation engine is unavailable"),
    ],
)
def test_query_refuses_unavailable_or_ambiguous_local_engine(
    tmp_path, report, monkeypatch, case, reason
):
    engine = native.NativeReportPreview(tmp_path, "bridge", "pbi")
    arguments = ["msmdsrv.exe", "-s"] if case == "missing_directory_value" else ["msmdsrv.exe"]
    model = SimpleNamespace(name=lambda: "msmdsrv.exe", cmdline=lambda: arguments)
    children = {
        "none": [],
        "unrelated": [SimpleNamespace(name=lambda: "another.exe")],
        "multiple": [model, model],
        "missing_directory_option": [model],
        "missing_directory_value": [model],
    }[case]

    def process(pid):
        assert pid == 123
        return SimpleNamespace(children=lambda: children)

    calls = []

    def command(args, timeout):
        calls.append(args)
        assert args == ["bridge", "status"]  # No native connect, query or fallback.
        return {
            "instances": [
                {
                    "currentFilePath": str(native.working_copy(report)),
                    "pid": 123,
                    "bridgeStatus": "connected",
                }
            ]
        }

    monkeypatch.setattr(native.psutil, "Process", process)
    monkeypatch.setattr(native, "command", command)
    with pytest.raises(ValueError, match=reason) as caught:
        engine.query(report, 'EVALUATE ROW("Value", 1)')
    if case in {"none", "unrelated"}:
        assert "live connection to a remote model" in str(caught.value)
        assert "Desktop page previews" in str(caught.value)
    assert calls == [["bridge", "status"]]
    assert not list(tmp_path.glob("native-query-*"))


def test_connection_uses_only_the_working_copy_childs_local_port(tmp_path, report, monkeypatch):
    engine = native.NativeReportPreview(tmp_path, "bridge", "pbi")
    directory = tmp_path / "local-model"
    directory.mkdir()
    (directory / "msmdsrv.port.txt").write_text("\ufeff51234\x00\r\n", encoding="utf-16-le")
    model = SimpleNamespace(
        name=lambda: "MSMDSRV.EXE", cmdline=lambda: ["msmdsrv.exe", "-s", str(directory)]
    )

    def process(pid):
        assert pid == 123
        return SimpleNamespace(
            children=lambda: [SimpleNamespace(name=lambda: "another.exe"), model]
        )

    calls = []
    name = "studio-report-test-123"

    def command(args, timeout=30):
        calls.append(args)
        if args == ["bridge", "status"]:
            return {
                "instances": [
                    {
                        "currentFilePath": str(tmp_path / "another.pbix"),
                        "pid": 999,
                        "bridgeStatus": "connected",
                    },
                    {
                        "currentFilePath": str(native.working_copy(report)),
                        "pid": 123,
                        "bridgeStatus": "connected",
                    },
                ]
            }
        assert args == [
            "pbi",
            "--json",
            "connect",
            "--data-source",
            "localhost:51234",
            "--name",
            name,
        ]
        return {"status": "connected", "connection": name}

    monkeypatch.setattr(native.psutil, "Process", process)
    monkeypatch.setattr(native, "command", command)
    assert engine.connection(report) == name
    assert len(calls) == 2


def test_page_capture_does_not_require_a_local_calculation_engine(tmp_path, report, monkeypatch):
    engine = native.NativeReportPreview(tmp_path, "bridge", "pbi")
    calls = []

    def no_process_query(*args):
        pytest.fail("A Desktop page screenshot must not inspect local calculation engines")

    def command(args, timeout):
        calls.append(args)
        if args == ["bridge", "status"]:
            return {
                "instances": [
                    {
                        "currentFilePath": str(native.working_copy(report)),
                        "pid": 123,
                        "bridgeStatus": "connected",
                    }
                ]
            }
        assert args[:5] == ["bridge", "screenshot", "page-one", "--pid", "123"]
        Path(args[args.index("--output") + 1]).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        return {"status": "ok", "pid": 123, "pageId": "page-one"}

    monkeypatch.setattr(native.psutil, "Process", no_process_query)
    monkeypatch.setattr(native, "command", command)
    assert engine.page(report, "page-one")["cached"] is False
    assert [args[1] for args in calls] == ["status", "screenshot", "status"]


def test_native_page_cache_and_failed_refresh_keep_good_image(tmp_path, report, monkeypatch):
    engine = native.NativeReportPreview(tmp_path, "bridge", "pbi")
    monkeypatch.setattr(engine, "instance", lambda _: 123)
    png = b"\x89PNG\r\n\x1a\nfixture"
    calls = []

    def capture(args, timeout):
        calls.append(args)
        Path(args[args.index("--output") + 1]).write_bytes(png)
        return {"status": "ok", "pid": 123, "pageId": "page-one"}

    monkeypatch.setattr(native, "command", capture)
    first = engine.page(report, "page-one")
    assert first["image"].startswith("data:image/png;base64,")
    assert not first["cached"]
    assert engine.page(report, "page-one")["cached"] and len(calls) == 1
    monkeypatch.setattr(
        native, "command", lambda *args: {"status": "ok", "pid": 999, "pageId": "page-one"}
    )
    with pytest.raises(ValueError, match="different report"):
        engine.page(report, "page-one", True)
    assert engine.page(report, "page-one")["image"] == first["image"]
    # Previews live under a folder named for the head's hash; no capture file stays behind.
    assert not list((tmp_path / "previews").rglob("capture-*"))
    with pytest.raises(ValueError, match="does not belong"):
        engine.page(report, "../escape")


def test_query_numeric_types_limits_and_cleanup(tmp_path, report, monkeypatch):
    engine = native.NativeReportPreview(tmp_path, "bridge", "pbi")
    monkeypatch.setattr(engine, "connection", lambda _: "specific-report")
    monkeypatch.setattr(engine, "instance", lambda _: 123)

    def execute(args, timeout):
        assert args[args.index("--connection") + 1] == "specific-report"
        assert args[args.index("--max-rows") + 1] == "2"
        query = Path(args[args.index("--file") + 1]).read_text(encoding="utf-8")
        assert "[MTD Sales]" in query and timeout <= 30
        return {
            "columns": ["[Value]", "[__StudioNumeric]", "[__StudioBoolean]"],
            "rows": [
                {"[Value]": "12.5", "[__StudioNumeric]": "True"},
                {"[Value]": None, "[__StudioNumeric]": "False"},
            ],
        }

    monkeypatch.setattr(native, "command", execute)
    result = engine.evaluate(
        report,
        None,
        DaxEvaluateRequest(
            workspace_id="workspace-test", expression="[MTD Sales]", engine="powerbi", top_n=1
        ),
    )
    assert result.rows == [{"Value": 12.5}] and result.truncated and result.engine == "powerbi"
    assert not list(tmp_path.glob("native-query-*"))

    def fail(*args):
        raise ValueError("Timed out")

    monkeypatch.setattr(native, "command", fail)
    with pytest.raises(ValueError, match="Timed out"):
        engine.evaluate(
            report, None, DaxEvaluateRequest(workspace_id="workspace-test", expression="1")
        )
    assert not list(tmp_path.glob("native-query-*"))
    with engine.lock(), pytest.raises(ValueError, match="Another Power BI preview"):
        engine.evaluate(
            report, None, DaxEvaluateRequest(workspace_id="workspace-test", expression="1")
        )


def test_commands_do_not_use_a_shell_and_bound_timeout(monkeypatch):
    def run(args, **options):
        assert not options["shell"] and options["timeout"] == 15
        assert options["env"]["PYTHONIOENCODING"] == "utf-8"
        return SimpleNamespace(returncode=0, stdout='{"status":"ok"}')

    monkeypatch.setattr(native.subprocess, "run", run)
    assert native.command(["pbi.exe", "--json"], 15)["status"] == "ok"


@pytest.mark.parametrize(
    "value,numeric,boolean,expected",
    [
        ("001", False, False, "001"),
        ("True", False, True, True),
        (None, False, False, None),
        ("-2.75", True, False, -2.75),
    ],
)
def test_native_scalar_preserves_types(
    tmp_path, report, monkeypatch, value, numeric, boolean, expected
):
    engine = native.NativeReportPreview(tmp_path, "bridge", "pbi")
    monkeypatch.setattr(engine, "connection", lambda _: "specific-report")
    monkeypatch.setattr(engine, "instance", lambda _: 123)
    monkeypatch.setattr(
        native,
        "command",
        lambda *args: {
            "columns": ["[Value]", "[__StudioNumeric]", "[__StudioBoolean]"],
            "rows": [
                {"[Value]": value, "[__StudioNumeric]": numeric, "[__StudioBoolean]": boolean}
            ],
        },
    )
    result = engine.evaluate(
        report, None, DaxEvaluateRequest(workspace_id="workspace-test", expression="1")
    )
    assert result.rows == [{"Value": expected}]
    assert type(result.rows[0]["Value"]) is type(expected)


def test_corrupt_image_cache_is_recaptured(tmp_path, report, monkeypatch):
    engine = native.NativeReportPreview(tmp_path, "bridge", "pbi")
    monkeypatch.setattr(engine, "instance", lambda _: 123)
    folder = tmp_path / "previews"
    folder.mkdir()
    (folder / "page-0.png").write_bytes(b"corrupt")

    def capture(args, timeout):
        Path(args[args.index("--output") + 1]).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        return {"status": "ok", "pid": 123, "pageId": "page-one"}

    monkeypatch.setattr(native, "command", capture)
    assert not engine.page(report, "page-one")["cached"]
