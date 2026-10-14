"""Run the authenticated local studio sidecar."""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys
from pathlib import Path

import uvicorn

from .api import create_app
from .service import StudioService, default_state_root
from .state_owner import StateRootOwnedError, own_state_root


def main() -> None:
    parser = argparse.ArgumentParser(description="Modelcraft local sidecar")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "::1"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=None)
    parser.add_argument(
        "--emit-token",
        action="store_true",
        help="Print a generated bearer token on stdout for parent-process handoff",
    )
    parser.add_argument(
        "--dev-browser-origin",
        action="append",
        default=None,
        help="Explicit loopback dev origin; may be repeated, omit for native production",
    )
    parser.add_argument(
        "--parent-stdin-lifetime",
        action="store_true",
        help="Shut down gracefully when the native parent's stdin pipe closes",
    )
    bundled_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    parser.add_argument("--repository", default=str(bundled_root))
    parser.add_argument("--state-root", default=str(default_state_root()))
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("port must be between 1 and 65535")
    generated_token = arguments.token is None
    token = arguments.token or secrets.token_urlsafe(32)
    if generated_token and not arguments.emit_token:
        parser.error("generated tokens require --emit-token so the parent process can capture them")
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        with own_state_root(arguments.state_root) as state_root:
            _serve(arguments, token, generated_token, state_root)
    except StateRootOwnedError as error:
        parser.exit(2, f"Modelcraft startup failed: {error}\n")


def _serve(
    arguments: argparse.Namespace, token: str, generated_token: bool, state_root: Path
) -> None:
    service = StudioService(arguments.repository, state_root)
    print(
        json.dumps(
            {
                "event": "sidecar_starting",
                "endpoint": f"http://{arguments.host}:{arguments.port}",
                "token": token if arguments.emit_token and generated_token else None,
            }
        ),
        flush=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(service, token, dev_browser_origins=arguments.dev_browser_origin),
            host=arguments.host,
            port=arguments.port,
            log_level="info",
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )

    def stop_server() -> None:
        server.should_exit = True

    if getattr(sys, "frozen", False):
        from .parent_lifetime import watch_parent_exit

        # Tauri terminates the PyInstaller launcher; its Python child must
        # still run the lifespan teardown that unloads the guarded model.
        watch_parent_exit(stop_server)
    if arguments.parent_stdin_lifetime:
        from .parent_lifetime import watch_stdin_eof

        # The native host owns the pipe writer even when the PyInstaller
        # launcher survives its exit. Keep normal ASGI lifespan teardown.
        watch_stdin_eof(stop_server)
    server.run()


if __name__ == "__main__":
    main()
