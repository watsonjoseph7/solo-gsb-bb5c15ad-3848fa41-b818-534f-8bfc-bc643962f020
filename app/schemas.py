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

    @model_validator(mode="after")
    def _default_must_be_supported(self) -> "NegotiationRequest":
        supported_lower = {tag.lower() for tag in self.supported_languages}
        if self.default_language.lower() not in supported_lower:
            raise ValueError(
                "default_language 必须包含在 supported_languages 中（大小写不敏感）"
            )
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


class NegotiationResponse(BaseModel):
    selected_language: str
    selected_supported_index: int
    fallback: bool
    fallback_reason: Literal["no-valid-range", "no-acceptable-candidate"] | None
    message: str
    parsed_ranges: list[ParsedRange]
    ignored_duplicates: list[IgnoredDuplicate]
    candidates: list[CandidateReport]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
