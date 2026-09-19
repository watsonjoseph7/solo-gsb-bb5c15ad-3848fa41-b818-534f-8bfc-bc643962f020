"""Pydantic request/response models for the language negotiation API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .negotiation import TAG_RE


class NegotiationRequest(BaseModel):
    """Negotiation input.

    ``accept_language`` is the raw header value; ``null``/missing or an
    empty string means "no valid ranges" and triggers the default
    fallback.
    """

    model_config = ConfigDict(extra="forbid")

    accept_language: str | None = Field(
        default=None,
        title="原始 Accept-Language 头",
        description="例如 'zh-CN,zh;q=0.9,en;q=0.8,*;q=0.5'；缺失/空串视为无有效范围",
    )
    supported_languages: list[str] = Field(
        min_length=1,
        title="服务端支持的 BCP 47 语言标签（按优先级/稳定顺序排列）",
    )
    default_language: str = Field(
        title="默认语言标签，必须包含在 supported_languages 中"
    )
    locale_fallbacks: dict[str, str] | None = Field(
        default=None,
        title="语言回退链（源标签 -> 下一标签 的有向映射）",
        description=(
            "标准 lookup 无可接受候选时，按正 q 具体范围的权重降序、头部顺序"
            "依次沿链查找首个未被 q=0 具体范围排除的受支持语言"
        ),
    )

    @field_validator("supported_languages")
    @classmethod
    def _validate_supported(cls, value: list[str]) -> list[str]:
        normalized_seen: set[str] = set()
        for tag in value:
            if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
                raise ValueError(f"不是合法的 BCP 47 标签: {tag!r}")
            key = tag.lower()
            if key in normalized_seen:
                raise ValueError(f"supported_languages 存在重复标签（大小写不敏感）: {tag!r}")
            normalized_seen.add(key)
        return value

    @field_validator("default_language")
    @classmethod
    def _validate_default(cls, value: str) -> str:
        if not TAG_RE.fullmatch(value):
            raise ValueError(f"default_language 不是合法的 BCP 47 标签: {value!r}")
        return value

    @field_validator("locale_fallbacks")
    @classmethod
    def _validate_locale_fallbacks(
        cls, value: dict[str, str] | None
    ) -> dict[str, str] | None:
        if value is None:
            return value
        graph: dict[str, str] = {}
        for source, target in value.items():
            if not TAG_RE.fullmatch(source):
                raise ValueError(
                    f"locale_fallbacks 的源不是合法的 BCP 47 标签: {source!r}"
                )
            if not TAG_RE.fullmatch(target):
                raise ValueError(
                    f"locale_fallbacks 中 {source!r} 的目标不是合法的 BCP 47 标签: {target!r}"
                )
            key = source.lower()
            if key in graph:
                raise ValueError(
                    f"locale_fallbacks 存在重复源（大小写不敏感）: {source!r}"
                )
            graph[key] = target.lower()
        # 环路检测：从每个源出发沿链行走，重复访问同一节点即环路。
        for start in graph:
            visited: set[str] = set()
            node = start
            while node in graph:
                if node in visited:
                    raise ValueError(
                        f"locale_fallbacks 存在环路，途经: {node!r}"
                    )
                visited.add(node)
                node = graph[node]
        return value

    @model_validator(mode="after")
    def _default_must_be_supported(self) -> "NegotiationRequest":
        supported_lower = {tag.lower() for tag in self.supported_languages}
        if self.default_language.lower() not in supported_lower:
            raise ValueError(
                "default_language 必须包含在 supported_languages 中（大小写不敏感）"
            )
        return self

    @model_validator(mode="after")
    def _locale_fallbacks_must_reach_supported(self) -> "NegotiationRequest":
        if not self.locale_fallbacks:
            return self
        supported_lower = {tag.lower() for tag in self.supported_languages}
        graph = {
            source.lower(): target.lower()
            for source, target in self.locale_fallbacks.items()
        }
        # 环路已在字段校验阶段拒绝，此处的行走必然终止。
        for start, first_hop in graph.items():
            node = first_hop
            while node not in supported_lower:
                following = graph.get(node)
                if following is None:
                    raise ValueError(
                        f"locale_fallbacks 中自 {start!r} 出发的链无法到达受支持语言"
                        f"（悬空于 {node!r}）"
                    )
                node = following
        return self


class TraceEvent(BaseModel):
    truncated_to: str = Field(description="逐级截断后的标签前缀，或 '*' 通配符")
    outcome: Literal["match", "no-match", "skipped"]
    matched_range: str | None = None
    q: float | None = None
    header_position: int | None = None
    detail: str | None = None


class CandidateReport(BaseModel):
    tag: str
    supported_index: int
    truncation_chain: list[str]
    events: list[TraceEvent]
    matched_range: str | None
    q: float | None = Field(description="命中范围的客户端权重；未命中为 null")
    status: Literal["selected", "eligible", "excluded", "unmatched", "no-range"]
    detail: str


class ParsedRange(BaseModel):
    range: str
    q: float
    position: int = Field(description="在 Accept-Language 头中的序号（从 1 开始）")


class IgnoredDuplicate(BaseModel):
    range: str
    position: int


class LocaleFallbackReport(BaseModel):
    source_range: str = Field(description="触发回退的客户端语言范围（小写）")
    path: list[str] = Field(
        description="实际回退路径：源范围 → 中间跳 → 最终标签（受支持节点保持服务端原始大小写）"
    )
    resolved_tag: str = Field(description="最终选中的服务端标签（服务端原始大小写）")


class NegotiationResponse(BaseModel):
    selected_language: str
    selected_supported_index: int
    fallback: bool
    fallback_reason: Literal["no-valid-range", "no-acceptable-candidate"] | None
    message: str
    locale_fallback: LocaleFallbackReport | None = Field(
        default=None,
        description="locale_fallbacks 链回退的触发信息；未触发或未提供 locale_fallbacks 时为 null",
    )
    parsed_ranges: list[ParsedRange]
    ignored_duplicates: list[IgnoredDuplicate]
    candidates: list[CandidateReport]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
