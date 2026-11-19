"""Toolsets, three default-closed safety gates, and visibility rules."""

from dataclasses import dataclass

from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.errors import ConfigError

REQUIREMENTS = {
    "api_key": (
        "an Everesteer API key: run `uvx mcp-server-everesteer auth` or set "
        "EVERESTEER_API_KEY (or EIQ_API_KEY)"
    ),
}

GATES = {
    "writes": "EVERESTEER_ALLOW_WRITES=true",
    "staking": "EVERESTEER_ALLOW_STAKING=true",
    "withdrawals": "EVERESTEER_ALLOW_WITHDRAWALS=true",
}

TOOLSETS = {
    "core": "Orientation, scoring rules, rounds, seasons, benchmarks, and dataset downloads",
    "models": "Model catalog, .pkl uploads, auto-submit lane, and multipliers",
    "compete": "Submissions, pre-flight validation, diagnostics, leaderboards, and scores",
    "compute": "Hosted training jobs, credit balance, dry-run pricing, logs, and artifacts",
    "account": "Agent profile, notifications, badges, API keys, registration, and freeze",
    "staking": "Classic/CREATE2/Vault/Event staking, payouts, wallet security, and withdrawals",
    "equities": "Alps equities tournament tools (opt-in via +equities; currently unlaunched)",
}

BASE_TOOLSETS = frozenset({"core", "models", "compete", "compute", "account"})
_META = {
    "auto": BASE_TOOLSETS,
    "all": frozenset(TOOLSETS),
}


def parse_toolsets(spec: str, *, include_staking: bool = False) -> frozenset[str]:
    """Parse `auto`, `all`, explicit toolset names, and `+name` / `-name` deltas."""
    selected: set[str] = set()
    tokens = [token.strip().lower() for token in spec.split(",") if token.strip()] or ["auto"]
    explicit_minus_staking = "-staking" in tokens
    if all(token[0] in "+-" for token in tokens):
        tokens.insert(0, "auto")
    for token in tokens:
        sign, name = (token[0], token[1:]) if token[0] in "+-" else ("+", token)
        names = _META.get(name) or ({name} if name in TOOLSETS else None)
        if names is None:
            known = ", ".join([*_META, *TOOLSETS])
            raise ConfigError(f"Unknown toolset {name!r} in EVERESTEER_TOOLSETS. Known: {known}.")
        if sign == "+":
            selected |= names
        else:
            selected -= names
    if include_staking and "auto" in tokens and not explicit_minus_staking:
        selected.add("staking")
    return frozenset(selected)


@dataclass(frozen=True)
class Capabilities:
    """Snapshot of active toolsets, configured credentials, and open safety gates."""

    toolsets: frozenset[str]
    have: frozenset[str]
    gates: frozenset[str]
    notes: tuple[str, ...] = ()

    @classmethod
    def detect(cls, settings: Settings) -> "Capabilities":
        have: set[str] = set()
        notes: list[str] = []
        try:
            if settings.credentials() is not None:
                have.add("api_key")
        except ConfigError as exc:
            notes.append(exc.message)

        gates: set[str] = set()
        if settings.allow_writes:
            gates.add("writes")
        if settings.allow_staking:
            gates.add("staking")
        if settings.allow_withdrawals:
            gates.add("withdrawals")

        return cls(
            toolsets=parse_toolsets(
                settings.toolsets,
                include_staking=settings.allow_staking or settings.allow_withdrawals,
            ),
            have=frozenset(have),
            gates=frozenset(gates),
            notes=tuple(dict.fromkeys(notes)),
        )


def visible(
    toolset: str, requires: tuple[str, ...], gate: str | None, caps: Capabilities
) -> tuple[bool, str]:
    """Return whether a tool is exposed under `caps`, and the exact step to enable it if not."""
    if toolset not in caps.toolsets:
        return False, f"add `{toolset}` to EVERESTEER_TOOLSETS (e.g. `auto,+{toolset}`)"
    if gate is not None and gate not in caps.gates:
        return False, f"set {GATES[gate]}"
    missing = [name for name in requires if name not in caps.have]
    if missing:
        return False, "needs " + "; ".join(REQUIREMENTS[name] for name in missing)
    return True, ""
