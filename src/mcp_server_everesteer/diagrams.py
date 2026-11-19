"""Programmatic Mermaid diagram generators (C4 architecture, lifecycle, safety, live tools)."""

from collections.abc import Iterable

from mcp_server_everesteer.capabilities import Capabilities
from mcp_server_everesteer.ops import OPERATIONS, Operation, enabled_operations

_C4_ARCHITECTURE = (
    "flowchart TB\n"
    '    user["User / Quant Researcher<br/>[Person]<br/>'
    'Configures gates & approves writes"]\n'
    '    agent["AI Research Agent<br/>[Person / Client]<br/>'
    'Claude Code · Desktop · Cursor · LangGraph"]\n'
    "\n"
    '    subgraph boundary["mcp-server-everesteer [Software System — Python 3.11+ / uv]"]\n'
    "        direction TB\n"
    "\n"
    '        subgraph c_surface["Delivery Surface Containers"]\n'
    "            direction LR\n"
    '            mcp_srv["MCP Server<br/>[Container: MCPServer / Starlette]<br/>'
    'stdio & streamable-http + BearerAuth"]\n'
    '            lg_int["LangGraph Integration<br/>[Container: LangChain / LangGraph]<br/>'
    'In-process _Bridge + interrupt() + draw_mermaid()"]\n'
    '            cli_app["CLI<br/>[Container: argparse]<br/>'
    'serve · auth · status · tools · diagram · logout"]\n'
    "        end\n"
    "\n"
    '        subgraph c_core["Agentic Control Plane"]\n'
    "            direction LR\n"
    '            ops_eng["Operations & Capabilities<br/>[Component: ops/ + capabilities.py]<br/>'
    '6 toolsets · 3 gates · 45s wait cap · progress"]\n'
    '            know["Prompts & Live Resources<br/>[Component: prompts.py + resources.py]<br/>'
    'everesteer://* · dynamic AGENT_GUIDE"]\n'
    "        end\n"
    "\n"
    '        subgraph c_guard["Safety, Models & State"]\n'
    "            direction LR\n"
    '            models["Forward-Compatible Models<br/>[Component: models.py]<br/>'
    "Normalized fields · extra='allow' · text hygiene\"]\n"
    '            safety["Safety & Money Guard<br/>[Component: safety.py + money.py]<br/>'
    '3 gates · micro-USDC · secret scrubbing"]\n'
    '            journal[("SQLite Journal & Cache<br/>[Container: SQLite / JSON]<br/>'
    'Daily USDC caps · dup block · catalog cache")]\n'
    "        end\n"
    "\n"
    '        transport["Async Protocol Transport<br/>[Component: httpx2 + transfer.py]<br/>'
    'Read retry · no write retry · presigned S3"]\n'
    "    end\n"
    "\n"
    '    subgraph ext["External Systems"]\n'
    "        direction LR\n"
    '        api["Everesteer API v1<br/>[External System]<br/>https://api.everesteer.ai/api/v1/*"]\n'
    '        cdn["Presigned S3 / CloudFront CDN<br/>[External System]<br/>'
    'Credential-free dataset & .pkl transfer"]\n'
    '        chain["Staking Contracts / Wallet<br/>[External System]<br/>'
    'USDC deposits · unsigned vault calldata"]\n'
    "    end\n"
    "\n"
    '    user -- "Sets gates & confirms writes" --> agent\n'
    '    agent -- "MCP JSON-RPC (stdio / HTTP)" --> mcp_srv\n'
    '    agent -- "Direct Python tool calls" --> lg_int\n'
    '    user -- "Terminal commands" --> cli_app\n'
    "\n"
    "    mcp_srv --> ops_eng\n"
    "    mcp_srv --> know\n"
    "    lg_int --> ops_eng\n"
    "    lg_int --> know\n"
    "    cli_app --> ops_eng\n"
    "\n"
    "    ops_eng --> models\n"
    "    ops_eng --> safety\n"
    "    ops_eng --> transport\n"
    "    safety --> journal\n"
    "    transport --> journal\n"
    "\n"
    '    transport -- "HTTPS + X-API-Key (+ CF Access)" --> api\n'
    '    transport -- "Presigned PUT/POST/GET (no key)" --> cdn\n'
    '    api -. "Relays locks & settlements" .-> chain\n'
    "\n"
    "    classDef person fill:#1d4ed8,fill-opacity:0.14,stroke:#1d4ed8,stroke-width:2px\n"
    "    classDef external fill:#64748b,fill-opacity:0.08,stroke:#64748b,stroke-dasharray:5 4\n"
    "    classDef guard fill:#dc2626,fill-opacity:0.12,stroke:#dc2626\n"
    "    classDef box fill:#64748b,fill-opacity:0.04,stroke:#64748b,stroke-opacity:0.45\n"
    "    class user,agent person\n"
    "    class api,cdn,chain external\n"
    "    class safety guard\n"
    "    class boundary,c_surface,c_core,c_guard,ext box"
)

_LIFECYCLE = (
    "flowchart LR\n"
    '    start(["whoami & get_started"]) --> mode{"Account Scope?"}\n'
    "\n"
    '    mode -- "Full Tournament" --> t_data["download_dataset<br/>train / validation / live"]\n'
    '    t_data --> t_model["create_model<br/>(opaque name)"]\n'
    '    t_model --> t_path{"Submission Mode?"}\n'
    '    t_path -- "Auto-Submit (Recommended)" --> t_pkl["upload_model (.pkl)<br/>'
    '→ get_model_upload_status<br/>→ set_auto_submit(true)<br/>→ verify lane_active"]\n'
    '    t_path -- "Manual Per-Round" --> t_man["validate_futures_submission<br/>'
    '→ submit_futures_predictions"]\n'
    '    t_pkl --> t_track["get_submission_status<br/>'
    '→ get_daily_progression (T+1..T+20)<br/>→ get_scores"]\n'
    "    t_man --> t_track\n"
    '    t_track --> t_stake["Optional: Strategy / Stake Plans<br/>(ALLOW_STAKING=true)"]\n'
    "\n"
    '    mode -- "Hackathon Event" --> h_data["download_dataset<br/>'
    'train (labeled) + live/val (blank)"]\n'
    '    h_data --> h_lane{"Target Board?"}\n'
    '    h_lane -- "Practice Board (Free)" --> h_val["submit_validation_diagnostics<br/>'
    '(preds + .pkl)"]\n'
    '    h_lane -- "Open Paid Round (Pool)" --> h_evt["submit_event_predictions<br/>'
    '(preds + .pkl)"]\n'
    '    h_val --> h_res["get_diagnostics_run / get_validation_panel<br/>'
    '→ get_diagnostics_leaderboard"]\n'
    "    h_evt --> h_res\n"
    '    h_res --> h_stand["get_diagnostics_standings<br/>(cumulative across rounds)"]'
)

_SAFETY = """\
sequenceDiagram
    autonumber
    participant A as AI Agent
    participant G as Capability & Gate Layer
    participant S as Safety & Money Guard
    participant J as SQLite Write Journal
    participant U as Human User
    participant E as Everesteer API

    A->>G: Call mutating tool (e.g. stake_on_model / withdraw_usdc)
    alt Gate disabled (default)
        G-->>A: Tool hidden / Refused with env-var hint
    else Gate enabled
        G->>S: Validate exact micro-USDC (reject float) & per-call/daily caps
        S->>J: Block if identical pending/unknown write exists
        S->>U: Server-authored confirmation (MCP Elicit / LangGraph interrupt)
        alt User declines or client lacks elicitation
            U-->>A: WriteResult(status="declined") — nothing sent
        else User approves
            S->>J: Record pending write + Idempotency-Key
            S->>E: Single HTTP request (never auto-retried)
            alt Definite response
                E-->>J: Mark sent / rejected
                J-->>A: Typed WriteResult
            else Network drop / timeout
                J-->>A: WriteResult(status="unknown", request_id=...)
            end
        end
    end\
"""

_ACCESS_LABELS = {
    "read": "read-only",
    "write": "writes (ALLOW_WRITES)",
    "funds": "confirmed / staking (ALLOW_STAKING)",
    "destructive": "withdrawals / destructive (mandatory confirm)",
}

_FLOWCHART_HEADER = """\
---
config:
  flowchart:
    nodeSpacing: 24
    rankSpacing: 12
    padding: 8
---
flowchart TB
"""

_STYLES = [
    "classDef default fill:#64748b,fill-opacity:0.06,stroke:#64748b",
    "classDef read fill:#64748b,fill-opacity:0.14,stroke:#64748b",
    "classDef write fill:#d97706,fill-opacity:0.16,stroke:#d97706",
    "classDef funds,approval fill:#dc2626,fill-opacity:0.16,stroke:#dc2626,stroke-width:2px",
    "classDef destructive fill:#991b1b,fill-opacity:0.22,stroke:#991b1b,stroke-width:2px",
    "classDef external fill-opacity:0,stroke-dasharray:5 4",
    "classDef box fill:#64748b,fill-opacity:0.05,stroke:#64748b,stroke-opacity:0.45",
    "classDef row fill:none,stroke:none",
]


def architecture_diagram() -> str:
    """Return the C4-style Mermaid architecture diagram."""
    return _C4_ARCHITECTURE


def lifecycle_diagram() -> str:
    """Return the Mermaid diagram covering Tournament & Hackathon submission lifecycles."""
    return _LIFECYCLE


def safety_diagram() -> str:
    """Return the Mermaid sequence diagram showing write & money safety guardrails."""
    return _SAFETY


def access_level(op: Operation) -> str:
    """Classify an operation for diagram color-coding."""
    if op.destructive or op.gate == "withdrawals":
        return "destructive"
    if op.needs_confirmation or op.gate == "staking":
        return "funds"
    return "read" if op.read_only else "write"


def subgraph(node_id: str, title: str, direction: str, body: list[str]) -> list[str]:
    return [
        f'subgraph {node_id}["{title}"]',
        f"    direction {direction}",
        *(f"    {line}" for line in body),
        "end",
    ]


def in_order(ids: list[str]) -> list[str]:
    return [" ~~~ ".join(ids)] if len(ids) > 1 else []


def build_tool_boxes(names: Iterable[str]) -> dict[str, list[str]]:
    operations = {op.name: op for op in OPERATIONS}
    areas: dict[str, dict[str, list[str]]] = {}
    for name in names:
        op = operations.get(name)
        area = op.toolset if op else "other"
        level = access_level(op) if op else ""
        areas.setdefault(area, {}).setdefault(level, []).append(name)

    boxes: dict[str, list[str]] = {}
    for area, levels in areas.items():
        key = area.replace("-", "_")
        nodes = [
            f'tools_{key}_{level or "all"}["{"<br/>".join(levels[level])}"]'
            + (f":::{level}" if level else "")
            for level in (*_ACCESS_LABELS, "")
            if level in levels
        ]
        boxes[f"tools_{key}"] = subgraph(f"tools_{key}", area, "LR", nodes)
    return boxes


def render_tool_flowchart(
    tool_names: Iterable[str],
    *,
    title: str = "Enabled Tools",
    lead_lines: Iterable[str] = (),
    lead_boxes: Iterable[str] = (),
    connect_from: str | None = None,
) -> str:
    """Render a Mermaid flowchart containing optional lead nodes and grouped tool boxes."""
    names = list(dict.fromkeys(tool_names))
    operations = {op.name: op for op in OPERATIONS}
    lines = list(lead_lines)
    boxes = list(lead_boxes)
    rows: list[str] = []
    areas = build_tool_boxes(names)
    if areas:
        ids = list(areas)
        grid: list[str] = []
        for start in range(0, len(ids), 3):
            row = ids[start : start + 3]
            rows.append(f"tools_row{start // 3}")
            body = [line for box in row for line in areas[box]]
            grid += subgraph(rows[-1], " ", "LR", body + in_order(row))
        lines += subgraph("tools", title, "TB", grid + in_order(rows))
        if connect_from is not None:
            lines.append(f"{connect_from} -- tool calls --> tools")
        boxes += ["tools", *ids]

    used = {access_level(operations[name]) for name in names if name in operations}
    if used:
        key = [
            f'legend_{level}["{text}"]:::{level}'
            for level, text in _ACCESS_LABELS.items()
            if level in used
        ]
        lines += subgraph("legend", "Legend", "TB", key)
        lines += [
            'everesteer["Everesteer API v1"]:::external',
            "tools -- HTTPS --> everesteer",
            "tools ~~~ legend",
        ]
        boxes.append("legend")

    lines += _STYLES
    if boxes:
        lines.append(f"class {','.join(boxes)} box")
    if rows:
        lines.append(f"class {','.join(rows)} row")
    return _FLOWCHART_HEADER + "".join(f"    {line}\n" for line in lines)


def tools_diagram(caps: Capabilities) -> str:
    """Render a Mermaid diagram of every tool exposed under `caps`, grouped by toolset."""
    return render_tool_flowchart(
        [op.name for op in enabled_operations(caps)],
        title="Enabled Tools",
    )
