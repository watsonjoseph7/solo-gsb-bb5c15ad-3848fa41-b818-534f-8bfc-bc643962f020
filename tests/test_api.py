"""API-level tests via FastAPI TestClient."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SUPPORTED = ["en-US", "zh-CN", "zh-TW", "ja-JP"]
DEFAULT = "en-US"


def _post(payload):
    return client.post("/api/v1/negotiate", json=payload)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "language-negotiator",
    }


def test_negotiate_success():
    response = _post(
        {
            "accept_language": "zh-CN,zh;q=0.9,en;q=0.8,*;q=0.5",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["selected_language"] == "zh-CN"
    assert body["selected_supported_index"] == 1
    assert body["fallback"] is False
    assert len(body["candidates"]) == 4
    zh_cn = body["candidates"][1]
    assert zh_cn["status"] == "selected"
    # Truncation trace: full tag matches directly.
    assert zh_cn["events"][0] == {
        "truncated_to": "zh-cn",
        "outcome": "match",
        "matched_range": "zh-cn",
        "q": 1.0,
        "header_position": 1,
        "detail": None,
    }


def test_missing_header_default_fallback():
    response = _post(
        {
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["selected_language"] == "en-US"
    assert body["fallback"] is True
    assert body["fallback_reason"] == "no-valid-range"
    assert body["parsed_ranges"] == []


def test_invalid_accept_language_returns_422():
    response = _post(
        {
            "accept_language": "en;q=2",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
        }
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "accept_language"]
    assert detail[0]["type"] == "accept_language.syntax_error"


def test_default_not_in_supported_returns_422():
    response = _post(
        {
            "accept_language": "en",
            "supported_languages": SUPPORTED,
            "default_language": "fr-FR",
        }
    )
    assert response.status_code == 422


def test_duplicate_supported_tags_returns_422():
    response = _post(
        {
            "supported_languages": ["en", "EN"],
            "default_language": "en",
        }
    )
    assert response.status_code == 422


def test_empty_supported_returns_422():
    response = _post(
        {"supported_languages": [], "default_language": "en"}
    )
    assert response.status_code == 422


def test_extra_field_rejected_422():
    response = _post(
        {
            "supported_languages": ["en"],
            "default_language": "en",
            "unexpected": 1,
        }
    )
    assert response.status_code == 422


def test_q_zero_trace_and_fallback():
    response = _post(
        {
            "accept_language": "*;q=0",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["selected_language"] == "en-US"
    assert body["fallback"] is True
    assert all(c["status"] == "excluded" for c in body["candidates"])
    assert "q=0" in body["message"]


def test_wildcard_and_duplicates():
    response = _post(
        {
            "accept_language": "de;q=0.9,en;q=0.1,en;q=0.5,*;q=0.05",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
        }
    )
    body = response.json()
    # en wins among wildcard candidates; duplicate second en ignored.
    assert body["selected_language"] == "en-US"
    assert body["ignored_duplicates"] == [
        {"range": "en", "position": 3}
    ]


def test_locale_fallback_chain_resolves():
    response = _post(
        {
            "accept_language": "zh-HK;q=0.9",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": {"zh-HK": "zh-MO", "zh-MO": "zh-TW"},
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["selected_language"] == "zh-TW"
    assert body["selected_supported_index"] == 2
    assert body["fallback"] is False
    assert body["fallback_reason"] is None
    assert body["locale_fallback"] == {
        "source_range": "zh-hk",
        "path": ["zh-hk", "zh-mo", "zh-TW"],
        "resolved_tag": "zh-TW",
    }
    zh_tw = body["candidates"][2]
    assert zh_tw["status"] == "selected"


def test_locale_fallback_weight_and_header_order():
    # fr has the higher weight -> its chain is tried first.
    response = _post(
        {
            "accept_language": "de;q=0.5,fr;q=0.8",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": {"de": "zh-CN", "fr": "ja-JP"},
        }
    )
    body = response.json()
    assert body["selected_language"] == "ja-JP"
    assert body["locale_fallback"]["source_range"] == "fr"
    # Equal weights -> header order decides.
    response = _post(
        {
            "accept_language": "de;q=0.5,fr;q=0.5",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": {"de": "zh-CN", "fr": "ja-JP"},
        }
    )
    body = response.json()
    assert body["selected_language"] == "zh-CN"
    assert body["locale_fallback"]["source_range"] == "de"


def test_locale_fallback_respects_q_zero_concrete_exclusion():
    response = _post(
        {
            "accept_language": "zh-HK;q=0.9,zh-TW;q=0",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": {"zh-HK": "zh-TW"},
        }
    )
    assert response.status_code == 200
    body = response.json()
    # zh-TW is excluded by a q=0 concrete range -> chain yields nothing.
    assert body["selected_language"] == "en-US"
    assert body["fallback"] is True
    assert body["fallback_reason"] == "no-acceptable-candidate"
    assert body["locale_fallback"] is None


def test_locale_fallback_absent_keeps_existing_behavior():
    response = _post(
        {
            "accept_language": "zh-HK;q=0.9",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["selected_language"] == "en-US"
    assert body["fallback"] is True
    assert body["locale_fallback"] is None


def test_locale_fallback_unused_when_standard_match_exists():
    response = _post(
        {
            "accept_language": "zh-TW;q=0.9",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": {"zh-TW": "zh-CN"},
        }
    )
    body = response.json()
    assert body["selected_language"] == "zh-TW"
    assert body["locale_fallback"] is None


def test_locale_fallback_case_insensitive_lookup():
    response = _post(
        {
            "accept_language": "ZH-hk;q=0.9",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": {"zh-HK": "ZH-tW"},
        }
    )
    body = response.json()
    assert body["selected_language"] == "zh-TW"
    assert body["locale_fallback"]["resolved_tag"] == "zh-TW"


@pytest.mark.parametrize(
    "locale_fallbacks",
    [
        {"zh-HK": "zh-TW", "ZH-hk": "zh-CN"},  # duplicate source (case-insensitive)
        {"zh HK": "zh-TW"},  # illegal source tag
        {"zh-HK": "not a tag"},  # illegal target tag
        {"zh-HK": "*"},  # wildcard is not a tag
        {"zh-HK": "zh-HK"},  # self loop
        {"zh-HK": "zh-MO", "zh-MO": "zh-HK"},  # cycle
        {"zh-HK": "zh-MO"},  # dangling: zh-MO unsupported, chain ends
        {"zh-HK": "zh-MO", "zh-MO": "zh-SG"},  # dangling multi-hop chain
    ],
)
def test_locale_fallback_invalid_returns_422(locale_fallbacks):
    response = _post(
        {
            "accept_language": "zh-HK;q=0.9",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": locale_fallbacks,
        }
    )
    assert response.status_code == 422


def test_locale_fallback_chain_must_reach_supported_case_insensitive():
    # Chain target matches a supported tag with different casing -> valid.
    response = _post(
        {
            "accept_language": "zh-HK;q=0.9",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": {"zh-HK": "ZH-tw"},
        }
    )
    assert response.status_code == 200
    assert response.json()["selected_language"] == "zh-TW"
