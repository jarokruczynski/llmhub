from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

from .providers_catalog import quota_scope_default

Kind = Literal[
    "quota",
    "retry",
    "auth",
    "error",
    "too_large",
    "truncated",
    "unsupported_param",
    "not_found",
    "unavailable",
]


Scope = Literal["hourly", "daily", "monthly", "allowance"]
SCOPE_NAMES = ("hourly", "daily", "monthly", "allowance")

SCOPE_MARKERS: tuple[tuple[Scope, tuple[str, ...]], ...] = (
    ("hourly", ("per hour", "hourly", "an hour", "each hour")),
    # "(tpd)" / "(rpd)": groq names the window only in the acronym on some bodies
    (
        "daily",
        ("per day", "daily", "a day", "each day", "resets at 00:00", "resets 00:00", "(tpd)", "(rpd)"),
    ),
    ("monthly", ("per month", "monthly", "a month", "each month")),
    (
        "allowance",
        (
            "quota has been exhausted",
            "freetieronly",
            "insufficient balance",
            "api key balance",
            "credit balance is too low",
            "out of credits",
        ),
    ),
)


@dataclass(frozen=True)
class QuotaRule:
    name: str
    provider: str | None = None
    codes: tuple[str, ...] = ()
    texts: tuple[str, ...] = ()
    scope: Scope | None = None


QUOTA_RULES: tuple[QuotaRule, ...] = (
    QuotaRule(
        name="openai-insufficient-quota",
        codes=("insufficient_quota", "insufficient_user_quota"),
        texts=("insufficient_quota", "exceeded your current quota"),
    ),
    QuotaRule(
        name="explabs-insufficient-quota",
        provider="explabs",
        codes=("insufficient_quota", "free_limit_reached"),
        texts=("insufficient_quota", "free_limit_reached", "hit the free limit"),
    ),
    QuotaRule(
        name="dashscope-free-quota",
        provider="dashscope",
        codes=(
            "free_allocated_quota_exceeded",
            "allocated_quota_exceeded",
            "allocationquota.freetieronly",
        ),
        texts=(
            "free quota has been exhausted",
            "free allocated quota exceeded",
            "allocated quota exceeded",
            "allocationquota.freetieronly",
        ),
        scope="allowance",
    ),
    QuotaRule(
        name="zai-1113",
        provider="zai",
        codes=("1113",),
        texts=("api key balance", "insufficient balance"),
        scope="allowance",
    ),
    QuotaRule(
        name="anthropic-credit-balance",
        provider="anthropic",
        codes=("credit_balance_too_low",),
        texts=("credit balance is too low",),
        scope="allowance",
    ),
    QuotaRule(
        name="antigravity-quota",
        provider="antigravity",
        texts=(
            "quota",
            "rate limit",
            "rate-limit",
            "limit reached",
            "try again later",
            "resource exhausted",
            "resource_exhausted",
        ),
    ),
    # Copilot bills in AI credits (legacy accounts: premium requests) and both renew with the
    # billing month, so the template's monthly quota_scope_default decides the window.
    QuotaRule(
        name="copilot-credits",
        provider="copilot",
        texts=(
            "ai credits",
            "premium request",
            "quota",
            "rate limit",
            "rate-limit",
            "usage limit",
            "exceeded",
        ),
    ),
    QuotaRule(
        name="generic-quota",
        texts=(
            "quota exceeded",
            "quota has been exhausted",
            "out of credits",
            "billing hard limit",
        ),
    ),
)


@dataclass(frozen=True)
class CodeRule:
    name: str
    kind: Kind
    provider: str | None = None
    codes: tuple[str, ...] = ()
    texts: tuple[str, ...] = ()


CODE_RULES: tuple[CodeRule, ...] = (
    CodeRule(name="zai-1305-overloaded", kind="retry", provider="zai", codes=("1305",)),
    CodeRule(name="zai-1210-bad-request", kind="error", provider="zai", codes=("1210",)),
    # the agent CLI is logged in interactively, so a lost session is the owner's to fix:
    # no key to rotate, no point retrying, and the event says to run the CLI and sign in
    CodeRule(
        name="antigravity-signed-out",
        kind="auth",
        provider="antigravity",
        texts=("sign in", "signed in", "sign-in", "not authenticated", "auth", "login", "log in"),
    ),
    CodeRule(
        name="copilot-signed-out",
        kind="auth",
        provider="copilot",
        texts=(
            "not logged in",
            "sign in",
            "authentication",
            "no valid credential",
            "copilot login",
        ),
    ),
    # a licence not assigned to this seat (or since revoked) is not a lost session: retrying
    # or signing in again changes nothing, so it stays an auth error with its own hint
    CodeRule(
        name="copilot-not-entitled",
        kind="auth",
        provider="copilot",
        texts=("not entitled", "no copilot subscription", "access denied"),
    ),
)

RETRY_STATUS = (408, 409, 425, 429, 500, 502, 503, 504, 529)
AUTH_STATUS = (401, 403)

# A route this account cannot use, whatever it does next: the model id is not on this key's
# plan, or it is a paid route reached with a free key. Retrying is free traffic burnt for
# nothing, so the pair is parked for days rather than seconds.
NOT_FOUND_STATUS = (402, 404)
NOT_FOUND_CODES = (
    "model_not_found",
    "invalidendpointormodel.notfound",
    "unavailable_route",
    "payment_required",
)
NOT_FOUND_TEXTS = (
    "does not exist or you do not have access",
    "model not found",
    "no such model",
    "unknown model",
)

# The provider's own upstream is down but it answers 4xx while saying so, which would
# otherwise read as "the request is bad". Transient: a short park, then the pair is tried again.
UNAVAILABLE_TEXTS = (
    "model is unavailable",
    "upstream request failed",
    "temporarily unavailable",
    "overloaded",
)

# How long the vendor says to wait. groq puts it in prose ("try again in 27m44.063s"), most
# others in a Retry-After header; both mean the same thing to the router.
RETRY_AFTER_PHRASES: tuple[re.Pattern[str], ...] = (
    re.compile(r"try again in\s+(.{1,40})", re.IGNORECASE),
    re.compile(r"retry after\s+(.{1,40})", re.IGNORECASE),
    re.compile(r"retry in\s+(.{1,40})", re.IGNORECASE),
    re.compile(r"[\"']?retry[_-]after[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)", re.IGNORECASE),
    # google puts it in the error details as a RetryInfo `retryDelay: "9.026s"`
    re.compile(r"[\"']?retry[_-]?delay[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?\s*[a-z]*)", re.IGNORECASE),
)

# Anchored at the start of the phrase so a later number in the same sentence cannot leak in.
# No \b after a unit: "27m44s" has no word boundary between "m" and "4".
DURATION_RE = re.compile(
    r"^\s*(?:(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?(?![a-z]))?"
    r"\s*(?:(\d+(?:\.\d+)?)\s*m(?:in(?:ute)?s?)?(?![a-z]))?"
    r"\s*(?:(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?(?![a-z]))?",
    re.IGNORECASE,
)
BARE_SECONDS_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?![\w.])")


def parse_duration(text: str) -> float | None:
    """Seconds from a vendor phrase: 27m44.063999999s, 1.234s, 30 seconds, 5 minutes, 30."""
    match = DURATION_RE.match(text)
    if match and any(match.groups()):
        hours, minutes, seconds = (float(value) if value else 0.0 for value in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    bare = BARE_SECONDS_RE.match(text)
    return float(bare.group(1)) if bare else None


def retry_after_from(text: str, headers: Any = None, now: datetime | None = None) -> float | None:
    """Seconds the vendor asked us to wait, from the body or a Retry-After header. None = unsaid."""
    for pattern in RETRY_AFTER_PHRASES:
        match = pattern.search(text)
        if not match:
            continue
        value = parse_duration(match.group(1))
        if value is not None and value > 0:
            return value
    header = None
    if headers is not None:
        try:
            header = headers.get("retry-after") or headers.get("Retry-After")
        except AttributeError:
            header = None
    if not header:
        return None
    header = str(header).strip()
    try:
        return max(0.0, float(header))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = (when - (now or datetime.now(UTC))).total_seconds()
    return delta if delta > 0 else None


# The number that matters is the vendor's ceiling, never what we sent: a groq TPM body carries
# both ("Limit 8000, Requested 11152") and the "Limit" word always comes first, so it wins the
# search; an OpenAI-shaped context-length body only ever states the one number.
REQUEST_CAP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\blimit\s+(\d+)", re.IGNORECASE),
    re.compile(r"maximum context length is (\d+)", re.IGNORECASE),
)


def request_cap_from(text: str | bytes | dict[str, Any] | None) -> int | None:
    """Pull a numeric request-size ceiling out of a vendor error body, or None when it names none."""
    body_text = _text_of(text)
    for pattern in REQUEST_CAP_PATTERNS:
        match = pattern.search(body_text)
        if match:
            return int(match.group(1))
    return None


# Which end of the request the ceiling is on. groq caps output tokens per minute on some
# routes ("on output tokens per minute (OTPM): Limit 1000") - the same "Limit N" shape as an
# input cap, but a number the caller controls with max_tokens instead of with the prompt.
REQUEST_CAP_OUT_MARKERS = ("output tokens", "otpm", "completion tokens", "output token")


def request_cap_axis(text: str | bytes | dict[str, Any] | None) -> str:
    """'out' when the body names an output-side ceiling, 'in' otherwise (the common case)."""
    lowered = _text_of(text).lower()
    return "out" if any(marker in lowered for marker in REQUEST_CAP_OUT_MARKERS) else "in"


# A sampling/decoding param a vendor route pins to one value (explabs routes some models to
# temperature=1.0 only) reads as a plain 400 with no code of its own, just prose naming the
# field. Catch the shapes seen so far; a caller (the router) strips the field and retries once
# rather than treating this like an ordinary bad request.
UNSUPPORTED_PARAM_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"value .* for '(\w+)' is not supported", re.IGNORECASE),
    re.compile(r"unsupported parameter:\s*['\"]?(\w+)", re.IGNORECASE),
    re.compile(r"'(\w+)' is not supported", re.IGNORECASE),
)


def unsupported_param_from(payload: dict[str, Any], text: str) -> str | None:
    """The param name a 400 body says this vendor route will not accept, or None."""
    error = payload.get("error")
    if isinstance(error, dict):
        param = error.get("param")
        if isinstance(param, str) and param:
            return param
    param = payload.get("param")
    if isinstance(param, str) and param:
        return param
    for pattern in UNSUPPORTED_PARAM_TEXT_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


@dataclass
class Classification:
    kind: Kind
    code: str | None = None
    message: str = ""
    rule: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    scope: Scope | None = None
    # seconds the vendor asked us to wait before trying this pair again; None = it said nothing
    retry_after_s: float | None = None
    # what the body said it counts: metric, window, the vendor's own limit and its used count.
    # None = the body named nothing; any field can still be None on its own.
    quota_detail: dict[str, Any] | None = None

    @property
    def is_quota(self) -> bool:
        return self.kind == "quota"

    @property
    def retryable(self) -> bool:
        return self.kind == "retry"


def _unwrap_errors_key(payload: dict[str, Any]) -> dict[str, Any]:
    """`{"errors": [{...}]}` instead of `{"error": {...}}` - cheap to fold into the usual shape."""
    if "error" not in payload:
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return {**payload, "error": errors[0]}
    return payload


def _payload_of(body: str | bytes | dict[str, Any] | None) -> dict[str, Any]:
    if body is None:
        return {}
    if isinstance(body, dict):
        return _unwrap_errors_key(body)
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return {}
    if isinstance(parsed, dict):
        return _unwrap_errors_key(parsed)
    # gemini's OpenAI-compatible endpoint answers a 429 with a JSON list holding one error
    # object instead of a dict, so unwrap it the same way everywhere else expects.
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        return parsed[0]
    return {}


def _text_of(body: str | bytes | dict[str, Any] | None) -> str:
    if body is None:
        return ""
    if isinstance(body, dict):
        return json.dumps(body)
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return body


def extract_code(payload: dict[str, Any]) -> str | None:
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("code", "type", "status"):
            value = error.get(key)
            if value not in (None, ""):
                return str(value)
    for key in ("code", "error_code"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def extract_message(payload: dict[str, Any], fallback: str = "") -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if message:
            return str(message)
    if isinstance(error, str) and error:
        return error
    for key in ("message", "msg", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback[:500]


def detect_scope(text: str) -> Scope | None:
    lowered = text.lower()
    for scope, needles in SCOPE_MARKERS:
        if any(needle in lowered for needle in needles):
            return scope
    return None


# What a quota body actually said it was counting. The window is the vendor's own unit, so it
# has a `minute` the hub keeps no books on: a per-minute cap is pacing, not a spent budget.
QuotaMetric = Literal["requests", "in_tokens", "out_tokens", "total_tokens"]
QUOTA_METRICS = ("requests", "in_tokens", "out_tokens", "total_tokens")
QUOTA_WINDOWS = ("minute", "hourly", "daily", "monthly")

WINDOW_BY_UNIT = {"minute": "minute", "hour": "hourly", "day": "daily", "month": "monthly"}
MINUTE_MARKERS = ("per minute", "a minute", "each minute", "per-minute", "(tpm)", "(rpm)", "(otpm)", "(itpm)")

# groq states the whole truth in one line: "on tokens per day (TPD): Limit 200000, Used 195998".
# The words before "per" name the metric, the unit names the window, and Limit is the vendor's
# own ceiling - the number to learn, as opposed to Used, which is only where we got to.
GROQ_LIMIT_RE = re.compile(
    r"on\s+(?P<words>[a-z ]{1,40}?)\s+per\s+(?P<unit>minute|hour|day|month)\s*"
    r"(?:\((?P<acronym>[a-z]+)\))?\s*:\s*limit\s+(?P<limit>\d+)"
    r"(?:\s*,\s*used\s+(?P<used>\d+))?",
    re.IGNORECASE,
)

# gemini's prose names the metric and the ceiling but never the window; its `details` do.
GEMINI_METRIC_RE = re.compile(r"metric:\s*(?P<metric>[\w./-]+)[,\s]+limit:\s*(?P<limit>\d+)", re.IGNORECASE)
GENERIC_LIMIT_RE = re.compile(r"\blimit[:\s]\s*(\d+)", re.IGNORECASE)


def _metric_from_words(words: str) -> QuotaMetric | None:
    """Metric a vendor's own wording names. Handles prose, quotaIds and metric paths alike."""
    text = re.sub(r"[^a-z]+", " ", words.lower())
    if "input token" in text or "prompt token" in text:
        return "in_tokens"
    if "output token" in text or "completion token" in text:
        return "out_tokens"
    if "request" in text:
        return "requests"
    if "token" in text:
        return "total_tokens"
    return None


ACRONYM_METRIC = {"t": "total_tokens", "r": "requests", "ot": "out_tokens", "it": "in_tokens"}
ACRONYM_WINDOW = {"m": "minute", "h": "hourly", "d": "daily"}


def _metric_from_acronym(acronym: str) -> tuple[QuotaMetric | None, str | None]:
    """TPD/RPM/OTPM/ITPM: everything before the P is the metric, the last letter the window."""
    match = re.fullmatch(r"([a-z]{1,2})p([a-z])", acronym.lower())
    if not match:
        return None, None
    metric = ACRONYM_METRIC.get(match.group(1))
    return metric, ACRONYM_WINDOW.get(match.group(2))  # type: ignore[return-value]


def _as_int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _quota_violations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The `details[].violations[]` google puts under a RESOURCE_EXHAUSTED error."""
    error = payload.get("error")
    root = error if isinstance(error, dict) else payload
    details = root.get("details")
    out: list[dict[str, Any]] = []
    if isinstance(details, list):
        for item in details:
            if not isinstance(item, dict):
                continue
            violations = item.get("violations")
            if isinstance(violations, list):
                out.extend(entry for entry in violations if isinstance(entry, dict))
    return out


def _detail(
    metric: QuotaMetric | None = None,
    window: str | None = None,
    limit: int | None = None,
    used: int | None = None,
) -> dict[str, Any] | None:
    if metric is None and window is None and limit is None and used is None:
        return None
    return {"metric": metric, "window": window, "limit": limit, "used": used}


def quota_detail_from(
    body: str | bytes | dict[str, Any] | None, payload: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """What the vendor said it counts, how far it counts it, and how much it allows.

    Four sources, most specific first: groq's one-line shape, google's structured violations,
    google's prose, and finally the window markers plus a bare "Limit N". Every field is
    optional - a body that only names a window still says which bucket is full.
    """
    text = _text_of(body)
    payload = _payload_of(body) if payload is None else payload

    match = GROQ_LIMIT_RE.search(text)
    if match:
        metric = _metric_from_words(match.group("words"))
        window = WINDOW_BY_UNIT.get(match.group("unit").lower())
        acronym_metric, acronym_window = _metric_from_acronym(match.group("acronym") or "")
        return _detail(
            metric or acronym_metric,
            window or acronym_window,
            _as_int(match.group("limit")),
            _as_int(match.group("used")),
        )

    for violation in _quota_violations(payload):
        quota_id = str(violation.get("quotaId") or "")
        metric = _metric_from_words(f"{quota_id} {violation.get('quotaMetric') or ''}")
        window = next((name for name in QUOTA_WINDOWS if _window_word(name) in quota_id.lower()), None)
        detail = _detail(metric, window, _as_int(violation.get("quotaValue")))
        if detail is not None:
            return detail

    prose = GEMINI_METRIC_RE.search(text)
    if prose:
        # no window here on purpose: the prose names none, so the scope falls to the markers
        # and then to the provider template, exactly as it did before
        return _detail(_metric_from_words(prose.group("metric")), None, _as_int(prose.group("limit")))

    lowered = text.lower()
    window = "minute" if any(marker in lowered for marker in MINUTE_MARKERS) else None
    if window is None:
        scope = detect_scope(text)
        window = scope if scope in ("hourly", "daily", "monthly") else None
    generic = GENERIC_LIMIT_RE.search(text)
    limit = _as_int(generic.group(1)) if generic else None
    metric = "total_tokens" if limit is not None and "token" in lowered else None
    return _detail(metric, window, limit if metric else None)


def _window_word(window: str) -> str:
    """`hourly` -> `perhour`, the way a google quotaId spells the same window."""
    return "per" + {"minute": "minute", "hourly": "hour", "daily": "day", "monthly": "month"}[window]


def default_scope(provider: str | None) -> Scope | None:
    """The catalog's `quota_scope_default`: last word when the body names no window."""
    scope = quota_scope_default(provider)
    return scope if scope in SCOPE_NAMES else None  # type: ignore[return-value]


def classify(
    status_code: int | None,
    body: str | bytes | dict[str, Any] | None = None,
    provider: str | None = None,
    headers: Any = None,
) -> Classification:
    result = _classify(status_code, body, provider)
    if result.retry_after_s is None:
        result.retry_after_s = retry_after_from(_text_of(body), headers)
    return result


def _classify(
    status_code: int | None,
    body: str | bytes | dict[str, Any] | None = None,
    provider: str | None = None,
) -> Classification:
    payload = _payload_of(body)
    raw_text = _text_of(body)
    text = raw_text.lower()
    code = extract_code(payload)
    message = extract_message(payload, raw_text)
    code_l = (code or "").lower()

    # too large is not a quota error and never retryable on the same candidate: a 413 always
    # means this, a 400 when the body actually names a size ceiling (a plain bad request must
    # still fall through to "error" below), and a 429 only when it says so in words - a plain
    # rate-limit body carries a "Limit N" of its own that has nothing to do with request size.
    request_cap = request_cap_from(raw_text)
    named_too_large = "request too large" in text
    if (
        status_code == 413
        or (status_code == 400 and request_cap is not None)
        or (status_code == 429 and named_too_large)
    ):
        detail: dict[str, Any] = {}
        if request_cap is not None:
            axis = "max_out_tokens" if request_cap_axis(raw_text) == "out" else "max_request_tokens"
            detail = {axis: request_cap}
        return Classification("too_large", code, message, "too_large", detail=detail)

    # same shape of trap as too_large: a 400 that names one rejected param is not a generic
    # bad request, it is a fact about this vendor route worth learning and never repeating
    if status_code == 400:
        param = unsupported_param_from(payload, raw_text)
        if param:
            return Classification(
                "unsupported_param", code, message, "unsupported_param", detail={"param": param}
            )

    # What the body itself says it counts. It decides the window before any marker does: a
    # vendor that names its own bucket is not guessing, and the prose markers and the template
    # default exist only for the vendors that name nothing.
    detail = quota_detail_from(raw_text, payload)
    detail_window = (detail or {}).get("window")
    detail_scope = detail_window if detail_window in ("hourly", "daily", "monthly") else None

    for rule in QUOTA_RULES:
        if rule.provider is not None and rule.provider != provider:
            continue
        matched = bool(code_l and code_l in tuple(c.lower() for c in rule.codes))
        if not matched:
            matched = any(needle in text for needle in rule.texts)
        if matched:
            # a per-minute cap is the vendor pacing this second, not a budget that is spent:
            # nothing to park and nothing to learn, so it goes back as a retry with the delay
            if detail_window == "minute":
                return Classification("retry", code, message, "quota-pacing", quota_detail=detail)
            scope = detail_scope or detect_scope(text) or rule.scope or default_scope(provider)
            return Classification("quota", code, message, rule.name, scope=scope, quota_detail=detail)

    # groq-style "Rate limit reached for model ... on tokens per day (TPD): Limit 200000".
    # A day or a month is a window the hub tracks, so the pair is parked until it resets; a
    # per-minute burst limit names no window the hub keeps books on and stays a retry.
    if "rate limit reached" in text or code_l == "rate_limit_exceeded":
        window = detail_scope or detect_scope(text)
        if window in ("hourly", "daily", "monthly"):
            return Classification(
                "quota", code, message, "rate-limit-window", scope=window, quota_detail=detail
            )

    for code_rule in CODE_RULES:
        if code_rule.provider is not None and code_rule.provider != provider:
            continue
        matched = bool(code_l and code_l in tuple(item.lower() for item in code_rule.codes))
        if not matched:
            matched = any(needle in text for needle in code_rule.texts)
        if matched:
            return Classification(code_rule.kind, code, message, code_rule.name)

    # A route this key cannot use at all, versus the provider's own upstream being down while
    # it answers 4xx. Both are facts about the pair, neither is a fact about the request, so
    # neither may reach the client - they park the pair and the run moves on. After the code
    # rules: a provider that has its own word for "overloaded" keeps it.
    if status_code in NOT_FOUND_STATUS or code_l in NOT_FOUND_CODES:
        return Classification("not_found", code or str(status_code), message, "not_found")
    if any(needle in text for needle in NOT_FOUND_TEXTS):
        return Classification("not_found", code or str(status_code or ""), message, "not_found")
    # 429 and 408 are the vendor pacing or timing out the caller, not its own upstream being
    # down, so they are excluded here and fall through to the retry/quota handling below.
    if status_code is not None and 400 <= status_code < 500 and status_code not in (429, 408):
        error = payload.get("error")
        server_error = isinstance(error, dict) and error.get("type") == "server_error"
        if server_error or any(needle in text for needle in UNAVAILABLE_TEXTS):
            return Classification("unavailable", code or str(status_code), message, "unavailable")

    if status_code is None:
        return Classification("retry", code or "transport", message or "transport error")
    if status_code in AUTH_STATUS:
        return Classification("auth", code or str(status_code), message)
    if status_code in RETRY_STATUS or status_code >= 500:
        return Classification("retry", code or str(status_code), message, quota_detail=detail)
    if status_code >= 400:
        return Classification("error", code or str(status_code), message)
    return Classification("error", code, message)
