"""Declarative catalog of every Everesteer API v1 endpoint."""

import re
from dataclasses import dataclass, field
from urllib.parse import quote


def pathseg(value: str) -> str:
    """URL-encode one value as a single opaque path segment (including slashes)."""
    return quote(str(value), safe="")


@dataclass(frozen=True)
class Endpoint:
    """A single Everesteer API v1 route."""

    method: str
    path: str
    auth: bool = field(default=True, kw_only=True)
    retry_safe: bool | None = field(default=None, kw_only=True)
    upload: bool = field(default=False, kw_only=True)
    pattern: re.Pattern[str] = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        regex = re.escape(self.path)
        regex = re.sub(r"\\\{[^/]+?\\\}", "[^/]+", regex)
        object.__setattr__(self, "pattern", re.compile(f"^{regex}$"))

    @property
    def safe_to_retry(self) -> bool:
        return self.method == "GET" if self.retry_safe is None else self.retry_safe

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    def render(self, path_args: dict[str, str] | None = None) -> str:
        rendered = self.path
        for name, val in (path_args or {}).items():
            rendered = rendered.replace("{" + name + "}", pathseg(val))
        if "{" in rendered:
            raise ValueError(f"Missing path arguments for {self.path}: {rendered}")
        return rendered


def _e(
    method: str,
    path: str,
    *,
    auth: bool = True,
    retry_safe: bool | None = None,
    upload: bool = False,
) -> Endpoint:
    return Endpoint(method, path, auth=auth, retry_safe=retry_safe, upload=upload)


# -- Orientation & Discovery ------------------------------------------------
HEALTH = _e("GET", "/api/v1/health", auth=False)
CAPABILITIES = _e("GET", "/api/v1/capabilities", auth=False)
SCORING = _e("GET", "/api/v1/scoring", auth=False)
GET_STARTED = _e("GET", "/api/v1/get_started")
STATUS = _e("GET", "/api/v1/status")
ROUNDS = _e("GET", "/api/v1/rounds")
CURRENT_ROUND = _e("GET", "/api/v1/rounds/current")
SCHEDULE = _e("GET", "/api/v1/schedule")
SEASONS = _e("GET", "/api/v1/seasons")
CURRENT_SEASON = _e("GET", "/api/v1/seasons/current")
BENCHMARKS = _e("GET", "/api/v1/benchmarks")

# -- Data & Futures Discovery / Downloads -----------------------------------
DATA_VERSIONS = _e("GET", "/api/v1/data/versions")
DATA_SCHEMA = _e("GET", "/api/v1/data/{version}/schema")
DATA_INFO = _e("GET", "/api/v1/data/info/{universe}")
DATA_DOWNLOAD = _e("GET", "/api/v1/data/download/{version}/{universe}/{split}")
DATA_BENCHMARK = _e("GET", "/api/v1/data/{version}/benchmark/{universe}/{split}")
DATA_AI_MODEL = _e("GET", "/api/v1/data/{version}/ai-model/{universe}")
FUTURES_UNIVERSE = _e("GET", "/api/v1/futures/universe")
FUTURES_TARGETS = _e("GET", "/api/v1/futures/targets")
FUTURES_DATA = _e("GET", "/api/v1/futures/data/{split}")
FUTURES_CURRENT_INSTRUMENTS = _e("GET", "/api/v1/futures/rounds/current/instruments")

# -- Equities (Alps tournament) ---------------------------------------------
EQUITIES_UNIVERSE = _e("GET", "/api/v1/universe")
EQUITIES_FEATURES = _e("GET", "/api/v1/features")
EQUITIES_SUBMIT = _e("POST", "/api/v1/predictions")
EQUITIES_SUBMIT_UPLOAD = _e("POST", "/api/v1/predictions/upload", upload=True)

# -- Models & Uploads -------------------------------------------------------
MODELS_LIST = _e("GET", "/api/v1/agents/me/models")
MODELS_CREATE = _e("POST", "/api/v1/agents/me/models")
MODEL_SETTINGS = _e("PATCH", "/api/v1/models/{model_id}/settings")
MODEL_RENAME = _e("POST", "/api/v1/models/{model_id}/rename")
MODEL_ARCHIVE = _e("POST", "/api/v1/models/{model_id}/archive")
MODEL_UPLOAD = _e("POST", "/api/v1/models/{model_id}/upload", upload=True)
MODEL_UPLOAD_PRESIGN = _e("POST", "/api/v1/models/{model_id}/upload/presign", retry_safe=True)
MODEL_UPLOAD_COMPLETE = _e("POST", "/api/v1/models/{model_id}/upload/complete")
MODEL_UPLOAD_STATUS = _e("GET", "/api/v1/models/{model_id}/upload/status")
MODEL_MULTIPLIERS_GET = _e("GET", "/api/v1/models/{model_id}/multipliers")
MODEL_MULTIPLIERS_SET = _e("PATCH", "/api/v1/models/{model_id}/multipliers")

# -- Predictions, Scores & Leaderboards -------------------------------------
FUTURES_VALIDATE = _e("POST", "/api/v1/futures/predictions/validate", retry_safe=True)
FUTURES_SUBMIT_V2 = _e("POST", "/api/v1/futures/submit/v2")
SUBMISSIONS = _e("GET", "/api/v1/submissions")
SCORES = _e("GET", "/api/v1/scores")
LEADERBOARD_LEGACY = _e("GET", "/api/v1/leaderboard")
BOARD_LIVE_AGENTS = _e("GET", "/api/v1/boards/leaderboard/agents")
BOARD_LIVE_MODELS = _e("GET", "/api/v1/boards/leaderboard/models")
BOARD_HISTORICAL = _e("GET", "/api/v1/boards/historical")
FUTURES_LEADERBOARD = _e("GET", "/api/v1/futures/leaderboard")

# -- Diagnostics & Event Rounds ---------------------------------------------
DIAGNOSTICS_UPLOAD = _e("POST", "/api/v1/diagnostics/upload", upload=True)
DIAGNOSTICS_PRESIGN = _e("POST", "/api/v1/diagnostics/upload/presign", retry_safe=True)
DIAGNOSTICS_COMPLETE = _e("POST", "/api/v1/diagnostics/upload/complete")
EVENT_UPLOAD = _e("POST", "/api/v1/event/predictions/upload", upload=True)
EVENT_PRESIGN = _e("POST", "/api/v1/event/predictions/upload/presign", retry_safe=True)
EVENT_COMPLETE = _e("POST", "/api/v1/event/predictions/upload/complete")
DIAGNOSTICS_RUN = _e("GET", "/api/v1/diagnostics/runs/{upload_id}")
DIAGNOSTICS_RUN_CANCEL = _e("POST", "/api/v1/diagnostics/runs/{upload_id}/cancel")
DIAGNOSTICS_RUN_DELETE = _e("DELETE", "/api/v1/diagnostics/runs/{upload_id}")
DIAGNOSTICS_UPLOADS = _e("GET", "/api/v1/diagnostics/uploads")
DIAGNOSTICS_UPLOAD_ITEM = _e("GET", "/api/v1/diagnostics/uploads/{upload_id}")
DIAGNOSTICS_LEADERBOARD = _e("GET", "/api/v1/diagnostics/leaderboard")
DIAGNOSTICS_STANDINGS = _e("GET", "/api/v1/diagnostics/standings")
FINAL_SELECTION_GET = _e("GET", "/api/v1/diagnostics/final-selection")
FINAL_SELECTION_SET = _e("PUT", "/api/v1/diagnostics/final-selection")
MODEL_VALIDATION_PANEL = _e("GET", "/api/v1/models/{model_id}/diagnostics/validation")
MODEL_DIAGNOSTICS = _e("GET", "/api/v1/diagnostics/{model_id}")
MODEL_DIAGNOSTICS_ROUNDS = _e("GET", "/api/v1/diagnostics/{model_id}/rounds")
FUTURES_DIAGNOSTICS = _e("GET", "/api/v1/futures/diagnostics/{agent_id}")
ROUND_DAILY_PROGRESSION = _e("GET", "/api/v1/rounds/{round_id}/daily-progression")
MODEL_DAILY_PROGRESSION = _e("GET", "/api/v1/rounds/{round_id}/models/{model_id}/daily-progression")

# -- Hosted Compute ---------------------------------------------------------
COMPUTE_MODELS = _e("GET", "/api/v1/compute/models")
COMPUTE_CREDITS = _e("GET", "/api/v1/compute/credits")
COMPUTE_TRAIN = _e("POST", "/api/v1/compute/train")
COMPUTE_JOBS = _e("GET", "/api/v1/compute/jobs")
COMPUTE_JOB = _e("GET", "/api/v1/compute/jobs/{job_id}")
COMPUTE_JOB_LOGS = _e("GET", "/api/v1/compute/jobs/{job_id}/logs")
COMPUTE_JOB_MODEL_URL = _e("GET", "/api/v1/compute/jobs/{job_id}/model")
COMPUTE_JOB_PREDICTIONS_URL = _e("GET", "/api/v1/compute/jobs/{job_id}/predictions")
COMPUTE_JOB_OUTPUT = _e("GET", "/api/v1/compute/jobs/{job_id}/output/{filename}")
COMPUTE_JOB_CANCEL = _e("DELETE", "/api/v1/compute/jobs/{job_id}")

# -- Account, Keys, Notifications & Security --------------------------------
AGENT_REGISTER = _e("POST", "/api/v1/agents/register", auth=False)
AGENT_PROFILE = _e("GET", "/api/v1/agents/me")
AGENT_RENAME = _e("PATCH", "/api/v1/agents/me")
AGENT_EMAIL_VERIFY = _e("POST", "/api/v1/agents/me/email/verify")
API_KEYS_LIST = _e("GET", "/api/v1/agents/keys")
API_KEYS_CREATE = _e("POST", "/api/v1/agents/keys")
API_KEYS_REVOKE = _e("DELETE", "/api/v1/agents/keys/{key_id}")
NOTIFICATIONS_LIST = _e("GET", "/api/v1/notifications")
NOTIFICATION_READ = _e("POST", "/api/v1/notifications/{notification_id}/read")
NOTIFICATIONS_READ_ALL = _e("POST", "/api/v1/notifications/read-all")
BADGES_LIST = _e("GET", "/api/v1/badges")
MODEL_BADGES = _e("GET", "/api/v1/models/{model_id}/badges")
SECURITY_FREEZE = _e("POST", "/api/v1/account/security/freeze")

# -- Staking, Vaults & Event Staking ----------------------------------------
STAKING_STAKE = _e("POST", "/api/v1/staking/stake")
STAKING_CONFIRM = _e("POST", "/api/v1/staking/confirm")
STAKING_UNSTAKE = _e("POST", "/api/v1/staking/unstake/{stake_id}")
STAKING_BALANCE = _e("GET", "/api/v1/staking/balance/{model_id}")
STAKING_CLAIM = _e("POST", "/api/v1/staking/claim")
STAKING_HISTORY = _e("GET", "/api/v1/staking/history/{model_id}")
STAKING_DEPOSIT_ADDRESS = _e("GET", "/api/v1/staking/deposit-address")
STAKING_RELAY_STAKE = _e("POST", "/api/v1/staking/relay-stake")
STAKING_RELAY_CLAIM = _e("POST", "/api/v1/staking/relay-claim")
STAKING_WITHDRAW = _e("POST", "/api/v1/staking/withdraw")
STAKING_FORWARDER_BALANCE = _e("GET", "/api/v1/staking/forwarder-balance")
STAKING_VAULT = _e("GET", "/api/v1/staking/vault")
STAKING_VAULT_CONFIG_TX = _e("POST", "/api/v1/staking/vault/config-tx")
STAKING_WALLETS = _e("GET", "/api/v1/staking/wallets")
EVENT_STAKING_SUMMARY = _e("GET", "/api/v1/event/staking/summary")
EVENT_STAKING_ALLOC_SET = _e("PUT", "/api/v1/event/staking/allocations/{model}")
EVENT_STAKING_ALLOC_DEL = _e("DELETE", "/api/v1/event/staking/allocations/{model}")

ALL_ENDPOINTS: tuple[Endpoint, ...] = tuple(
    value for value in list(globals().values()) if isinstance(value, Endpoint)
)


def find_endpoint(method: str, path: str) -> Endpoint | None:
    """Resolve a concrete HTTP method + path to its Endpoint definition, preferring exact paths."""
    candidates = [e for e in ALL_ENDPOINTS if e.method == method and e.pattern.match(path)]
    candidates.sort(key=lambda e: e.path.count("{"))
    return candidates[0] if candidates else None
