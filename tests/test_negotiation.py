"""Unit tests for Accept-Language parsing and negotiation core."""

import pytest

from app.negotiation import (
    AcceptLanguageError,
    negotiate,
    parse_accept_language,
    truncation_chain,
)


class TestParse:
    def test_none_and_empty(self):
        assert parse_accept_language(None) == []
        assert parse_accept_language("   ") == []

    def test_basic_q_weights_and_order(self):
        ranges = parse_accept_language("zh-CN,zh;q=0.9,en;q=0.8")
        assert [(r.range, r.q, r.position) for r in ranges] == [
            ("zh-cn", 1.0, 1),
            ("zh", 0.9, 2),
            ("en", 0.8, 3),
        ]

    def test_wildcard(self):
        ranges = parse_accept_language("*;q=0.5")
        assert ranges[0].range == "*"
        assert ranges[0].q == 0.5

    def test_duplicate_ranges_keep_first(self):
        ranges = parse_accept_language("en;q=0.8,EN;q=0.2, fr")
        assert [(r.range, r.q) for r in ranges] == [("en", 0.8), ("fr", 1.0)]

    def test_q_zero_preserved(self):
        ranges = parse_accept_language("en;q=0")
        assert ranges[0].q == 0.0

    @pytest.mark.parametrize(
        "header",
        [
            "en;q=",  # missing q value
            "en;q=2",  # out of range
            "en;q=1.0001",  # too many decimals
            "en;q=.5",  # leading dot forbidden
            "en;r=0.5",  # unknown parameter
            "en;q;q",  # q without value
            "not a tag",  # whitespace in tag
            "12345",  # not BCP 47
            "en,,fr",  # empty element
            "en-",  # dangling separator
            "en;;fr",  # empty parameter
        ],
    )
    def test_invalid_syntax(self, header):
        with pytest.raises(AcceptLanguageError):
            parse_accept_language(header)

    def test_ows_around_elements_and_params_allowed(self):
        # RFC 9110 allows OWS around elements, ";" and "=".
        ranges = parse_accept_language(" en , fr ; q = 0.5 ")
        assert [(r.range, r.q) for r in ranges] == [("en", 1.0), ("fr", 0.5)]


class TestTruncationChain:
    def test_basic(self):
        assert truncation_chain("zh-Hant-TW") == [
            "zh-hant-tw",
            "zh-hant",
            "zh",
        ]

    def test_singleton_prefixes_skipped_but_intermediate_kept(self):
        # RFC 4647 truncates one subtag at a time and only skips prefixes
        # whose trailing subtag is a singleton.
        assert truncation_chain("zh-CN-u-ca-chinese") == [
            "zh-cn-u-ca-chinese",
            "zh-cn-u-ca",
            "zh-cn",
            "zh",
        ]

    def test_privateuse_singleton(self):
        assert truncation_chain("en-US-x-foo") == [
            "en-us-x-foo",
            "en-us",
            "en",
        ]

    def test_pure_privateuse_tag(self):
        assert truncation_chain("x-foo-bar") == ["x-foo-bar", "x-foo"]


class TestNegotiate:
    SUPPORTED = ["en-US", "zh-CN", "zh-TW", "ja-JP"]
    DEFAULT = "en-US"

    def test_weights_and_truncation(self):
        result = negotiate(
            "zh-TW;q=0.95,zh;q=0.9,en;q=0.8,*;q=0.5",
            self.SUPPORTED,
            self.DEFAULT,
        )
        assert result["selected_language"] == "zh-TW"
        assert result["fallback"] is False
        # zh-CN truncates to zh -> matched by 'zh' range at q 0.9.
        zh_cn = next(c for c in result["candidates"] if c["tag"] == "zh-CN")
        assert zh_cn["matched_range"] == "zh"
        assert zh_cn["q"] == 0.9
        # ja-JP only matches wildcard.
        ja = next(c for c in result["candidates"] if c["tag"] == "ja-JP")
        assert ja["matched_range"] == "*"
        assert ja["q"] == 0.5
        match_events = [e for e in ja["events"] if e["outcome"] == "match"]
        assert match_events[-1]["truncated_to"] == "*"

    def test_q_zero_excludes_tag(self):
        result = negotiate(
            "en;q=0,zh-CN;q=0.9", self.SUPPORTED, self.DEFAULT
        )
        en = result["candidates"][0]
        assert en["status"] == "excluded"
        assert en["q"] == 0
        assert result["selected_language"] == "zh-CN"

    def test_q_zero_blocks_wildcard_excluded_candidates(self):
        # All explicit ranges q=0 -> default fallback even with wildcard q=1
        # concrete en q=0 excludes en; ja has wildcard q=1 though...
        result = negotiate(
            "en;q=0,ja;q=0,*;q=0.1", self.SUPPORTED, self.DEFAULT
        )
        assert result["selected_language"] == "zh-CN"
        assert result["fallback"] is False

    def test_all_excluded_falls_back_to_default(self):
        result = negotiate("*;q=0", self.SUPPORTED, self.DEFAULT)
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is True
        assert result["fallback_reason"] == "no-acceptable-candidate"
        assert "q=0" in result["message"]

    def test_missing_header_falls_back(self):
        for header in (None, "", "   "):
            result = negotiate(header, self.SUPPORTED, self.DEFAULT)
            assert result["selected_language"] == "en-US"
            assert result["fallback_reason"] == "no-valid-range"
            assert all(c["status"] == "no-range" for c in result["candidates"])

    def test_tie_breaks_by_server_order(self):
        # en and zh both match * at q=0.1 -> earliest in supported list wins.
        result = negotiate("de;q=0.9,*;q=0.1", self.SUPPORTED, self.DEFAULT)
        assert result["selected_language"] == "en-US"

    def test_exact_match_beats_prefix_and_wildcard(self):
        supported = ["en", "en-US"]
        result = negotiate("en-US;q=0.1,en;q=0.9,*;q=0.99", supported, "en")
        # en-US exact q=0.1 vs en exact q=0.9: highest q wins -> en.
        assert result["selected_language"] == "en"
        en_us = result["candidates"][1]
        assert en_us["matched_range"] == "en-us"
        # wildcard event skipped because concrete match exists
        assert all(
            e["outcome"] != "match" or e["truncated_to"] != "*"
            for e in en_us["events"]
        )

    def test_duplicate_header_ranges_reported(self):
        result = negotiate(
            "en;q=0.8,en;q=0.2", self.SUPPORTED, self.DEFAULT
        )
        assert len(result["parsed_ranges"]) == 1
        assert result["ignored_duplicates"] == [
            {"range": "en", "position": 2}
        ]
        assert result["candidates"][0]["q"] == 0.8

    def test_wildcard_only_selects_first_supported(self):
        result = negotiate("*", self.SUPPORTED, self.DEFAULT)
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is False


class TestLocaleFallback:
    SUPPORTED = ["en-US", "zh-CN", "zh-TW", "ja-JP"]
    DEFAULT = "en-US"

    def test_chain_resolves_when_no_acceptable_candidate(self):
        result = negotiate(
            "zh-HK;q=0.9",
            self.SUPPORTED,
            self.DEFAULT,
            locale_fallbacks={"zh-HK": "zh-TW"},
        )
        assert result["selected_language"] == "zh-TW"
        assert result["selected_supported_index"] == 2
        assert result["fallback"] is False
        assert result["fallback_reason"] is None
        report = result["locale_fallback"]
        assert report["source_range"] == "zh-hk"
        assert report["path"] == ["zh-hk", "zh-TW"]
        assert report["resolved_tag"] == "zh-TW"
        zh_tw = next(c for c in result["candidates"] if c["tag"] == "zh-TW")
        assert zh_tw["status"] == "selected"

    def test_multi_hop_path_passes_through_unsupported_tags(self):
        result = negotiate(
            "zh-HK;q=0.9",
            self.SUPPORTED,
            self.DEFAULT,
            locale_fallbacks={"zh-HK": "zh-MO", "zh-MO": "zh-TW"},
        )
        assert result["selected_language"] == "zh-TW"
        assert result["locale_fallback"]["path"] == ["zh-hk", "zh-mo", "zh-TW"]

    def test_first_supported_hop_wins(self):
        result = negotiate(
            "zh-HK;q=0.9",
            self.SUPPORTED,
            self.DEFAULT,
            locale_fallbacks={
                "zh-HK": "zh-MO",
                "zh-MO": "zh-TW",
                "zh-TW": "zh-CN",
            },
        )
        assert result["selected_language"] == "zh-TW"
        assert result["locale_fallback"]["path"] == ["zh-hk", "zh-mo", "zh-TW"]

    def test_sources_ordered_by_weight_then_header_order(self):
        # Higher q wins even when it appears later in the header.
        result = negotiate(
            "de;q=0.5,fr;q=0.8",
            self.SUPPORTED,
            self.DEFAULT,
            locale_fallbacks={"de": "zh-CN", "fr": "ja-JP"},
        )
        assert result["selected_language"] == "ja-JP"
        assert result["locale_fallback"]["source_range"] == "fr"
        # Equal weights: header order decides.
        result = negotiate(
            "de;q=0.5,fr;q=0.5",
            self.SUPPORTED,
            self.DEFAULT,
            locale_fallbacks={"de": "zh-CN", "fr": "ja-JP"},
        )
        assert result["selected_language"] == "zh-CN"
        assert result["locale_fallback"]["source_range"] == "de"

    def test_q_zero_concrete_range_blocks_fallback_target(self):
        result = negotiate(
            "zh-HK;q=0.9,zh-TW;q=0",
            self.SUPPORTED,
            self.DEFAULT,
            locale_fallbacks={"zh-HK": "zh-TW"},
        )
        # zh-TW is excluded by a q=0 concrete range -> chain yields nothing.
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is True
        assert result["fallback_reason"] == "no-acceptable-candidate"
        assert result["locale_fallback"] is None

    def test_excluded_hop_skipped_and_chain_continues(self):
        result = negotiate(
            "zh-HK;q=0.9,zh-CN;q=0",
            ["zh-CN", "zh-TW"],
            "zh-CN",
            locale_fallbacks={"zh-HK": "zh-CN", "zh-CN": "zh-TW"},
        )
        # zh-CN is excluded by q=0; the walk continues to zh-TW.
        assert result["selected_language"] == "zh-TW"
        assert result["locale_fallback"]["path"] == ["zh-hk", "zh-CN", "zh-TW"]

    def test_next_source_tried_when_chain_dead_ends_on_excluded(self):
        result = negotiate(
            "zh-HK;q=0.9,fr;q=0.8,zh-TW;q=0",
            self.SUPPORTED,
            self.DEFAULT,
            locale_fallbacks={"zh-HK": "zh-TW", "fr": "zh-CN"},
        )
        # zh-hk chain ends at excluded zh-TW; fr chain resolves to zh-CN.
        assert result["selected_language"] == "zh-CN"
        assert result["locale_fallback"]["source_range"] == "fr"
        assert result["locale_fallback"]["path"] == ["fr", "zh-CN"]

    def test_q_zero_range_and_wildcard_are_not_sources(self):
        result = negotiate(
            "zh-HK;q=0",
            self.SUPPORTED,
            self.DEFAULT,
            locale_fallbacks={"zh-HK": "zh-TW"},
        )
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is True
        assert result["locale_fallback"] is None

    def test_standard_match_wins_and_chain_is_not_consulted(self):
        result = negotiate(
            "zh-TW;q=0.9",
            self.SUPPORTED,
            self.DEFAULT,
            locale_fallbacks={"zh-TW": "zh-CN"},
        )
        assert result["selected_language"] == "zh-TW"
        assert result["locale_fallback"] is None

    def test_no_locale_fallbacks_keeps_existing_behavior(self):
        result = negotiate("zh-HK;q=0.9", self.SUPPORTED, self.DEFAULT)
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is True
        assert result["locale_fallback"] is None

    def test_missing_header_ignores_chains(self):
        result = negotiate(
            None,
            self.SUPPORTED,
            self.DEFAULT,
            locale_fallbacks={"zh-HK": "zh-TW"},
        )
        assert result["selected_language"] == "en-US"
        assert result["fallback_reason"] == "no-valid-range"
        assert result["locale_fallback"] is None
