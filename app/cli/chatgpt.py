"""Sign in with ChatGPT, import the credentials and run a live smoke test.

  login   Run on the computer with the browser. Saves owner-only credentials under
          ~/.config/x-ai-auto-post/ (no database or server keys needed).
  import  Run where the API and Worker run. Reads that credential file from stdin and
          stores it encrypted in PostgreSQL.
  smoke   Run where the API and Worker run. Lists the account's models and completes one
          Responses API request. It uses the ChatGPT plan's usage limits.
"""
import argparse
import asyncio
import http.server
import json
import re
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from app.core.config import CHATGPT_MODELS_URL
from app.services import chatgpt_signin as signin
from app.services.auth import OAuthService, ServiceError
from app.services.openai import ResponsesClient

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "x-ai-auto-post"
HOST_ID_FILE = "host-id"
CREDENTIALS_FILE = "chatgpt-credentials.json"
DEFAULT_PORT = 1455
DEFAULT_TIMEOUT_SECONDS = 300
SMOKE_PROMPT = "Reply with exactly: OK"
SAFE_ERROR_CODE = re.compile(r"[a-z0-9_.-]{1,64}")


class _CallbackServer(http.server.HTTPServer):
    query = None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    timeout = 10

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != signin.CALLBACK_PATH:
            self.send_error(404)
            return
        # Repeated parameters are dropped so that they can never satisfy the state check.
        values = parse_qs(url.query, keep_blank_values=True)
        self.server.query = {key: items[0] for key, items in values.items() if len(items) == 1}
        body = "Sign-in finished. Return to the terminal.\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # The request line contains the authorization code; never print it.
        pass


def _wait_for_callback(server, timeout):
    deadline = time.monotonic() + timeout
    while server.query is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceError("oauth_callback_timeout")
        server.timeout = remaining
        server.handle_request()
    return server.query


def login(*, config_dir: Path, port: int, consent: bool, timeout: float,
          http_client: httpx.Client, open_browser, out, err) -> int:
    credentials_path = config_dir / CREDENTIALS_FILE
    try:
        host_id = signin.load_or_create_host_id(config_dir / HOST_ID_FILE)
        saved = signin.read_record(credentials_path)
    except ServiceError as exc:
        print(f"Sign-in failed: error_code={exc.error_code}", file=err)
        return 1
    try:
        server = _CallbackServer(("127.0.0.1", port), _CallbackHandler)
    except (OSError, OverflowError):
        print(f"Sign-in failed: 127.0.0.1:{port} is not available. Choose another --port.", file=err)
        return 1
    with server:
        url, attempt = signin.start(signin.callback_uri(server.server_port), host_id,
                                    saved["client_id"] if saved else None, consent=consent)
        print("Opening the browser for Sign in with ChatGPT. If it does not open, visit:", file=out)
        print(url, file=out)
        open_browser(url)
        try:
            query = _wait_for_callback(server, timeout)
            record = signin.finish(http_client, attempt, query, host_id,
                                   expected_subject=saved["subject"] if saved else None)
        except ServiceError as exc:
            print(f"Sign-in failed: error_code={exc.error_code}", file=err)
            if exc.error_code == "oauth_scope_missing":
                print("ChatGPT plan usage was not granted. Run again with --consent to approve it.",
                      file=err)
            return 1
    signin.write_private_json(credentials_path, record)
    print(f"Signed in as {record['email'] or record['subject']}. ChatGPT plan usage: enabled.", file=out)
    print(f"Saved owner-only credentials: {credentials_path}", file=out)
    print("Next: import them where the API and Worker run (see README).", file=out)
    return 0


def import_credentials(stream, auth: OAuthService, out, err) -> int:
    try:
        auth.import_chatgpt(json.load(stream))
    except ValueError:
        print("Import failed: error_code=oauth_import_invalid", file=err)
        return 1
    except ServiceError as exc:
        print(f"Import failed: error_code={exc.error_code}", file=err)
        return 1
    status = auth.status("chatgpt")
    print(f"Imported ChatGPT credentials: client_id={status['client_id']} host_id={status['host_id']}",
          file=out)
    return 0


async def smoke(settings, auth: OAuthService, http_client: httpx.AsyncClient, out, err) -> int:
    try:
        models = await _listed_models(http_client, await auth.access_token("chatgpt"))
    except ServiceError as exc:
        _print_failure(err, "Model list", exc)
        return 1
    print("Available models: " + ", ".join(models), file=out)
    if settings.chatgpt_model not in models:
        print(f"CHATGPT_MODEL={settings.chatgpt_model!r} is not available to this account.", file=err)
        return 1
    try:
        text = await ResponsesClient(settings, auth, http_client).complete(SMOKE_PROMPT)
    except ServiceError as exc:
        _print_failure(err, "Responses", exc)
        return 1
    print(f"OK: response.completed received ({len(text)} characters).", file=out)
    return 0


def _print_failure(err, step, exc):
    request_id = getattr(exc, "request_id", None) or "-"
    print(f"{step} failed: error_code={exc.error_code} request_id={request_id}", file=err)


async def _listed_models(http_client, access_token):
    """Model slugs intended for display, in the server's order."""
    try:
        response = await http_client.get(
            CHATGPT_MODELS_URL, headers={"Authorization": "Bearer " + access_token},
            timeout=30, follow_redirects=False)
    except httpx.RequestError:
        raise ServiceError("models_transport_error", retryable=True) from None
    if response.status_code != 200:
        raise ServiceError(_body_error_code(response) or "models_api_error")
    try:
        models = response.json()["models"]
        if not isinstance(models, list):
            raise TypeError
        return [model["slug"] for model in models if isinstance(model, dict)
                and model.get("visibility") == "list" and isinstance(model.get("slug"), str)]
    except (ValueError, KeyError, TypeError):
        raise ServiceError("models_invalid") from None


def _body_error_code(response):
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None
    return code if isinstance(code, str) and SAFE_ERROR_CODE.fullmatch(code) else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli.chatgpt", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    login_parser = commands.add_parser("login", help="sign in on this computer (opens a browser)")
    login_parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                              help="loopback callback port on 127.0.0.1 (0 picks a free port)")
    login_parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    login_parser.add_argument("--consent", action="store_true",
                              help="show the consent screen again to grant ChatGPT plan usage")
    login_parser.add_argument("--no-browser", action="store_true",
                              help="only print the authorization URL")
    login_parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                              help="seconds to wait for the browser callback")
    commands.add_parser("import", help="store credentials read from stdin")
    commands.add_parser("smoke", help="check the model catalog and complete one response")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "login":
        open_browser = (lambda url: None) if args.no_browser else webbrowser.open
        with httpx.Client(follow_redirects=False) as client:
            return login(config_dir=args.config_dir, port=args.port, consent=args.consent,
                         timeout=args.timeout, http_client=client, open_browser=open_browser,
                         out=sys.stdout, err=sys.stderr)
    from app.core.config import get_settings
    from app.core.db import get_session_factory

    settings, factory = get_settings(), get_session_factory()
    if args.command == "import":
        return import_credentials(sys.stdin, OAuthService(settings, factory, None),
                                  sys.stdout, sys.stderr)
    return asyncio.run(_run_smoke(settings, factory))


async def _run_smoke(settings, factory):
    async with httpx.AsyncClient(follow_redirects=False) as client:
        return await smoke(settings, OAuthService(settings, factory, client), client,
                           sys.stdout, sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
