from __future__ import annotations

import re
from calendar import monthrange
from datetime import UTC, date, datetime
from pathlib import Path

from fb_automator.listing_models import (
    ApplicantStatus,
    Evidence,
    FurnishingStatus,
    GenderRequirement,
    InternationalStatus,
    LeaseType,
    ListingAttributes,
    ListingKind,
    RegistrationStatus,
    RequirementLevel,
    UtilitiesStatus,
)
from fb_automator.storage import PostStore

NUMBER = r"\d{1,4}(?:[.,]\d{1,3})?"
MONTHS = {
    "january": 1,
    "januari": 1,
    "february": 2,
    "februari": 2,
    "march": 3,
    "maart": 3,
    "april": 4,
    "may": 5,
    "mei": 5,
    "june": 6,
    "juni": 6,
    "july": 7,
    "juli": 7,
    "august": 8,
    "augustus": 8,
    "september": 9,
    "october": 10,
    "oktober": 10,
    "november": 11,
    "december": 12,
}
MONTH_PATTERN = "|".join(MONTHS)

LOCATION_TERMS = (
    "Amsterdam Nieuw-West",
    "Amsterdam Oud-Zuid",
    "Amsterdam-Zuid",
    "Amsterdam Centrum",
    "Amsterdam Oost",
    "Amsterdam West",
    "Watergraafsmeer",
    "Hoofddorppleinbuurt",
    "Rivierenbuurt",
    "Buitenveldert",
    "Bos en Lommer",
    "Venserpolder",
    "Houthavens",
    "De Baarsjes",
    "Oud-West",
    "Nieuw-West",
    "De Pijp",
    "Westerpark",
    "Jordaan",
    "Overtoom",
    "Sloterkade",
    "Delflandplein",
    "Lelylaan",
    "Amstelveen",
)


def _first_match(patterns: tuple[str, ...], text: str) -> re.Match[str] | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            return match
    return None


def _number(raw: str) -> float:
    cleaned = raw.strip().replace(" ", "")
    if "," in cleaned and "." in cleaned:
        decimal = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        cleaned = cleaned.replace(thousands, "").replace(decimal, ".")
    elif "," in cleaned:
        before, after = cleaned.rsplit(",", 1)
        cleaned = before + after if len(after) == 3 else before + "." + after
    elif cleaned.count(".") == 1 and len(cleaned.rsplit(".", 1)[1]) == 3:
        cleaned = cleaned.replace(".", "")
    return float(cleaned)


def _evidence(match: re.Match[str], confidence: float, strength: str = "mentioned") -> Evidence:
    text = " ".join(match.group(0).split())
    return Evidence(text=text[:180], confidence=confidence, strength=strength)


def _extract_price(text: str) -> tuple[float | None, Evidence | None]:
    patterns = (
        rf"(?:huur(?:prijs)?|rent|price|fee)\s*(?:bedraagt|is|:)?\s*(?:ongeveer|around|approximately|ca\.)?\s*€?\s*({NUMBER})",
        rf"€\s*({NUMBER})\s*(?:p/?m|per maand|a month|monthly|incl|excl|all[- ]in)",
        rf"({NUMBER})\s*(?:euro|€)\s*(?:p/?m|per maand|a month)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = _number(match.group(1))
            if 200 <= value <= 5_000:
                return value, _evidence(match, 0.96)
    return None, None


def _extract_deposit(text: str) -> tuple[float | None, float | None, Evidence | None]:
    amount_match = _first_match(
        (
            rf"(?:borg|deposit)\s*(?:is|:|van)?\s*€?\s*({NUMBER})",
            rf"€\s*({NUMBER})[^\n]{{0,20}}(?:borg|deposit)",
        ),
        text,
    )
    months_match = _first_match(
        (r"(?:borg|deposit)\s*(?:is|:)?\s*(?:eenmalig\s*)?(\d+(?:[.,]\d+)?)\s*(?:x|months?|maanden)",),
        text,
    )
    amount = _number(amount_match.group(1)) if amount_match else None
    months = _number(months_match.group(1)) if months_match else None
    match = amount_match or months_match
    return amount, months, _evidence(match, 0.95) if match else None


def _extract_sizes(text: str) -> tuple[float | None, float | None, Evidence | None]:
    room_match = _first_match(
        (
            rf"\b(?:kamer|room)\b[^\n]{{0,90}}?({NUMBER})\s*(?:m2|m²|²|vierkante meter)",
            rf"({NUMBER})\s*(?:m2|m²|²)[^\n]{{0,35}}\b(?:kamer|room)\b",
        ),
        text,
    )
    values = [
        (_number(match.group(1)), match)
        for match in re.finditer(rf"({NUMBER})\s*(?:m2|m²|vierkante meter)", text, re.IGNORECASE)
    ]
    room_size = _number(room_match.group(1)) if room_match else None
    room_evidence = _evidence(room_match, 0.94) if room_match else None
    if room_size is None:
        plausible_rooms = [(value, match) for value, match in values if 5 <= value <= 40]
        if plausible_rooms:
            room_size, match = plausible_rooms[0]
            room_evidence = _evidence(match, 0.78)
    property_sizes = [value for value, _ in values if 40 < value <= 300]
    property_size = property_sizes[0] if property_sizes else None
    return room_size, property_size, room_evidence


def _extract_listing_kind(text: str) -> tuple[ListingKind, Evidence | None]:
    co_application = _first_match(
        (r"apply for the apartment together|submitting an application",), text
    )
    if co_application:
        return ListingKind.CO_APPLICATION, _evidence(co_application, 0.99)
    wanted = _first_match(
        (
            r"kamer\s*/\s*appartement gezocht in amsterdam",
            r"ik ben op zoek naar een nieuwe plek om te wonen",
            r"looking for (?:a )?(?:room|apartment|place) to (?:live|rent)",
        ),
        text,
    )
    if wanted:
        return ListingKind.WANTED, _evidence(wanted, 0.98)
    offered = _first_match(
        (
            r"\bkamer\b[^\n]{0,55}\b(?:vrij|vrijkomt|beschikbaar|te huur)\b",
            r"\broom\b (?:is )?(?:available|for (?:rent|sublet))",
            r"\b(?:nieuwe\s+)?huisgenoot(?:je)?\b[^\n]{0,40}\b(?:gezocht|zoeken|wanted)\b",
            r"\bop zoek naar (?:een )?nieuwe huisgenoot\b",
            r"\blooking for (?:one person|a third roommate)\b",
            r"\blooking for (?:a )?roommate for (?:my|our)\b",
            r"\broom to rent\b",
            r"roommate wanted",
        ),
        text,
    )
    if offered:
        return ListingKind.OFFER, _evidence(offered, 0.92)
    return ListingKind.UNKNOWN, None


def _extract_registration(text: str) -> tuple[RegistrationStatus, Evidence | None]:
    denied = _first_match(
        (
            r"inschrijving (?:is )?niet mogelijk",
            r"geen registratie",
            r"without registration",
            r"no registration",
            r"registration (?:is )?not possible",
        ),
        text,
    )
    if denied:
        return RegistrationStatus.NOT_ALLOWED, _evidence(denied, 0.99, "required")
    required = _first_match(
        (r"inschrijven is verplicht|registration (?:is )?required",), text
    )
    if required:
        return RegistrationStatus.REQUIRED, _evidence(required, 0.99, "required")
    allowed = _first_match(
        (
            r"inschrijven (?:is )?mogelijk",
            r"inschrijving mogelijk",
            r"registration possible",
            r"\+\s*registration",
            r"kan je (?:je )?inschrijven",
        ),
        text,
    )
    if allowed:
        return RegistrationStatus.ALLOWED, _evidence(allowed, 0.98)
    return RegistrationStatus.UNKNOWN, None


def _extract_lease_type(text: str) -> tuple[LeaseType, Evidence | None]:
    sublet = _first_match(
        (
            r"\bsublet\b|\bsubrent\b|\bonderhuur\b",
            r"tijdelijk(?:e)?(?: te huur)?",
            r"\b(?:1|one) maand\b|\b6 maanden\b",
            r"oct(?:ober)?\s*[-–]\s*feb(?:ruary)?",
        ),
        text,
    )
    if sublet:
        return LeaseType.SUBLET, _evidence(sublet, 0.94)
    indefinite = _first_match((r"onbepaalde tijd|long[- ]term|lange termijn",), text)
    if indefinite:
        return LeaseType.INDEFINITE, _evidence(indefinite, 0.96)
    fixed = _first_match(
        (
            r"\b\d+(?:[.,]\d+)?\s*(?:maanden?|months?|jaar|years?)\b",
            r"(?:tot|until)\s+\d{1,2}?\s*(?:of\s+)?(?:" + MONTH_PATTERN + r")",
        ),
        text,
    )
    if fixed:
        return LeaseType.FIXED_TERM, _evidence(fixed, 0.86)
    return LeaseType.UNKNOWN, None


def _extract_utilities(text: str) -> tuple[UtilitiesStatus, Evidence | None]:
    included = _first_match(
        (
            r"all[- ]in(?:clusive)?",
            r"utilities included|rent includes bills",
            r"(?:€\s*[\d.,]+|huur)[^\n]{0,35}\b(?:incl\.?|inclusief)\b",
        ),
        text,
    )
    excluded = _first_match(
        (
            r"(?:€\s*[\d.,]+|huur)[^\n]{0,35}\b(?:excl\.?|exclusief)\b",
            r"excluding utilities|servicekosten (?:komen|zijn)[^\n]{0,25}(?:bij|apart)",
        ),
        text,
    )
    if included and excluded:
        return UtilitiesStatus.MIXED, _evidence(excluded, 0.90)
    if included:
        return UtilitiesStatus.INCLUDED, _evidence(included, 0.92)
    if excluded:
        return UtilitiesStatus.EXCLUDED, _evidence(excluded, 0.92)
    return UtilitiesStatus.UNKNOWN, None


def _extract_furnishing(text: str) -> tuple[FurnishingStatus, Evidence | None]:
    unfurnished = _first_match((r"\bunfurnished\b|rest van de meubels niet",), text)
    if unfurnished:
        return FurnishingStatus.UNFURNISHED, _evidence(unfurnished, 0.97)
    partial = _first_match((r"meubels (?:zijn )?(?:beschikbaar )?ter overname",), text)
    if partial:
        return FurnishingStatus.PARTIAL, _evidence(partial, 0.95)
    furnished = _first_match((r"gemeubileerd|fully furnished|\bfurnished\b",), text)
    if furnished:
        return FurnishingStatus.FURNISHED, _evidence(furnished, 0.96)
    return FurnishingStatus.UNKNOWN, None


def _extract_gender(text: str) -> tuple[GenderRequirement, Evidence | None]:
    women_preferred = _first_match(
        (r"ideally looking for a female|bij voorkeur.*(?:vrouw|dame|meid)",), text
    )
    if women_preferred:
        return GenderRequirement.WOMEN, _evidence(women_preferred, 0.86, "preferred")
    women_required = _first_match(
        (
            r"females? only|only women",
            r"vrouwelijke huisgenoot|werkende vrouw|nederlandse vrouwelijke",
            r"kamerzoekende (?:dames|meiden)|sorry mannen",
            r"op zoek naar een (?:.*?\s)?(?:vrouw|dame|meid)",
        ),
        text,
    )
    if women_required:
        return GenderRequirement.WOMEN, _evidence(women_required, 0.94, "required")
    men = _first_match((r"mannelijke.*huisgenoot|nieuwe mannelijke",), text)
    if men:
        return GenderRequirement.MEN, _evidence(men, 0.97, "required")
    any_gender = _first_match(
        (r"m/v/x|m/v\b|man of vrouw|everyone welcome|iedereen welkom",), text
    )
    if any_gender:
        return GenderRequirement.ANY, _evidence(any_gender, 0.92)
    return GenderRequirement.UNKNOWN, None


def _extract_language(text: str) -> tuple[RequirementLevel, Evidence | None]:
    preferred = _first_match(
        (r"nederlands sprekend of lerend.*voorkeur|dutch.*preferred",), text
    )
    if preferred:
        return RequirementLevel.PREFERRED, _evidence(preferred, 0.86, "preferred")
    required = _first_match(
        (
            r"dutch speaking only",
            r"nederlands\s*sprekend|nederlandssprekend|nederlandstalige",
            r"no internationals|geen internationals",
        ),
        text,
    )
    if required:
        return RequirementLevel.REQUIRED, _evidence(required, 0.94, "required")
    return RequirementLevel.UNKNOWN, None


def _extract_internationals(text: str) -> tuple[InternationalStatus, Evidence | None]:
    excluded = _first_match(
        (r"no internationals|geen internationals|unfortunately no internationals",), text
    )
    if excluded:
        return InternationalStatus.EXCLUDED, _evidence(excluded, 0.99, "required")
    welcome = _first_match((r"internationals welcome|everyone welcome|iedereen welkom",), text)
    if welcome:
        return InternationalStatus.WELCOME, _evidence(welcome, 0.98)
    return InternationalStatus.UNKNOWN, None


def _extract_applicant_status(text: str) -> tuple[ApplicantStatus, Evidence | None]:
    no_students = _first_match(
        (r"no students|geen studenten|studenten.*teleurstellen|niet meer studerend",), text
    )
    if no_students:
        return ApplicantStatus.NO_STUDENTS, _evidence(no_students, 0.97, "required")
    student = _first_match(
        (r"vereist dat je.*studeert|student required|must be a student",), text
    )
    if student:
        return ApplicantStatus.STUDENT, _evidence(student, 0.96, "required")
    either = _first_match(
        (r"werkend of studerend|working or.*stud(?:y|ies)|einde van (?:haar|je) studie",), text
    )
    if either:
        return ApplicantStatus.WORKING_OR_STUDENT, _evidence(either, 0.88)
    working = _first_match(
        (
            r"werkende? (?:vrouw|huisgenoot|iemand|dame)",
            r"iemand die werkt|who has a job|currently also works|also working",
            r"stable income|full[- ]time",
        ),
        text,
    )
    if working:
        return ApplicantStatus.WORKING, _evidence(working, 0.88)
    return ApplicantStatus.UNKNOWN, None


def _extract_age(text: str) -> tuple[int | None, int | None, Evidence | None]:
    range_match = _first_match(
        (
            r"\b(1[89]|[2-6]\d)\s*(?:-|–|t/m)\s*(1[89]|[2-6]\d)\b",
            r"tussen de\s+(1[89]|[2-6]\d)\s+en\s+(1[89]|[2-6]\d)",
        ),
        text,
    )
    if range_match:
        return int(range_match.group(1)), int(range_match.group(2)), _evidence(range_match, 0.90)
    plus_match = re.search(r"\b(1[89]|[2-6]\d)\s*\+", text)
    if plus_match:
        return int(plus_match.group(1)), None, _evidence(plus_match, 0.91)
    max_match = re.search(r"(?:niet ouder dan|not older than)\s+(1[89]|[2-6]\d)", text, re.IGNORECASE)
    if max_match:
        return None, int(max_match.group(1)), _evidence(max_match, 0.92)
    return None, None, None


def _extract_location(text: str) -> tuple[str | None, Evidence | None]:
    for location in sorted(LOCATION_TERMS, key=len, reverse=True):
        match = re.search(re.escape(location), text, re.IGNORECASE)
        if match:
            return location, _evidence(match, 0.86)
    return None, None


def _date_from_parts(day: int, month: int, year: int | None, reference: date) -> date:
    inferred_year = year or reference.year
    if year is None and month < reference.month - 1:
        inferred_year += 1
    return date(inferred_year, month, day)


def _extract_dates(text: str, reference: date) -> tuple[str | None, str | None, Evidence | None]:
    numeric = _first_match(
        (r"(?:per|vanaf|from|starting)\s+(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?",),
        text,
    )
    start_match = _first_match(
        (
            rf"(?:per|vanaf|from|starting|available from)\s+(?:(begin|mid|half)\s*)?(\d{{1,2}})?(?:st|nd|rd|th)?\s*(?:of\s+)?({MONTH_PATTERN})(?:\s+(\d{{4}}))?",
        ),
        text,
    )
    available_from = None
    evidence = None
    if numeric:
        year_value = int(numeric.group(3)) if numeric.group(3) else None
        if year_value is not None and year_value < 100:
            year_value += 2000
        parsed = _date_from_parts(
            int(numeric.group(1)), int(numeric.group(2)), year_value, reference
        )
        available_from = parsed.isoformat()
        evidence = _evidence(numeric, 0.94)
    elif start_match:
        marker = start_match.group(1)
        day = int(start_match.group(2)) if start_match.group(2) else (15 if marker in {"mid", "half"} else 1)
        month = MONTHS[start_match.group(3).lower()]
        year_value = int(start_match.group(4)) if start_match.group(4) else None
        available_from = _date_from_parts(day, month, year_value, reference).isoformat()
        evidence = _evidence(start_match, 0.90)

    end_match = _first_match(
        (
            rf"(?:tot|until|through)\s+(\d{{1,2}})?(?:st|nd|rd|th)?\s*(?:of\s+)?({MONTH_PATTERN})(?:\s+(\d{{4}}))?",
        ),
        text,
    )
    available_to = None
    if end_match:
        month = MONTHS[end_match.group(2).lower()]
        year_value = int(end_match.group(3)) if end_match.group(3) else None
        end_year = year_value or reference.year
        day = int(end_match.group(1)) if end_match.group(1) else monthrange(end_year, month)[1]
        parsed = _date_from_parts(day, month, year_value, reference)
        if available_from and parsed < date.fromisoformat(available_from) and year_value is None:
            parsed = date(parsed.year + 1, parsed.month, parsed.day)
        available_to = parsed.isoformat()
        evidence = evidence or _evidence(end_match, 0.88)
    return available_from, available_to, evidence


def _extract_amenities(text: str) -> tuple[tuple[str, ...], bool | None]:
    patterns = {
        "balcony": r"balkon|balcony|loggia",
        "garden": r"\btuin\b|\bgarden\b",
        "terrace": r"terras|terrace|dakterras",
        "shared_living_room": r"gedeelde woonkamer|shared (?:a )?living room|woonkamer.*delen",
        "no_shared_living_room": r"geen (?:gedeelde )?woonkamer|no shared living room",
        "dishwasher": r"vaatwasser|dishwasher",
        "washing_machine": r"wasmachine|washing machine|laundry room",
        "storage": r"berging|storage area|shed|inbouwkast",
    }
    amenities = tuple(
        name for name, pattern in patterns.items() if re.search(pattern, text, re.IGNORECASE)
    )
    private_bathroom = None
    if re.search(r"private bathroom|eigen badkamer|eigen.*douche", text, re.IGNORECASE):
        private_bathroom = True
    return amenities, private_bathroom


def _extra_badges(text: str) -> list[str]:
    patterns = {
        "No couples": r"geen.*stelletjes|no couples",
        "No pets": r"geen.*huisdieren|no pets",
        "Non-smoking": r"no(?:r)? smoking|niet roken|non[- ]smoking",
        "No guarantors": r"no guarantors|geen garantsteller",
        "Youth contract": r"jongerencontract|youth contract",
        "No night shifts": r"geen nachtdiensten|no night shifts",
        "No parking permit": r"parkeervergunning.*niet mogelijk|no parking permit",
        "Multiple rooms": r"\b2 rooms\b|twee losse kamers|2 kamers",
    }
    return [label for label, pattern in patterns.items() if re.search(pattern, text, re.IGNORECASE)]


def extract_listing(raw_post_key: str, text: str, scraped_at: str) -> ListingAttributes:
    evidence: dict[str, Evidence] = {}

    listing_kind, item = _extract_listing_kind(text)
    if item:
        evidence["listing_kind"] = item
    monthly_rent, item = _extract_price(text)
    if item:
        evidence["monthly_rent"] = item
    deposit_amount, deposit_months, item = _extract_deposit(text)
    if item:
        evidence["deposit"] = item
    room_size, property_size, item = _extract_sizes(text)
    if item:
        evidence["room_size_m2"] = item
    registration, item = _extract_registration(text)
    if item:
        evidence["registration"] = item
    lease_type, item = _extract_lease_type(text)
    if item:
        evidence["lease_type"] = item
    utilities, item = _extract_utilities(text)
    if item:
        evidence["utilities"] = item
    furnishing, item = _extract_furnishing(text)
    if item:
        evidence["furnishing"] = item
    gender, item = _extract_gender(text)
    if item:
        evidence["gender"] = item
    dutch_requirement, item = _extract_language(text)
    if item:
        evidence["dutch_requirement"] = item
    internationals, item = _extract_internationals(text)
    if item:
        evidence["internationals"] = item
    applicant_status, item = _extract_applicant_status(text)
    if item:
        evidence["applicant_status"] = item
    age_min, age_max, item = _extract_age(text)
    if item:
        evidence["age"] = item
    location, item = _extract_location(text)
    if item:
        evidence["location"] = item

    reference = date.fromisoformat(scraped_at[:10])
    available_from, available_to, item = _extract_dates(text, reference)
    if item:
        evidence["availability"] = item
    amenities, private_bathroom = _extract_amenities(text)
    gender_strength = evidence.get("gender", Evidence("", 0)).strength

    badges: list[str] = []
    badge_values = (
        (listing_kind == ListingKind.WANTED, "Wanted, not offered"),
        (listing_kind == ListingKind.CO_APPLICATION, "Application pending"),
        (lease_type == LeaseType.SUBLET, "Temporary/sublet"),
        (lease_type == LeaseType.INDEFINITE, "Indefinite contract"),
        (registration == RegistrationStatus.NOT_ALLOWED, "No registration"),
        (registration == RegistrationStatus.REQUIRED, "Registration required"),
        (registration == RegistrationStatus.ALLOWED, "Registration possible"),
        (
            gender == GenderRequirement.WOMEN,
            "Women preferred" if gender_strength == "preferred" else "Women only",
        ),
        (
            gender == GenderRequirement.MEN,
            "Men preferred" if gender_strength == "preferred" else "Men only",
        ),
        (dutch_requirement == RequirementLevel.REQUIRED, "Dutch required"),
        (dutch_requirement == RequirementLevel.PREFERRED, "Dutch preferred"),
        (internationals == InternationalStatus.EXCLUDED, "No internationals"),
        (internationals == InternationalStatus.WELCOME, "Internationals welcome"),
        (applicant_status == ApplicantStatus.WORKING, "Working preferred/required"),
        (applicant_status == ApplicantStatus.STUDENT, "Student required"),
        (applicant_status == ApplicantStatus.NO_STUDENTS, "No students"),
        (furnishing == FurnishingStatus.FURNISHED, "Furnished"),
        (furnishing == FurnishingStatus.UNFURNISHED, "Unfurnished"),
        (private_bathroom is True, "Private bathroom"),
        (age_min is not None or age_max is not None, "Age restricted"),
    )
    badges.extend(label for condition, label in badge_values if condition)
    badges.extend(_extra_badges(text))

    return ListingAttributes(
        raw_post_key=raw_post_key,
        listing_kind=listing_kind,
        monthly_rent=monthly_rent,
        utilities=utilities,
        deposit_amount=deposit_amount,
        deposit_months=deposit_months,
        room_size_m2=room_size,
        property_size_m2=property_size,
        location_text=location,
        available_from=available_from,
        available_to=available_to,
        lease_type=lease_type,
        registration=registration,
        furnishing=furnishing,
        gender=gender,
        age_min=age_min,
        age_max=age_max,
        dutch_requirement=dutch_requirement,
        internationals=internationals,
        applicant_status=applicant_status,
        private_bathroom=private_bathroom,
        amenities=amenities,
        particularities=tuple(dict.fromkeys(badges)),
        evidence=evidence,
        extracted_at=datetime.now(UTC).isoformat(),
    )


def extract_database(path: Path) -> tuple[int, int]:
    with PostStore(path) as store:
        rows = store.raw_posts_for_extraction()
        listings = [
            extract_listing(row["dedupe_key"], row["text"], row["last_seen_at"])
            for row in rows
        ]
        store.upsert_listings(listings)
        return len(listings), store.listing_count()
