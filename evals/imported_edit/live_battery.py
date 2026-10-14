r"""Live end-to-end check of imported-report editing against a running sidecar.

    .venv\Scripts\python.exe evals\imported_edit\live_battery.py --endpoint http://127.0.0.1:8767 ^
        --token <token> --pbix "C:\\path\\Agents Performance - Dashboard.pbix" [--desktop]

It drives the HTTP API the way the app does (import, edit, render, undo, restore, save a copy) and,
with ``--desktop``, the Power BI Desktop steps (open the edited copy, a measure change, a rename with
dependents, the delete guard). Every step is asserted; nothing is left half done: report edits are
undone, and the Desktop steps restore the report to its imported state at the end. Run it against an
isolated state folder (``studio-sidecar-edit-qa`` in .claude/launch.json), not the dev workspace.

The stdlib only, so it runs from any Python; from Git Bash on this machine 127.0.0.1 does not resolve
(getaddrinfo 11001), so start it from PowerShell.
"""

# ruff: noqa: E501 - step details are long human-readable messages
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
CHART = "b404a461ede7604a2774"  # column chart with TopN / BottomN / Others fills
TABLE = "e82e02f93055d6b0b544"  # table with conditional font colors and a measure filter
CARD = "97d1504080b699eae7d7"


class Api:
    def __init__(self, endpoint: str, token: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(
        self, method: str, path: str, body: Any = None, timeout: float = 300
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        # The endpoint is the local sidecar the operator passed in (http only).
        request = urllib.request.Request(  # noqa: S310
            self.endpoint + path, data=data, method=method, headers=self.headers
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            raw, status = error.read(), error.code
        try:
            return status, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return status, raw.decode("utf-8", "replace")[:500]

    def settle(self, ws: str, status: int, body: Any, timeout: float = 400) -> tuple[int, Any]:
        """Follow a 202 to the batch's end; the HTTP edit routes hand long batches to a worker."""
        if status != 202 or not isinstance(body, dict) or "batch_id" not in body:
            return status, body
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(1.5)
            code, state = self.call(
                "GET", f"/v1/workspaces/{ws}/imported-report/edits/{body['batch_id']}"
            )
            if code == 200 and state.get("state") == "done":
                return 200, state.get("receipt") or state
            if code == 200 and state.get("state") == "failed":
                error = state.get("error") or {}
                return int(error.get("status", 422)), error
        return 504, {"code": "battery_timeout", "message": "the batch did not finish in time"}

    def edit(self, ws: str, head: str, ops: list[dict[str, Any]], **flags: Any) -> tuple[int, Any]:
        body = {
            "operations": ops,
            "expected_head": head,
            "idempotency_key": "qa-" + uuid.uuid4().hex,
            **flags,
        }
        return self.settle(
            ws, *self.call("POST", f"/v1/workspaces/{ws}/imported-report/edits", body)
        )

    def undo(self, ws: str, head: str, **flags: Any) -> tuple[int, Any]:
        body = {"expected_head": head, "idempotency_key": "qa-" + uuid.uuid4().hex, **flags}
        return self.settle(
            ws, *self.call("POST", f"/v1/workspaces/{ws}/imported-report/undo", body)
        )

    def report(self, ws: str) -> dict[str, Any]:
        status, body = self.call("GET", f"/v1/workspaces/{ws}/imported-report")
        assert status == 200, (status, body)
        return dict(body)

    def render(self, ws: str, page: str, head: str) -> dict[str, Any]:
        status, body = self.call(
            "POST",
            f"/v1/workspaces/{ws}/imported-report/pages/{page}/render?revision={head}",
            {"selections": [], "searches": {}},
            timeout=600,
        )
        assert status == 200, (status, body)
        return dict(body)


class Battery:
    def __init__(self, api: Api) -> None:
        self.api = api
        self.results: list[dict[str, Any]] = []
        self.ws = ""
        self.page = ""

    def step(self, name: str, action: Any) -> Any:
        started = time.monotonic()
        try:
            detail = action()
            outcome = {"name": name, "ok": True, "detail": detail}
        except AssertionError as error:
            outcome = {"name": name, "ok": False, "detail": f"assertion: {error}"}
        except Exception as error:  # noqa: BLE001 - one failed step must not stop the rest
            outcome = {"name": name, "ok": False, "detail": f"{type(error).__name__}: {error}"}
        outcome["seconds"] = round(time.monotonic() - started, 1)
        self.results.append(outcome)
        mark = "PASS" if outcome["ok"] else "FAIL"
        print(
            f"[{mark}] {name} ({outcome['seconds']} s) {str(outcome['detail'])[:260]}", flush=True
        )
        return outcome

    # --- helpers ---------------------------------------------------------------------------

    def head(self) -> str:
        return str(self.api.report(self.ws)["head"]["id"])

    def visual(self, page_payload: dict[str, Any], visual_id: str) -> dict[str, Any]:
        for row in page_payload["visuals"]:
            if row["id"] == visual_id:
                return dict(row)
        raise AssertionError(f"visual {visual_id} is not on the page")

    # --- scenarios -------------------------------------------------------------------------

    def run(self, pbix: str | None, workspace: str | None, desktop: bool) -> None:
        def start() -> str:
            if workspace:
                self.ws = workspace
            else:
                assert pbix, "pass --pbix or --workspace"
                status, body = self.api.call(
                    "POST",
                    "/v1/reports/import",
                    {"path": pbix, "idempotency_key": "qa-import-" + uuid.uuid4().hex},
                    timeout=900,
                )
                assert status == 200, (status, body)
                self.ws = body["id"]
            report = self.api.report(self.ws)
            self.page = report["pages"][0]["id"]
            assert report["head"]["id"] == "r0000" and report["head"]["edited"] is False, report[
                "head"
            ]
            assert report["capabilities"]["edit_report"] is True, report["capabilities"]
            return (
                f"{self.ws}: {len(report['pages'])} page(s), head r0000, {report['capabilities']}"
            )

        self.step("import and report payload", start)
        if not self.ws:
            return
        baseline: dict[str, Any] = {}

        def render_baseline() -> str:
            baseline.update(self.api.render(self.ws, self.page, "r0000"))
            row = self.visual(baseline, CHART)
            assert row["fidelity"] in {"exact", "approximate"}, row["fidelity"]
            assert baseline["revision"] == "r0000"
            return f"{len(baseline['visuals'])} visuals; chart series {json.dumps(row['formatting'].get('series'))[:120]}"

        self.step("baseline render carries the head revision", render_baseline)

        def move_and_undo() -> str:
            status, receipt = self.api.edit(
                self.ws,
                "r0000",
                [{"op": "set_position", "visual": CHART, "x": 200}],
            )
            assert status == 200, (status, receipt)
            head = receipt["head"]["id"]
            assert head == "r0001", head
            after = self.visual(self.api.render(self.ws, self.page, head), CHART)
            assert abs(after["position"]["x"] - 200) < 1, after["position"]
            assert after["formatting"] == self.visual(baseline, CHART)["formatting"], (
                "moving the chart changed its existing formatting"
            )
            status, receipt2 = self.api.undo(self.ws, head)
            assert status == 200, (status, receipt2)
            back = self.visual(self.api.render(self.ws, self.page, receipt2["head"]["id"]), CHART)
            assert back["position"] == self.visual(baseline, CHART)["position"], (
                "undo did not restore the chart's position"
            )
            return f"replaced {receipt.get('replaced')}; visible_in {receipt.get('visible_in')}; undone to {receipt2['head']['id']}"

        self.step("move a chart, preserve its formatting, undo it", move_and_undo)

        def move_add_delete() -> str:
            head = self.head()
            status, receipt = self.api.edit(
                self.ws, head, [{"op": "set_position", "visual": CARD, "y": 900, "height": 210}]
            )
            assert status == 200, (status, receipt)
            moved = self.visual(self.api.render(self.ws, self.page, receipt["head"]["id"]), CARD)
            assert abs(moved["position"]["y"] - 900) < 1, moved["position"]
            status, receipt = self.api.edit(
                self.ws,
                receipt["head"]["id"],
                [
                    {
                        "op": "add_visual",
                        "page": self.page,
                        "visual_type": "clusteredColumnChart",
                        "fields": [
                            {"table": "DimChannel", "name": "ChannelName"},
                            {"table": "# Measures", "name": "Total Sales"},
                        ],
                    }
                ],
            )
            assert status == 200, (status, receipt)
            page = self.api.render(self.ws, self.page, receipt["head"]["id"])
            known = {v["id"] for v in baseline["visuals"]}
            new = [v for v in page["visuals"] if v["id"] not in known]
            assert len(new) == 1, [v["id"] for v in new]
            box = new[0]["position"]
            clashes = [
                v["id"]
                for v in page["visuals"]
                if v["id"] != new[0]["id"]
                and v.get("kind") not in {"group", "shape"}
                and v["position"]["x"] < box["x"] + box["width"]
                and box["x"] < v["position"]["x"] + v["position"]["width"]
                and v["position"]["y"] < box["y"] + box["height"]
                and box["y"] < v["position"]["y"] + v["position"]["height"]
            ]
            assert clashes == [], f"the new chart lies on {clashes}"
            status, deleted = self.api.edit(
                self.ws,
                receipt["head"]["id"],
                [{"op": "delete_visual", "visual": new[0]["id"]}],
            )
            assert status == 200, (status, deleted)
            return f"moved a card to y=900, added a chart at {box}, deleted it again"

        self.step("move a card, add a chart in free space, delete it", move_add_delete)

        def filter_page() -> str:
            head = self.head()
            before = self.api.render(self.ws, self.page, head)
            status, receipt = self.api.edit(
                self.ws,
                head,
                [
                    {
                        "op": "set_page_filter",
                        "page": self.page,
                        "filter": {
                            "kind": "categorical",
                            "field": {"table": "DimChannel", "name": "ChannelName"},
                            "values": ["Store"],
                        },
                    }
                ],
            )
            assert status == 200, (status, receipt)
            after = self.api.render(self.ws, self.page, receipt["head"]["id"])
            changed = [
                v["id"]
                for v in after["visuals"]
                if v.get("rows")
                != next(b for b in before["visuals"] if b["id"] == v["id"]).get("rows")
            ]
            assert changed, "a page filter on the channel changed no visual's numbers"
            status, cleared = self.api.edit(
                self.ws,
                receipt["head"]["id"],
                [{"op": "clear_filter", "scope": "page", "target": self.page}],
            )
            assert status == 200, (status, cleared)
            final = self.api.render(self.ws, self.page, cleared["head"]["id"])
            assert all(
                v.get("rows")
                == next(b for b in before["visuals"] if b["id"] == v["id"]).get("rows")
                for v in final["visuals"]
            ), "clearing the filter did not bring the numbers back"
            return f"{len(changed)} visuals changed under Channel = Store, all back after clear_filter"

        self.step("a page filter changes the numbers and clears again", filter_page)

        def history_restore() -> str:
            status, rows = self.api.call("GET", f"/v1/workspaces/{self.ws}/imported-report/history")
            assert status == 200 and len(rows) >= 5, (status, len(rows) if rows else rows)
            head = rows[0]["id"]
            status, preview = self.api.call(
                "GET", f"/v1/workspaces/{self.ws}/imported-report/history/r0000/discard-preview"
            )
            assert status == 200 and preview["would_discard"], preview
            status, body = self.api.settle(
                self.ws,
                *self.api.call(
                    "POST",
                    f"/v1/workspaces/{self.ws}/imported-report/restore",
                    {
                        "revision_id": "r0000",
                        "expected_head": head,
                        "idempotency_key": "qa-" + uuid.uuid4().hex,
                    },
                ),
            )
            assert status == 409 and body.get("code") == "needs_confirmation", (status, body)
            status, receipt = self.api.settle(
                self.ws,
                *self.api.call(
                    "POST",
                    f"/v1/workspaces/{self.ws}/imported-report/restore",
                    {
                        "revision_id": "r0000",
                        "expected_head": head,
                        "idempotency_key": "qa-" + uuid.uuid4().hex,
                        "confirmed_discard": True,
                    },
                ),
            )
            assert status == 200, (status, receipt)
            report = self.api.report(self.ws)
            assert report["head"]["edited"] is False, report["head"]
            again = self.api.render(self.ws, self.page, report["head"]["id"])
            assert self.visual(again, CHART)["formatting"].get("series") == self.visual(
                baseline, CHART
            )["formatting"].get("series")
            return f"{len(rows)} history rows; restore to r0000 asked, then made head {report['head']['id']} identical to the import"

        self.step("history, discard preview and restore to the original", history_restore)

        def save_copy() -> str:
            folder = Path(tempfile.mkdtemp(prefix="studio-qa-copy-"))
            status, first = self.api.settle(
                self.ws,
                *self.api.call(
                    "POST",
                    f"/v1/workspaces/{self.ws}/imported-report/save-copy",
                    {
                        "destination": str(folder),
                        "idempotency_key": "qa-" + uuid.uuid4().hex,
                    },
                ),
            )
            assert status == 200, (status, first)
            status, second = self.api.settle(
                self.ws,
                *self.api.call(
                    "POST",
                    f"/v1/workspaces/{self.ws}/imported-report/save-copy",
                    {
                        "destination": str(folder),
                        "idempotency_key": "qa-" + uuid.uuid4().hex,
                    },
                ),
            )
            assert status == 200 and second["path"] != first["path"], (
                "a second copy overwrote the first"
            )
            sys.path.insert(0, str(REPO / "src"))
            from ai_powerbi_analyst.studio.pbix_package import PbixPackage, data_model_digest

            package = PbixPackage(Path(first["path"]))
            assert package.has("Report/definition/report.json")
            return f"copies {Path(first['path']).name} and {Path(second['path']).name}; digest {str(data_model_digest(Path(first['path'])))[:12]}"

        self.step("save a copy twice: never overwritten, opens as a package", save_copy)

        if not desktop:
            return
        self.desktop_steps()

    # --- Power BI Desktop steps ------------------------------------------------------------

    def desktop_steps(self) -> None:
        def capabilities() -> str:
            # The first look for Desktop runs in the background; ask until it has answered.
            deadline = time.monotonic() + 90
            caps = self.api.report(self.ws)["capabilities"]
            while caps.get("checking") and time.monotonic() < deadline:
                time.sleep(3)
                caps = self.api.report(self.ws)["capabilities"]
            assert caps["edit_model"] is True, f"model edits are off: {caps}"
            return json.dumps(caps)[:200]

        outcome = self.step("model edits are ready (Desktop, bridge, pbi, receipt)", capabilities)
        if not outcome["ok"]:
            return

        def open_desktop() -> str:
            status, body = self.api.settle(
                self.ws,
                *self.api.call(
                    "POST", f"/v1/workspaces/{self.ws}/open-source-report", {}, timeout=300
                ),
            )
            assert status == 200, (status, body)
            deadline = time.monotonic() + 120
            state = ""
            while time.monotonic() < deadline:
                state = self.api.report(self.ws)["desktop"]["state"]
                if state in {"open", "open_unsaved"}:
                    break
                time.sleep(3)
            assert state in {"open", "open_unsaved"}, state
            return f"Desktop is {state}"

        self.step("open the edited copy in Power BI Desktop", open_desktop)

        def measure_cycle() -> str:
            head = self.head()
            status, receipt = self.api.edit(
                self.ws,
                head,
                [
                    {
                        "op": "create_measure",
                        "table": "# Measures",
                        "name": "QA Sales in Thousands",
                        "expression": "[Total Sales] / 1000",
                        "format_string": "#,##0.0",
                    }
                ],
                confirm_model=True,
            )
            assert status == 200, (status, receipt)
            names = {m["Name"] for m in self.api.report(self.ws)["measures"]}
            assert "QA Sales in Thousands" in names, (
                "the new measure is not in the report's measures"
            )
            status, refusal = self.api.edit(
                self.ws,
                receipt["head"]["id"],
                [{"op": "delete_measure", "name": "Total Sales"}],
                confirm_model=True,
            )
            assert status in {409, 422}, (status, refusal)
            assert "used by" in json.dumps(refusal), refusal
            status, renamed = self.api.edit(
                self.ws,
                receipt["head"]["id"],
                [
                    {
                        "op": "rename_measure",
                        "name": "QA Sales in Thousands",
                        "new_name": "QA Thousands",
                    }
                ],
                confirm_model=True,
            )
            assert status == 200, (status, renamed)
            status, undone = self.api.undo(self.ws, renamed["head"]["id"], desktop_work="save")
            assert status == 200, (status, undone)
            status, undone2 = self.api.undo(self.ws, undone["head"]["id"], desktop_work="save")
            assert status == 200, (status, undone2)
            names = {m["Name"] for m in self.api.report(self.ws)["measures"]}
            assert "QA Sales in Thousands" not in names and "QA Thousands" not in names, names
            return "created, guarded a used measure, renamed, then undone twice: the model is back to the import"

        self.step("a measure through Desktop: create, delete guard, rename, undo", measure_cycle)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--endpoint", default="http://127.0.0.1:8767")
    parser.add_argument("--token", required=True)
    parser.add_argument("--pbix", help="a PBIX to import into the (empty) QA state")
    parser.add_argument("--workspace", help="an existing imported workspace instead of importing")
    parser.add_argument(
        "--desktop", action="store_true", help="also run the Power BI Desktop steps"
    )
    args = parser.parse_args()
    battery = Battery(Api(args.endpoint, args.token))
    battery.run(args.pbix, args.workspace, args.desktop)
    folder = Path(__file__).parent / "battery-results"
    folder.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = folder / f"live-{stamp}.json"
    target.write_text(json.dumps(battery.results, indent=1), encoding="utf-8")
    failed = [r for r in battery.results if not r["ok"]]
    print(f"\n{len(battery.results) - len(failed)} passed, {len(failed)} failed; wrote {target}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
