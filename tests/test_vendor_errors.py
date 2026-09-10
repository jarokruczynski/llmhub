from __future__ import annotations

import json

import pytest

from llmhub.vendor_errors import classify, request_cap_from


def test_explabs_insufficient_quota_code() -> None:
    body = json.dumps({"error": {"message": "You exceeded your quota", "code": "insufficient_quota"}})
    result = classify(429, body, "explabs")
    assert result.kind == "quota"
    assert result.code == "insufficient_quota"


def test_dashscope_free_quota_text() -> None:
    body = json.dumps({"code": "Arrearage", "message": "Access denied, free quota has been exhausted"})
    result = classify(400, body, "dashscope")
    assert result.kind == "quota"


def test_zai_code_1113() -> None:
    body = json.dumps({"error": {"code": "1113", "message": "insufficient balance"}})
    assert classify(200, body, "zai").kind == "quota"


def test_anthropic_credit_balance() -> None:
    body = json.dumps(
        {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "Your credit balance is too low"},
        }
    )
    assert classify(400, body, "anthropic").kind == "quota"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 529])
def test_plain_transient_statuses_are_retry(status: int) -> None:
    result = classify(status, json.dumps({"error": {"message": "rate limit"}}), "alpha")
    assert result.kind == "retry"


def test_transport_error_is_retry() -> None:
    assert classify(None, "connection refused", "alpha").kind == "retry"


def test_auth_status_is_auth() -> None:
    assert classify(401, "{}", "alpha").kind == "auth"


def test_bad_request_is_error() -> None:
    result = classify(
        400, json.dumps({"error": {"message": "bad model", "code": "invalid_request"}}), "alpha"
    )
    assert result.kind == "error"
    assert result.code == "invalid_request"


def test_provider_scoped_rule_does_not_leak() -> None:
    body = json.dumps({"error": {"code": "1113", "message": "something else"}})
    assert classify(400, body, "alpha").kind == "error"


def test_non_json_body_falls_back_to_text_scan() -> None:
    assert classify(429, "free quota has been exhausted", "dashscope").kind == "quota"
    assert classify(500, "<html>gateway error</html>", "alpha").kind == "retry"


def test_explabs_free_limit_reached_is_quota() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "You've hit the free limit for GPT-6 Astra free daily tier "
                "(375,000 input / 75,000 output tokens per day). It resets at 00:00 UTC.",
                "type": "insufficient_quota",
                "param": None,
                "code": "free_limit_reached",
            }
        }
    )
    result = classify(429, body, "explabs")
    assert result.kind == "quota"
    assert result.code == "free_limit_reached"


def test_dashscope_free_tier_only_403_is_quota_not_auth() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "The free quota has been exhausted. To continue accessing the "
                "model on a paid basis, please complete your payment information.",
                "type": "AllocationQuota.FreeTierOnly",
                "param": None,
                "code": "AllocationQuota.FreeTierOnly",
            }
        }
    )
    result = classify(403, body, "dashscope")
    assert result.kind == "quota"
    assert result.code == "AllocationQuota.FreeTierOnly"


def test_dashscope_free_tier_only_code_without_matching_text() -> None:
    body = json.dumps({"error": {"code": "AllocationQuota.FreeTierOnly", "message": "denied"}})
    assert classify(403, body, "dashscope").kind == "quota"


def test_zai_1305_is_transient_retry() -> None:
    body = json.dumps({"error": {"code": "1305", "message": "The service may be temporarily overloaded"}})
    result = classify(429, body, "zai")
    assert result.kind == "retry"
    assert result.rule == "zai-1305-overloaded"


def test_zai_1210_is_client_error_no_retry() -> None:
    body = json.dumps({"error": {"code": "1210", "message": "API call parameter error"}})
    result = classify(429, body, "zai")
    assert result.kind == "error"
    assert result.retryable is False


def test_zai_code_rules_do_not_leak_to_other_providers() -> None:
    # "overloaded" would be read as a provider-side outage for anyone, so the message here has
    # to be neutral for the test to be about the code rule at all
    body = json.dumps({"error": {"code": "1305", "message": "something else entirely"}})
    assert classify(400, body, "dashscope").kind == "error"


def test_explabs_free_limit_message_hints_daily_scope() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "You've hit the free limit for GPT-6 Astra free daily tier "
                "(375,000 input / 75,000 output tokens per day). It resets at 00:00 UTC.",
                "type": "insufficient_quota",
                "code": "free_limit_reached",
            }
        }
    )
    assert classify(429, body, "explabs").scope == "daily"


def test_zai_1113_hints_allowance_scope() -> None:
    body = json.dumps({"error": {"code": "1113", "message": "insufficient balance"}})
    assert classify(200, body, "zai").scope == "allowance"


def test_dashscope_free_tier_only_hints_allowance_scope() -> None:
    body = json.dumps({"error": {"code": "AllocationQuota.FreeTierOnly", "message": "denied"}})
    assert classify(403, body, "dashscope").scope == "allowance"


def test_hourly_and_monthly_scope_hints() -> None:
    hourly = json.dumps({"error": {"message": "Quota exceeded: 10 requests per hour"}})
    assert classify(429, hourly, "alpha").scope == "hourly"
    monthly = json.dumps({"error": {"message": "Quota exceeded for this month, monthly cap"}})
    assert classify(429, monthly, "alpha").scope == "monthly"


def test_quota_without_window_hint_has_no_scope() -> None:
    body = json.dumps({"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}})
    result = classify(429, body, "explabs")
    assert result.kind == "quota"
    assert result.scope is None


def test_non_quota_classification_has_no_scope() -> None:
    assert classify(500, json.dumps({"error": {"message": "daily boom"}}), "alpha").scope is None


GEMINI_429 = (
    '{"error": {"code": 429, "message": "You exceeded your current quota, please check your plan '
    'and billing details.", "status": "RESOURCE_EXHAUSTED"}}'
)


def test_gemini_scopeless_quota_defaults_to_hourly() -> None:
    result = classify(429, GEMINI_429, "gemini")
    assert result.kind == "quota"
    assert result.scope == "hourly"


def test_scopeless_quota_stays_scopeless_without_a_template_default() -> None:
    assert classify(429, GEMINI_429, "openrouter").scope is None
    assert classify(429, GEMINI_429, None).scope is None


def test_text_scope_beats_the_template_default() -> None:
    body = '{"error": {"message": "quota exceeded: 1000 requests per day", "code": 429}}'
    assert classify(429, body, "gemini").scope == "daily"


def test_template_default_only_applies_to_quota_errors() -> None:
    assert classify(500, '{"error": {"message": "boom"}}', "gemini").scope is None


GROQ_TPM_BODY = (
    '{"error": {"message": "Request too large for model `openai/gpt-oss-120b` in organization '
    "`org_...` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Requested 11152"
    '"}}'
)


def test_request_cap_from_groq_tpm_body() -> None:
    assert request_cap_from(GROQ_TPM_BODY) == 8000


def test_request_cap_from_maximum_context_length() -> None:
    body = json.dumps({"error": {"message": "maximum context length is 8192 tokens"}})
    assert request_cap_from(body) == 8192


def test_request_cap_from_context_length_with_requested_count() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "This model's maximum context length is 16385 tokens, however you requested 20000"
            }
        }
    )
    assert request_cap_from(body) == 16385


def test_request_cap_from_body_with_no_number_is_none() -> None:
    body = json.dumps({"error": {"message": "bad request, nothing about size here"}})
    assert request_cap_from(body) is None


def test_413_is_classified_too_large_not_quota() -> None:
    result = classify(413, GROQ_TPM_BODY, "groq")
    assert result.kind == "too_large"
    assert result.is_quota is False
    assert result.retryable is False
    assert result.detail["max_request_tokens"] == 8000


def test_413_without_a_parseable_number_is_still_too_large() -> None:
    body = json.dumps({"error": {"message": "request too large"}})
    result = classify(413, body, "alpha")
    assert result.kind == "too_large"
    assert result.detail == {}


def test_400_with_context_length_body_is_too_large() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "This model's maximum context length is 16385 tokens, however you requested 20000"
            }
        }
    )
    result = classify(400, body, "alpha")
    assert result.kind == "too_large"
    assert result.detail["max_request_tokens"] == 16385


def test_400_without_a_size_shape_stays_a_plain_error() -> None:
    result = classify(400, json.dumps({"error": {"message": "bad model"}}), "alpha")
    assert result.kind == "error"


def test_unsupported_param_from_json_param_field() -> None:
    body = json.dumps({"error": {"message": "bad request", "param": "temperature"}})
    result = classify(400, body, "explabs")
    assert result.kind == "unsupported_param"
    assert result.detail["param"] == "temperature"


def test_unsupported_param_from_value_for_is_not_supported_text() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "The value 0.0 for 'temperature' is not supported by this model "
                "route. Supported values are between 1.0 and 1.0."
            }
        }
    )
    result = classify(400, body, "zai")
    assert result.kind == "unsupported_param"
    assert result.detail["param"] == "temperature"


def test_unsupported_param_from_unsupported_parameter_text() -> None:
    body = json.dumps({"error": {"message": "Unsupported parameter: top_p"}})
    result = classify(400, body, "alpha")
    assert result.kind == "unsupported_param"
    assert result.detail["param"] == "top_p"


def test_unsupported_param_from_bare_is_not_supported_text() -> None:
    body = json.dumps({"error": {"message": "'presence_penalty' is not supported on this model"}})
    result = classify(400, body, "alpha")
    assert result.kind == "unsupported_param"
    assert result.detail["param"] == "presence_penalty"


def test_explabs_failed_to_generate_json_stays_a_plain_error() -> None:
    body = json.dumps(
        {"error": {"message": "Failed to generate JSON. Please adjust your prompt. See 'failed_generation'."}}
    )
    result = classify(400, body, "explabs")
    assert result.kind == "error"


# --- router failure handling ---------------------------------------------------------------

GROQ_TPD_BODY = json.dumps(
    {
        "error": {
            "message": "Rate limit reached for model `llama-3.3-70b` in organization `org_x` "
            "service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 195998, "
            "Requested 7854. Please try again in 27m44.063999999s.",
            "type": "tokens",
            "code": "rate_limit_exceeded",
        }
    }
)

GROQ_OTPM_BODY = json.dumps(
    {
        "error": {
            "message": "Request too large for model `llama-3.3-70b` in organization `org_x` "
            "service tier `on_demand` on output tokens per minute (OTPM): Limit 1000, "
            "Requested 1816. Please try again in 1.234s.",
            "type": "tokens",
            "code": "rate_limit_exceeded",
        }
    }
)

GROQ_MODEL_NOT_FOUND = json.dumps(
    {
        "error": {
            "message": "The model `qwen/qwen3-32b` does not exist or you do not have access to it.",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }
)

ZEN_UNAVAILABLE = json.dumps(
    {
        "error": {
            "type": "server_error",
            "message": "Error from provider (Console): Upstream request failed: Model is unavailable.",
        }
    }
)


def test_groq_404_model_not_found_is_not_found() -> None:
    result = classify(404, GROQ_MODEL_NOT_FOUND, "groq")
    assert result.kind == "not_found"
    assert result.code == "model_not_found"


def test_not_found_code_without_a_404_status() -> None:
    body = json.dumps({"error": {"code": "InvalidEndpointOrModel.NotFound", "message": "nope"}})
    assert classify(400, body, "dashscope").kind == "not_found"


def test_not_found_from_text_alone() -> None:
    body = json.dumps({"error": {"message": "no such model on this account"}})
    assert classify(400, body, "alpha").kind == "not_found"


def test_payment_required_is_not_found() -> None:
    body = json.dumps({"error": {"message": "this route needs a paid plan"}})
    result = classify(402, body, "alpha")
    assert result.kind == "not_found"


def test_opencode_zen_server_error_on_a_400_is_unavailable() -> None:
    result = classify(400, ZEN_UNAVAILABLE, "opencode-zen")
    assert result.kind == "unavailable"
    assert "Model is unavailable" in result.message


def test_unavailable_text_on_a_4xx_without_the_server_error_type() -> None:
    body = json.dumps({"error": {"message": "The model is temporarily unavailable, try later"}})
    assert classify(400, body, "alpha").kind == "unavailable"


def test_a_5xx_stays_retry_not_unavailable() -> None:
    body = json.dumps({"error": {"type": "server_error", "message": "model is unavailable"}})
    assert classify(503, body, "alpha").kind == "retry"


def test_groq_429_overloaded_stays_retry_not_unavailable() -> None:
    body = '{"error":{"message":"The server is overloaded, try again in 2s"}}'
    result = classify(429, body, "groq")
    assert result.kind == "retry"
    assert result.retry_after_s == 2.0


def test_groq_tpd_rate_limit_is_a_daily_quota() -> None:
    result = classify(429, GROQ_TPD_BODY, "groq")
    assert result.kind == "quota"
    assert result.scope == "daily"
    assert result.rule == "rate-limit-window"


def test_the_daily_rate_limit_rule_is_not_groq_only() -> None:
    body = json.dumps({"error": {"message": "Rate limit reached on requests per day (RPD): Limit 50"}})
    assert classify(429, body, "somevendor").scope == "daily"


def test_groq_tpm_rate_limit_stays_a_retry_with_a_delay() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "Rate limit reached for model `m` on tokens per minute (TPM): "
                "Limit 6000, Used 5000, Requested 2000. Please try again in 8.5s.",
                "code": "rate_limit_exceeded",
            }
        }
    )
    result = classify(429, body, "groq")
    assert result.kind == "retry"
    assert result.retry_after_s == 8.5


def test_groq_otpm_request_too_large_records_the_output_axis() -> None:
    result = classify(429, GROQ_OTPM_BODY, "groq")
    assert result.kind == "too_large"
    assert result.detail == {"max_out_tokens": 1000}
    assert "max_request_tokens" not in result.detail


def test_tpm_request_too_large_still_uses_the_input_axis() -> None:
    result = classify(413, GROQ_TPM_BODY, "groq")
    assert result.detail == {"max_request_tokens": 8000}


def test_retry_after_from_a_groq_prose_delay() -> None:
    assert classify(429, GROQ_TPD_BODY, "groq").retry_after_s == 27 * 60 + 44.063999999


def test_retry_after_from_a_short_prose_delay() -> None:
    body = json.dumps({"error": {"message": "slow down, please try again in 1.234s"}})
    assert classify(429, body, "alpha").retry_after_s == 1.234


def test_retry_after_from_a_plain_english_delay() -> None:
    body = json.dumps({"error": {"message": "too fast, retry after 30 seconds"}})
    assert classify(429, body, "alpha").retry_after_s == 30.0


def test_retry_after_from_a_header_when_the_body_says_nothing() -> None:
    result = classify(429, json.dumps({"error": {"message": "slow down"}}), "alpha", {"Retry-After": "45"})
    assert result.retry_after_s == 45.0


def test_body_delay_beats_the_header() -> None:
    body = json.dumps({"error": {"message": "please try again in 5s"}})
    assert classify(429, body, "alpha", {"retry-after": "600"}).retry_after_s == 5.0


def test_no_delay_anywhere_is_none() -> None:
    assert classify(429, json.dumps({"error": {"message": "slow down"}}), "alpha").retry_after_s is None


# --- what the body says it counts ----------------------------------------------------------


def gemini_quota_body(quota_id: str, quota_value: str, metric: str, retry: str = "9.026s") -> str:
    return json.dumps(
        {
            "error": {
                "code": 429,
                "message": "You exceeded your current quota, please check your plan and billing "
                f"details.\n* Quota exceeded for metric: {metric}, limit: {quota_value}, model: "
                "gemini-3.8-flash",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {"quotaMetric": metric, "quotaId": quota_id, "quotaValue": quota_value}
                        ],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry},
                ],
            }
        }
    )


GEMINI_RPD = gemini_quota_body(
    "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
    "20",
    "generativelanguage.googleapis.com/generate_content_free_tier_requests",
)

GEMINI_RPM = gemini_quota_body(
    "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
    "10",
    "generativelanguage.googleapis.com/generate_content_free_tier_requests",
)


def test_gemini_per_day_violation_names_the_metric_the_window_and_the_limit() -> None:
    result = classify(429, GEMINI_RPD, "gemini")
    assert result.kind == "quota"
    # the quotaId names the day; without it the gemini template would have said hourly
    assert result.scope == "daily"
    assert result.quota_detail == {"metric": "requests", "window": "daily", "limit": 20, "used": None}


def test_gemini_per_minute_violation_is_pacing_not_exhaustion() -> None:
    result = classify(429, GEMINI_RPM, "gemini")
    assert result.kind == "retry"
    assert result.rule == "quota-pacing"
    assert result.scope is None
    assert result.retry_after_s == 9.026
    assert result.quota_detail["window"] == "minute"


def test_gemini_retry_delay_is_read_from_the_details() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "You exceeded your current quota",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "42s"},
                ],
            }
        }
    )
    assert classify(429, body, "gemini").retry_after_s == 42.0


def test_gemini_prose_metric_without_details_still_names_the_ceiling() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "You exceeded your current quota.\n* Quota exceeded for metric: "
                "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
                "limit: 20, model: gemini-3.8-flash",
                "status": "RESOURCE_EXHAUSTED",
            }
        }
    )
    result = classify(429, body, "gemini")
    assert result.quota_detail == {"metric": "requests", "window": None, "limit": 20, "used": None}
    # the prose names no window, so the gemini template default still decides
    assert result.scope == "hourly"


def test_groq_tpd_detail_carries_the_stated_limit_and_used() -> None:
    result = classify(429, GROQ_TPD_BODY, "groq")
    assert result.quota_detail == {
        "metric": "total_tokens",
        "window": "daily",
        "limit": 200000,
        "used": 195998,
    }


def test_groq_rpd_detail_is_a_request_count() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "Rate limit reached for model `llama-3.3-70b` in organization `org_x` "
                "on requests per day (RPD): Limit 1000, Used 1000, Requested 1. Please try "
                "again in 27m44s.",
                "code": "rate_limit_exceeded",
            }
        }
    )
    result = classify(429, body, "groq")
    assert result.kind == "quota"
    assert result.scope == "daily"
    assert result.quota_detail == {
        "metric": "requests",
        "window": "daily",
        "limit": 1000,
        "used": 1000,
    }


def test_groq_otpm_stays_too_large_and_never_becomes_a_quota_hit() -> None:
    result = classify(429, GROQ_OTPM_BODY, "groq")
    assert result.kind == "too_large"
    assert result.detail == {"max_out_tokens": 1000}


def test_groq_tpm_detail_is_a_minute_window() -> None:
    body = json.dumps(
        {
            "error": {
                "message": "Rate limit reached for model `m` on tokens per minute (TPM): "
                "Limit 6000, Used 5000, Requested 2000. Please try again in 8.5s.",
                "code": "rate_limit_exceeded",
            }
        }
    )
    result = classify(429, body, "groq")
    assert result.kind == "retry"
    assert result.quota_detail == {
        "metric": "total_tokens",
        "window": "minute",
        "limit": 6000,
        "used": 5000,
    }


def test_a_bare_quota_body_names_nothing_to_learn() -> None:
    body = json.dumps({"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}})
    assert classify(429, body, "explabs").quota_detail is None


GEMINI_COMPAT_429 = json.dumps(
    [
        {
            "error": {
                "code": 429,
                "message": (
                    "You exceeded your current quota, please check your plan and billing "
                    "details.\n* Quota exceeded for metric: generativelanguage.googleapis.com/"
                    "generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash\n"
                    "Please retry in 21.459201202s."
                ),
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.Help",
                        "links": [
                            {
                                "description": "Learn more",
                                "url": "https://ai.google.dev/gemini-api/docs/rate-limits",
                            }
                        ],
                    },
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaMetric": "generativelanguage.googleapis.com/"
                                "generate_content_free_tier_requests",
                                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                "quotaDimensions": {"location": "global", "model": "gemini-3.8-flash"},
                                "quotaValue": "20",
                            }
                        ],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "21s"},
                ],
            }
        }
    ]
)


def test_gemini_openai_compat_429_body_is_a_json_list() -> None:
    # the compat endpoint wraps the single error object in a JSON list instead of a dict;
    # _payload_of has to unwrap it or extract_code/extract_message/quota_detail_from all miss.
    # The prose "retry in 21.459201202s" wins over the details' retryDelay "21s" because
    # retry_after_from tries the prose patterns first and returns on the first match - and the
    # prose is more precise here anyway.
    result = classify(429, GEMINI_COMPAT_429, "gemini")
    assert result.kind == "quota"
    assert result.scope == "daily"
    assert result.code == "429"
    assert result.quota_detail == {"metric": "requests", "window": "daily", "limit": 20, "used": None}
    assert result.retry_after_s == 21.459201202


def test_a_generic_token_limit_is_read_as_a_total() -> None:
    body = json.dumps({"error": {"message": "Quota exceeded, monthly token Limit 500000"}})
    result = classify(429, body, "somevendor")
    assert result.scope == "monthly"
    assert result.quota_detail == {
        "metric": "total_tokens",
        "window": "monthly",
        "limit": 500000,
        "used": None,
    }
