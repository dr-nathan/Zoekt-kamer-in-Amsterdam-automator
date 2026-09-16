from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


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


@dataclass(frozen=True, slots=True)
class Evidence:
    text: str
    confidence: float
    strength: str = "mentioned"


@dataclass(frozen=True, slots=True)
class ListingAttributes:
    raw_post_key: str
    listing_kind: ListingKind = ListingKind.UNKNOWN
    monthly_rent: float | None = None
    utilities: UtilitiesStatus = UtilitiesStatus.UNKNOWN
    deposit_amount: float | None = None
    deposit_months: float | None = None
    room_size_m2: float | None = None
    property_size_m2: float | None = None
    location_text: str | None = None
    available_from: str | None = None
    available_to: str | None = None
    lease_type: LeaseType = LeaseType.UNKNOWN
    registration: RegistrationStatus = RegistrationStatus.UNKNOWN
    furnishing: FurnishingStatus = FurnishingStatus.UNKNOWN
    gender: GenderRequirement = GenderRequirement.UNKNOWN
    age_min: int | None = None
    age_max: int | None = None
    dutch_requirement: RequirementLevel = RequirementLevel.UNKNOWN
    internationals: InternationalStatus = InternationalStatus.UNKNOWN
    applicant_status: ApplicantStatus = ApplicantStatus.UNKNOWN
    private_bathroom: bool | None = None
    amenities: tuple[str, ...] = ()
    particularities: tuple[str, ...] = ()
    evidence: dict[str, Evidence] = field(default_factory=dict)
    extraction_version: str = "rules-v1"
    extracted_at: str = ""

    def evidence_dict(self) -> dict[str, dict[str, Any]]:
        return {name: asdict(item) for name, item in self.evidence.items()}

