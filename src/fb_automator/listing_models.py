from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ListingKind(StrEnum):
    OFFER = "offer"
    WANTED = "wanted"
    CO_APPLICATION = "co_application"
    UNKNOWN = "unknown"


class LeaseType(StrEnum):
    SUBLET = "sublet"
    FIXED_TERM = "fixed_term"
    INDEFINITE = "indefinite"
    UNKNOWN = "unknown"


class RegistrationStatus(StrEnum):
    ALLOWED = "allowed"
    NOT_ALLOWED = "not_allowed"
    REQUIRED = "required"
    UNKNOWN = "unknown"


class FurnishingStatus(StrEnum):
    FURNISHED = "furnished"
    UNFURNISHED = "unfurnished"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class UtilitiesStatus(StrEnum):
    INCLUDED = "included"
    EXCLUDED = "excluded"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class GenderRequirement(StrEnum):
    WOMEN = "women"
    MEN = "men"
    ANY = "any"
    UNKNOWN = "unknown"


class RequirementLevel(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    NONE = "none"
    UNKNOWN = "unknown"


class InternationalStatus(StrEnum):
    WELCOME = "welcome"
    EXCLUDED = "excluded"
    UNKNOWN = "unknown"


class ApplicantStatus(StrEnum):
    WORKING = "working"
    STUDENT = "student"
    WORKING_OR_STUDENT = "working_or_student"
    NO_STUDENTS = "no_students"
    ANY = "any"
    UNKNOWN = "unknown"


class EvidenceStrength(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    MENTIONED = "mentioned"


class EvidenceField(StrEnum):
    LISTING_KIND = "listing_kind"
    MONTHLY_RENT = "monthly_rent"
    UTILITIES = "utilities"
    DEPOSIT = "deposit"
    ROOM_SIZE = "room_size_m2"
    PROPERTY_SIZE = "property_size_m2"
    LOCATION = "location"
    AVAILABILITY = "availability"
    LEASE_TYPE = "lease_type"
    REGISTRATION = "registration"
    FURNISHING = "furnishing"
    GENDER = "gender"
    AGE = "age"
    DUTCH_REQUIREMENT = "dutch_requirement"
    INTERNATIONALS = "internationals"
    APPLICANT_STATUS = "applicant_status"
    PRIVATE_BATHROOM = "private_bathroom"
    AMENITIES = "amenities"
    PARTICULARITIES = "particularities"


class AttributeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: EvidenceField
    quote: str = Field(
        min_length=1,
        description="Short verbatim phrase from the post supporting this field.",
    )
    confidence: float = Field(ge=0, le=1)
    strength: EvidenceStrength


class ExtractedListing(BaseModel):
    """Strict schema returned by the language model."""

    model_config = ConfigDict(extra="forbid")

    listing_kind: ListingKind
    monthly_rent: float | None = Field(
        ge=0, description="Monthly rent in euros, excluding deposits."
    )
    utilities: UtilitiesStatus
    deposit_amount: float | None = Field(
        ge=0, description="Deposit in euros, if explicitly stated."
    )
    deposit_months: float | None = Field(ge=0)
    room_size_m2: float | None = Field(ge=0)
    property_size_m2: float | None = Field(ge=0)
    location_text: str | None = Field(description="Most useful stated location for display.")
    city: str | None
    neighborhood: str | None
    available_from: str | None = Field(description="ISO date YYYY-MM-DD, or null.")
    available_to: str | None = Field(description="ISO date YYYY-MM-DD, or null.")
    lease_type: LeaseType
    registration: RegistrationStatus
    furnishing: FurnishingStatus
    gender: GenderRequirement
    age_min: int | None = Field(ge=0, le=120)
    age_max: int | None = Field(ge=0, le=120)
    dutch_requirement: RequirementLevel
    internationals: InternationalStatus
    applicant_status: ApplicantStatus
    private_bathroom: bool | None
    amenities: list[str] = Field(description="Short normalized amenity names in English.")
    particularities: list[str] = Field(
        description="Concise display labels for notable conditions or restrictions."
    )
    summary: str = Field(
        min_length=1,
        description="Factual housing-focused summary of at most 45 words.",
    )
    evidence: list[AttributeEvidence]

    @field_validator("available_from", "available_to")
    @classmethod
    def validate_iso_date(cls, value: str | None) -> str | None:
        if value is not None:
            date.fromisoformat(value)
        return value

    @field_validator("summary")
    @classmethod
    def validate_summary_length(cls, value: str) -> str:
        if len(value.split()) > 45:
            raise ValueError("summary must contain at most 45 words")
        return value


@dataclass(frozen=True, slots=True)
class ListingAttributes:
    raw_post_key: str
    source_hash: str
    listing_kind: ListingKind
    monthly_rent: float | None
    utilities: UtilitiesStatus
    deposit_amount: float | None
    deposit_months: float | None
    room_size_m2: float | None
    property_size_m2: float | None
    location_text: str | None
    city: str | None
    neighborhood: str | None
    available_from: str | None
    available_to: str | None
    lease_type: LeaseType
    registration: RegistrationStatus
    furnishing: FurnishingStatus
    gender: GenderRequirement
    age_min: int | None
    age_max: int | None
    dutch_requirement: RequirementLevel
    internationals: InternationalStatus
    applicant_status: ApplicantStatus
    private_bathroom: bool | None
    amenities: tuple[str, ...]
    particularities: tuple[str, ...]
    summary: str
    evidence: tuple[AttributeEvidence, ...]
    extraction_version: str
    extracted_at: str

    def evidence_jsonable(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.evidence]
