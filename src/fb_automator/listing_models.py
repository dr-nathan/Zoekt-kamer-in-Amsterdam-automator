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


class Currency(StrEnum):
    CHF = "CHF"
    EUR = "EUR"
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


class LausanneNeighborhood(StrEnum):
    """Official statistical neighborhoods used by the City of Lausanne."""

    CENTRE = "Centre"
    MAUPAS_VALENCY = "Maupas / Valency"
    SEBEILLON_MALLEY = "Sébeillon / Malley"
    MONTOIE_BOURDONNETTE = "Montoie / Bourdonnette"
    MONTRIOND_COUR = "Montriond / Cour"
    SOUS_GARE_OUCHY = "Sous-Gare / Ouchy"
    MONTCHOISI = "Montchoisi"
    FLORIMONT_CHISSIEZ = "Florimont / Chissiez"
    MOUSQUINES_BELLEVUE = "Mousquines / Bellevue"
    VALLON_BETHUSY = "Vallon / Béthusy"
    CHAILLY_ROVEREAZ = "Chailly / Rovéréaz"
    SALLAZ_VENNES_SECHAUD = "Sallaz / Vennes / Séchaud"
    SAUVABELIN = "Sauvabelin"
    BORDE_BELLEVAUX = "Borde / Bellevaux"
    VINET_PONTAISE = "Vinet / Pontaise"
    BOSSONS_BLECHERETTE = "Bossons / Blécherette"
    BEAULIEU_GREY_BOISY = "Beaulieu / Grey / Boisy"
    ZONES_FORAINES = "Zones foraines"
    OUTSIDE_LAUSANNE = "Hors Lausanne"


class AmsterdamNeighborhood(StrEnum):
    """Stable city districts used to normalize Amsterdam housing posts."""

    CENTRUM = "Centrum"
    WEST = "West"
    NIEUW_WEST = "Nieuw-West"
    ZUID = "Zuid"
    OOST = "Oost"
    NOORD = "Noord"
    ZUIDOOST = "Zuidoost"
    WESTPOORT = "Westpoort"
    WEESP = "Weesp"
    OUTSIDE_AMSTERDAM = "Hors Amsterdam"


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
    LANGUAGE_REQUIREMENT = "language_requirement"
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

    listing_kind: ListingKind = Field(
        description=(
            "Housing transaction direction: offer when the poster has housing and seeks a tenant "
            "or roommate; wanted only when the poster seeks housing for themselves; "
            "co_application when seeking someone to jointly apply for housing."
        )
    )
    monthly_rent: float | None = Field(
        ge=0, description="Monthly rent in the stated currency, excluding deposits."
    )
    currency: Currency
    utilities: UtilitiesStatus
    deposit_amount: float | None = Field(
        ge=0, description="Deposit in the stated currency, if explicitly stated."
    )
    deposit_months: float | None = Field(ge=0)
    room_size_m2: float | None = Field(ge=0)
    property_size_m2: float | None = Field(ge=0)
    location_text: str | None = Field(
        description=(
            "Most useful stated location for display, starting with the exact municipality when "
            "the listing is outside the configured source city."
        )
    )
    city: str | None
    neighborhood: LausanneNeighborhood | AmsterdamNeighborhood | None = Field(
        description=(
            "Standardized neighborhood for the configured source city, the matching Hors city "
            "label when clearly outside it, or null when it cannot be established."
        )
    )
    available_from: str | None = Field(description="ISO date YYYY-MM-DD, or null.")
    available_to: str | None = Field(description="ISO date YYYY-MM-DD, or null.")
    lease_type: LeaseType
    registration: RegistrationStatus
    furnishing: FurnishingStatus
    gender: GenderRequirement
    age_min: int | None = Field(ge=0, le=120)
    age_max: int | None = Field(ge=0, le=120)
    language_requirement: RequirementLevel
    internationals: InternationalStatus
    applicant_status: ApplicantStatus
    private_bathroom: bool | None
    amenities: list[str] = Field(description="Short normalized amenity names in French.")
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
        words = value.split()
        if len(words) <= 45:
            return value
        return " ".join(words[:45]).rstrip(" ,;:") + "…"


@dataclass(frozen=True, slots=True)
class ListingAttributes:
    raw_post_key: str
    source_hash: str
    listing_kind: ListingKind
    monthly_rent: float | None
    currency: Currency
    utilities: UtilitiesStatus
    deposit_amount: float | None
    deposit_months: float | None
    room_size_m2: float | None
    property_size_m2: float | None
    location_text: str | None
    city: str | None
    neighborhood: LausanneNeighborhood | AmsterdamNeighborhood | None
    available_from: str | None
    available_to: str | None
    lease_type: LeaseType
    registration: RegistrationStatus
    furnishing: FurnishingStatus
    gender: GenderRequirement
    age_min: int | None
    age_max: int | None
    language_requirement: RequirementLevel
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
