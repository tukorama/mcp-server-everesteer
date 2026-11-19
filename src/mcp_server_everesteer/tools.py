"""MCP tool registration, progress reporting, 45 s client wait cap, and Elicit confirmation."""

import inspect
import logging
import typing
from typing import Annotated, Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Context,
    Elicit,
    ElicitationResult,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from mcp_server_everesteer.capabilities import Capabilities
from mcp_server_everesteer.errors import EveresteerError
from mcp_server_everesteer.models import WriteResult
from mcp_server_everesteer.ops import (
    Call,
    Operation,
    Runtime,
    confirmation_required,
    enabled_operations,
)
from mcp_server_everesteer.redaction import scrub

logger = logging.getLogger(__name__)

# Claude Desktop cancels stdio tool calls after ~60 s, so blocking waits are capped at 45 s
# and return `status="pending"` with a resumable id instead of timing out mid-call.
SHORT_CALL_CLIENTS = ("desktop", "claude-ai")
SHORT_CALL_SECONDS = 45.0


class Confirmation(BaseModel):
    """Human approval prompt payload for confirmed MCP tool calls."""

    confirm: bool = Field(default=False, description="Proceed with this action on Everesteer?")


def _wait_cap(ctx: Context[Any, Any]) -> float | None:
    params = ctx.session.client_params
    if params is None:
        return None
    name = params.client_info.name.lower()
    if any(marker in name for marker in SHORT_CALL_CLIENTS):
        logger.info(
            "Client %s enforces short tool timeouts; capping waits to %.0fs",
            name,
            SHORT_CALL_SECONDS,
        )
        return SHORT_CALL_SECONDS
    return None


def runtime_of(ctx: Context[Any, Any]) -> Runtime:
    return cast(Runtime, ctx.request_context.lifespan_context)


def make_call(ctx: Context[Any, Any]) -> Call:
    async def report(fraction: float, message: str) -> None:
        await ctx.report_progress(fraction, 1.0, message)

    return Call(runtime=runtime_of(ctx), report=report, wait_cap=_wait_cap(ctx))


def agent_error(exc: BaseException, op_name: str) -> ToolError:
    if isinstance(exc, EveresteerError):
        return ToolError(scrub(exc.for_agent()))
    logger.exception("Unexpected failure in %s", op_name)
    return ToolError(scrub(f"Internal error: {type(exc).__name__}: {exc}"))


async def _invoke(op: Operation, ctx: Context[Any, Any], kwargs: dict[str, Any]) -> Any:
    try:
        return await op.fn(make_call(ctx), **kwargs)
    except ToolError:
        raise
    except Exception as exc:
        raise agent_error(exc, op.name) from exc


def refusal_without_elicitation(op: Operation) -> str:
    if op.mandatory_confirm or op.gate == "withdrawals":
        return (
            f"{op.name} requires explicit human confirmation, but this MCP client does not support "
            "elicitation. Use a client that supports elicitation."
        )
    return (
        f"{op.name} asks for human confirmation before running, and this MCP client does not "
        "support elicitation. Call with dry_run=true where supported, use a client with "
        "elicitation, or set EVERESTEER_CONFIRM=none to skip confirmation for non-withdrawal "
        "writes."
    )


def _resolver(op: Operation, arg_names: list[str]) -> Any:
    describe = op.describe
    if describe is None:
        raise TypeError(f"{op.name} has no describe callable")

    async def ask(**kwargs: Any) -> Any:
        ctx = cast(Context[Any, Any], kwargs.pop("ctx"))
        settings = runtime_of(ctx).settings
        if not confirmation_required(op, settings, kwargs):
            return Confirmation(confirm=True)
        resuming = ctx.input_responses is not None or ctx.request_state is not None
        capabilities = ctx.client_capabilities
        if not resuming and (capabilities is None or capabilities.elicitation is None):
            raise ToolError(refusal_without_elicitation(op))
        try:
            text = await describe(make_call(ctx), **kwargs)
        except Exception as exc:
            raise agent_error(exc, op.name) from exc
        return Elicit(scrub(text), Confirmation)

    returns = Elicit[Confirmation] | Confirmation
    parameters = [inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context)]
    parameters += [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY) for name in arg_names]
    ask.__name__ = ask.__qualname__ = f"confirm_{op.name}"
    ask.__signature__ = inspect.Signature(parameters, return_annotation=returns)
    ask.__annotations__ = {"ctx": Context, "return": returns}
    return ask


def _declined(result_type: type[WriteResult]) -> WriteResult:
    return result_type(
        status="declined",
        message="The user did not confirm, so nothing was sent.",
        next_step="Do not retry unless the user explicitly asks again.",
    )


def tool_function(op: Operation) -> Any:
    """Wrap a transport-agnostic Operation as an MCPServer tool function."""
    signature = inspect.signature(op.fn, eval_str=True)
    hints = typing.get_type_hints(op.fn, include_extras=True)
    params = [
        param.replace(kind=inspect.Parameter.KEYWORD_ONLY)
        for param in list(signature.parameters.values())[1:]
    ]
    names = [param.name for param in params]
    annotations: dict[str, Any] = {name: hints[name] for name in names if name in hints}
    extra = [inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context)]
    annotations["ctx"] = Context

    if op.needs_confirmation:
        returns = hints.get("return")
        if not (isinstance(returns, type) and issubclass(returns, WriteResult)):
            raise TypeError(f"{op.name} requires confirmation, so it must return WriteResult")
        _declined(returns)
        confirmed = Annotated[ElicitationResult[Confirmation], Resolve(_resolver(op, names))]
        extra.append(
            inspect.Parameter("confirmation", inspect.Parameter.KEYWORD_ONLY, annotation=confirmed)
        )
        annotations["confirmation"] = confirmed

    async def tool(ctx: Context[Any, Any], confirmation: Any = None, **kwargs: Any) -> Any:
        if op.needs_confirmation:
            accepted = isinstance(confirmation, AcceptedElicitation) and bool(
                cast(Confirmation, cast(Any, confirmation).data).confirm
            )
            if not accepted:
                return _declined(cast(type[WriteResult], hints["return"]))
        return await _invoke(op, ctx, kwargs)

    annotations["return"] = hints.get("return", Any)
    tool.__name__ = op.name
    tool.__qualname__ = op.name
    tool.__doc__ = op.description
    tool.__signature__ = signature.replace(parameters=[*params, *extra])
    tool.__annotations__ = annotations
    return tool


def register_tools(server: MCPServer, caps: Capabilities) -> list[Operation]:
    """Register every enabled Operation on ``server`` with MCP ToolAnnotations."""
    operations = enabled_operations(caps)
    for op in operations:
        server.add_tool(
            tool_function(op),
            name=op.name,
            title=op.title,
            description=op.description,
            annotations=ToolAnnotations(
                read_only_hint=op.read_only,
                destructive_hint=op.destructive,
                idempotent_hint=op.idempotent,
                open_world_hint=True,
            ),
        )
    return operations
