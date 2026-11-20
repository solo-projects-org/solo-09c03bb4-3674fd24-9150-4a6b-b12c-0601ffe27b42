"""Pydantic request/response models."""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, BeforeValidator, Field, StringConstraints

Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


def _normalize_country(value):
    if isinstance(value, str):
        return value.strip().upper()
    return value


Country = Annotated[
    str,
    BeforeValidator(_normalize_country),
    StringConstraints(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$"),
]
NonNegativeInt = Annotated[int, Field(ge=0)]


class BaseAmountIn(BaseModel):
    year: int = Field(ge=1900, le=2100, examples=[2023])
    code: Code = Field(examples=["01012100"])
    amount_cents: int = Field(ge=0, description="import amount in integer cents (分)")


class BaseIn(BaseModel):
    amounts: list[BaseAmountIn] = Field(
        min_length=1, description="import amounts for exactly two adjacent years"
    )


class BaseOut(BaseModel):
    years: list[int]
    codes_per_year: dict[str, int]


class MappingEntryIn(BaseModel):
    old_code: Code = Field(description="code in the earlier year")
    new_code: Code = Field(description="code in the later year")


class MappingIn(BaseModel):
    version: int = Field(ge=1, description="monotonically increasing mapping version")
    entries: list[MappingEntryIn] = Field(
        min_length=1,
        description="complete old<->new mapping; unchanged codes appear as (code, code)",
    )


class GroupSummary(BaseModel):
    group_id: int
    old_codes: list[str]
    new_codes: list[str]
    old_total_cents: int = Field(description="total of the earlier year's amounts")
    new_total_cents: int = Field(description="total of the later year's amounts")


class ChangeRecord(BaseModel):
    version: int
    replaced_version: Optional[int] = Field(
        description="version this one replaced; null for the first version"
    )
    removed_group_ids: list[int] = Field(description="group ids to take down")
    summaries: list[GroupSummary] = Field(description="group summaries to write")


class SummaryOut(BaseModel):
    version: Optional[int]
    years: Optional[list[int]]
    groups: list[GroupSummary]


class MappingOut(BaseModel):
    version: int
    replaced_version: Optional[int]
    created_at: str
    entries: list[MappingEntryIn]


class VersionInfo(BaseModel):
    version: int
    replaced_version: Optional[int]
    created_at: str


class VersionListOut(BaseModel):
    current_version: Optional[int]
    versions: list[VersionInfo]


# ---------------------------------------------------------- 国别明细 / 来源国
class PendingItemIn(BaseModel):
    amount_cents: NonNegativeInt = Field(
        description="pending amount in integer cents, apportionable to the candidates"
    )
    candidates: list[Country] = Field(
        min_length=1, description="candidate countries (ISO-style 2-letter codes)"
    )


class PendingItemOut(BaseModel):
    amount_cents: int
    candidates: list[str]


class CountryDetailIn(BaseModel):
    confirmed: dict[Country, NonNegativeInt] = Field(
        default_factory=dict, description="confirmed amount per country, in cents"
    )
    pending: list[PendingItemIn] = Field(
        default_factory=list, description="pending amounts with candidate countries"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "confirmed": {"CN": 100, "US": 80},
                    "pending": [{"amount_cents": 120, "candidates": ["CN", "US"]}],
                }
            ]
        }
    }


class CountryDetailOut(BaseModel):
    year: int
    code: str
    base_amount_cents: int = Field(description="base-table amount the detail must match")
    confirmed: dict[str, int]
    pending: list[PendingItemOut]
    updated_at: str


class DetailListOut(BaseModel):
    details: list[CountryDetailOut]


class MissingItemOut(BaseModel):
    code: str
    amount_cents: int


class AssignmentOut(BaseModel):
    code: str = Field(description="tax code the pending amount belongs to")
    amount_cents: int
    candidates: list[str]
    assigned: str = Field(description="country the amount is apportioned to")


class ApportionmentOut(BaseModel):
    totals: dict[str, int] = Field(description="resulting total per country")
    leaders: list[str] = Field(
        description="countries tied at the top; a single entry means a unique leader"
    )
    assignments: list[AssignmentOut]


class EvidenceOut(BaseModel):
    type: Literal["tie", "different_leaders"] = Field(
        description="one apportionment with a tied first place, or two "
        "apportionments with different leaders"
    )
    apportionments: list[ApportionmentOut]


class OriginYearOut(BaseModel):
    year: int
    status: Literal["no_imports", "incomplete", "determined", "undetermined"]
    codes: list[str] = Field(description="group member codes for this year")
    group_total_cents: int
    missing: Optional[list[MissingItemOut]] = Field(
        default=None, description="base items still lacking details (incomplete)"
    )
    leader: Optional[str] = Field(
        default=None, description="country that ranks first under every "
        "feasible apportionment (determined)"
    )
    leader_min_cents: Optional[int] = Field(
        default=None, description="worst-case total of the leader"
    )
    runner_up_max_cents: Optional[int] = Field(
        default=None, description="best-case total of the strongest rival"
    )
    evidence: Optional[EvidenceOut] = Field(
        default=None, description="sample apportionments for analysts (undetermined)"
    )


class OriginOut(BaseModel):
    group_id: int
    version: int = Field(description="mapping version this answer is based on")
    years: list[int]
    results: list[OriginYearOut]
