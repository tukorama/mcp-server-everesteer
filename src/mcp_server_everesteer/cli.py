"""Command-line interface for mcp-server-everesteer."""

import argparse
import asyncio
import getpass
import json
import sys
from typing import Any

import uvicorn
from pydantic import ValidationError

from mcp_server_everesteer import __version__, diagrams
from mcp_server_everesteer.config import Settings, write_private_json
from mcp_server_everesteer.errors import EveresteerError
from mcp_server_everesteer.http_app import build_http_app
from mcp_server_everesteer.logging_setup import configure_logging
from mcp_server_everesteer.ops import Runtime, enabled_operations, hidden_operations
from mcp_server_everesteer.ops.core import whoami
from mcp_server_everesteer.ops.registry import Call
from mcp_server_everesteer.server import create_server, detect_capabilities


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-server-everesteer",
        description="Agent-first MCP server and LangGraph tools for the Everesteer API.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    auth = commands.add_parser("auth", help="store an API key (and optional CF Access token)")
    auth.add_argument(
        "--cf-access",
        action="store_true",
        help="also prompt for Cloudflare Access service token credentials",
    )
    auth.add_argument(
        "--no-verify",
        action="store_true",
        help="save credentials locally without making a verification request",
    )

    commands.add_parser("logout", help="remove stored local credentials")
    commands.add_parser("status", help="show identity fingerprint, active gates, and toolsets")
    commands.add_parser("tools", help="list exposed and hidden tools for the current configuration")

    diagram = commands.add_parser(
        "diagram",
        help="print Mermaid diagrams generated from code (architecture, lifecycle, safety, tools)",
    )
    diagram.add_argument(
        "--kind",
        choices=["architecture", "lifecycle", "safety", "tools"],
        default="tools",
    )
    diagram.add_argument(
        "--fence",
        action="store_true",
        help="wrap output in a ```mermaid markdown code fence",
    )
    return parser


def _serve(args: argparse.Namespace, settings: Settings) -> int:
    server = create_server(settings)
    if args.transport == "stdio":
        server.run()
        return 0
    app = build_http_app(server, settings, host=args.host)
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)
    return 0


def _collect_auth_payload(args: argparse.Namespace) -> dict[str, Any] | None:
    api_key = getpass.getpass("Everesteer API key (eiq_...): ").strip()
    if not api_key:
        print("Aborted: API key cannot be empty.", file=sys.stderr)
        return None

    payload: dict[str, Any] = {"api_key": api_key}
    if args.cf_access:
        cf_id = input("CF-Access-Client-Id: ").strip()
        cf_secret = getpass.getpass("CF-Access-Client-Secret: ").strip()
        if cf_id and cf_secret:
            payload["cf_access_client_id"] = cf_id
            payload["cf_access_client_secret"] = cf_secret
    return payload


async def _verify_saved_credentials(settings: Settings) -> int:
    fresh = Settings(
        base_url=settings.base_url,
        credentials_file=settings.credentials_file,
    )
    runtime = Runtime.create(fresh)
    try:
        identity = await whoami(Call(runtime=runtime))
    finally:
        await runtime.aclose()
    print(json.dumps(identity.model_dump(mode="json"), indent=2))
    return 0 if identity.authenticated else 1


def _auth(args: argparse.Namespace, settings: Settings) -> int:
    payload = _collect_auth_payload(args)
    if payload is None:
        return 1

    path = settings.resolved_credentials_file
    write_private_json(path, payload)
    print(f"Saved credentials to {path} (0600 permissions).", file=sys.stderr)

    if args.no_verify:
        return 0
    return asyncio.run(_verify_saved_credentials(settings))


def _logout(settings: Settings) -> int:
    path = settings.resolved_credentials_file
    if path.exists():
        path.unlink()
        print(f"Removed stored credentials at {path}.")
    else:
        print(f"No stored credentials found at {path}.")
    return 0


async def _status(settings: Settings) -> int:
    runtime = Runtime.create(settings)
    try:
        identity = await whoami(Call(runtime=runtime))
    finally:
        await runtime.aclose()
    print(json.dumps(identity.model_dump(mode="json"), indent=2))
    return 0


def _tools(settings: Settings) -> int:
    caps = detect_capabilities(settings)
    operations = enabled_operations(caps)
    width = max((len(op.name) for op in operations), default=10)
    for op in operations:
        gate_tag = f" [gate:{op.gate}]" if op.gate else ""
        print(f"{op.name:<{width}}  [{op.toolset}] {op.title}{gate_tag}")
    print(f"\n{len(operations)} tools exposed.")

    hidden: dict[str, list[str]] = {}
    for op, reason in hidden_operations(caps):
        hidden.setdefault(reason, []).append(op.name)
    if hidden:
        print("\nHidden tools:")
        for reason, names in hidden.items():
            print(f"- {len(names)} tools: {reason}")
    return 0


def _diagram(args: argparse.Namespace, settings: Settings) -> int:
    if args.kind == "architecture":
        text = diagrams.architecture_diagram()
    elif args.kind == "lifecycle":
        text = diagrams.lifecycle_diagram()
    elif args.kind == "safety":
        text = diagrams.safety_diagram()
    else:
        text = diagrams.tools_diagram(detect_capabilities(settings))

    if args.fence:
        print(f"```mermaid\n{text.rstrip()}\n```")
    else:
        print(text.rstrip())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings()
    except ValidationError as exc:
        for error in exc.errors():
            name = "_".join(str(part) for part in error["loc"]).upper()
            print(f"Invalid EVERESTEER_{name}: {error['msg']}", file=sys.stderr)
        return 2

    configure_logging(settings.log_level)
    command = args.command or "serve"
    if args.command is None:
        args = _parser().parse_args(["serve"])

    try:
        if command == "serve":
            return _serve(args, settings)
        if command == "auth":
            return _auth(args, settings)
        if command == "logout":
            return _logout(settings)
        if command == "status":
            return asyncio.run(_status(settings))
        if command == "tools":
            return _tools(settings)
        return _diagram(args, settings)
    except EveresteerError as exc:
        print(exc.for_agent(), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130
