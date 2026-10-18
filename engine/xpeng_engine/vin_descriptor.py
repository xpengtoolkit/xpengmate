#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vin_descriptor.py — offline XPENG (小鹏) VIN decoder. Standard library only.

Renamed from ``xpeng_vin.py``; the module is shipped inside the integration, and
there is no standalone ``xpeng_vin.py`` in the repository any more. Import it by
its real name (``from .vin_descriptor import decode``, or by path for a
checkout).

WHY THIS EXISTS
---------------
There is no usable third-party decoder for XPENG VINs:

  * NHTSA vPIC  -> ``DecodeWMI/L1N`` returns ``Count: 0`` (L1N is not in vPIC at
    all) and ``DecodeVinValues`` returns ``ErrorCode: 7`` ("Manufacturer is not
    registered with NHTSA for sale or importation in the U.S."). Every descriptor
    field comes back empty. It is not a partial source, it is a blank one.
  * No public per-manufacturer positions-4-8 table exists for XPENG.
  * Third-party sites (vinstate.com, vindecoder.eu, ...) do carry an L1N table,
    but they are rate-limited, need network, and mix VIN-derived data with
    used-car-listing data.

So this module implements the decodable parts itself and keeps a small,
explicitly-evidenced lookup table for the rest.

WHAT IS ACTUALLY DECODABLE FOR XPENG
------------------------------------
===============  ==========================================================
Positions        Meaning
===============  ==========================================================
1-3              WMI. ``L1N`` = Zhaoqing XPeng New Energy Investment Co.
4-8              VDS. Manufacturer-internal *model family* key ONLY.
9                Check digit. XPENG **does** use the ISO 3779 / GB 16735
                 mod-11 scheme — verified against every sample in this file.
10               Model year (ISO 3779 30-year cycle).
11               Plant. ``B`` = Guangzhou CN, ``D`` = Graz AT (Magna Steyr).
12-17            Serial. **Serial ranges are per-market, not global** — an AU
                 car sits at ``80xxxx`` while EU cars sit at ``18xxxx``, so
                 "serial -> build date" reasoning does not transfer.
===============  ==========================================================

!!! THE BIG WARNING — READ BEFORE ADDING TRIM LOGIC !!!
------------------------------------------------------
**Positions 4-8 do NOT encode trim, battery, drivetrain or plant.**

``NSGHA`` is stamped on every G6 observed so far, regardless of variant:

    L1NNSGHA1SB000001   G6,      AU, RHD, MY2025, plant B (synthetic serial)
    L1NNSGHA2SB18xxxx   G6,      DE,      MY2025, plant B
    L1NNSGHA1TDxxxxxx   G6 LongRange RWD, MY2026, plant D (Magna Graz)
    L1NNSGHAXTDxxxxxx   G6 LongRange,     MY2026, plant D

Two German owners who ordered LongRange RWD cars received ``NSGHA`` — identical
to the Performance AWD cars in the same thread. Any decoder claiming
"pos 6 G = AWD", "pos 7 H = Performance motor" or "pos 8 A = trim level" is
an LLM hallucination that has been circulating as fact. See
``VDS_TABLE["NSGHA"]["falsified_claims"]``.

Trim/battery/drivetrain are **not in the VIN at all**. Get them from the
manufacturer's own API instead (XPENG: ``POST /biz/v5/vehicle/v2/myVehicles``
returns ``seriesName`` / ``seriesCode`` / ``vehicleTypeCode`` / ``diagramVersion``).

USAGE
-----
    >>> import vin_descriptor
    >>> info = vin_descriptor.decode("L1NNSGHA1SB000001")
    >>> info.model_name, info.model_year, info.plant
    ('XPENG G6', 2025, 'Guangzhou')

    >>> print(vin_descriptor.explain("L1NNSGHA1SB000001"))

CLI:
    python3 custom_components/xpeng/vin_descriptor.py L1NNSGHA1SB000001
    python3 custom_components/xpeng/vin_descriptor.py L1NNSGHA1SB000001 --json
    python3 custom_components/xpeng/vin_descriptor.py --selftest
    python3 custom_components/xpeng/vin_descriptor.py --learn L1NNSGHA1SB000001 \\
            --model "XPENG G6" --source manual

Grow the table from real observations:
    overlay = vin_descriptor.load_overlay("my_overlay.json")   # {"NXXXX": {...}}
    info = vin_descriptor.decode(vin, overlay=overlay)
    vin_descriptor.save_overlay("my_overlay.json",
                                vin_descriptor.learn(vin,
                                                     model_name="XPENG G9")["overlay"])

Author: built for the xpeng-mqtt / HA XPENG integration project.
License: MIT.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping

__version__ = "1.0.0"
TABLE_VERSION = "2026.02.2"

# --------------------------------------------------------------------------- #
# Static tables
# --------------------------------------------------------------------------- #

#: VIN charset per ISO 3779 — I, O and Q are never used.
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

#: World Manufacturer Identifiers seen on XPENG vehicles.
WMI_TABLE: dict[str, dict[str, Any]] = {
    "L1N": {
        "make": "XPENG",
        "make_zh": "小鹏",
        "brand_zh": "小鹏牌",
        "manufacturer": "Zhaoqing XPeng New Energy Investment Co., Ltd.",
        "manufacturer_zh": "肇庆小鹏新能源投资有限公司",
        "manufacturer_address": "广东省广州市黄埔区中新广州知识城鹏行环街1号",
        "region": "China",
        "country_code": "CN",
        "website": "https://www.xiaopeng.com",
        "confidence": "confirmed",
    },
}

#: VDS (positions 4-8) -> model family.
#:
#: ``encodes`` is deliberately narrow. Never widen it to trim/battery without
#: hard evidence — see the module docstring.
#:
#: ``confidence`` values:
#:   confirmed — corroborated by 2+ independent real vehicles, one of which we
#:               physically control or which carries an official model code.
#:   reported  — a single credible source; treat as a strong hint.
VDS_TABLE: dict[str, dict[str, Any]] = {
    "NSGHA": {
        "model_family": "G6",
        "model_name": "XPENG G6",
        "body_style": "SUV",
        "miit_model_code": None,          # not yet observed for the BEV G6
        "powertrain": None,               # deliberately unknown — see below
        "encodes": ["model_family"],
        "confidence": "confirmed",
        "evidence": (
            "AU RHD G6 MY2025 plant B (this project's own vehicle)",
            "Australian ROVER RVD VTA-064130 typical VIN L1NNSGHA9PB044903 "
            "(MY2023, check digit valid)",
            "DE G6 MY2025 plant B (xpeng-fahrer.de thread 3740)",
            "DE G6 LongRange RWD MY2026 plant D (xpeng-fahrer.de thread 3740)",
            "DE G6 LongRange MY2026 plant D (xpeng-fahrer.de thread 3740)",
        ),
        "observed_variants": (
            "LongRange RWD and Performance AWD both carry NSGHA",
            "spans plant B (CN) and plant D (AT)",
            "spans model years 2023, 2025 and 2026",
            "spans LHD and RHD",
            "used by both the Chinese domestic and the Australian export approvals",
        ),
        "falsified_claims": (
            "pos 6 'G' = AWD / dual motor",
            "pos 7 'H' = high-performance motor",
            "pos 8 'A' = trim / equipment level",
            "pos 5 'S' = body style",
        ),
    },
    "NSGHB": {
        "model_family": "G9",
        "model_name": "XPENG G9",
        "body_style": "SUV",
        "miit_model_code": None,          # no MIIT announcement observed for this VDS
        "powertrain": None,               # unknown — positions 4-8 do not encode it
        "encodes": ["model_family"],
        "confidence": "reported",         # one vehicle, owner-reported
        "evidence": (
            "EU G9 AWD Performance (Black Edition), MY2025, plant D (Magna Graz), "
            "reported by its owner through the issue tracker",
            "the API's own vehicleTypeName for that VIN is `E38b` (recorded as "
            "observed; what E38 designates is not established here)",
            "sits in the NSGH* series beside the confirmed NSGHA (G6) and NSGHC (X9)",
        ),
        "observed_variants": (
            "one vehicle so far: AWD Performance, 93.1 kWh pack, tow bar — recorded "
            "as an observation, not as something the VDS encodes",
        ),
        "caveat": (
            "no MIIT or ROVER type approval has been seen for this VDS yet, and the "
            "G9 ships at least four packs — none of that is separable from positions "
            "4-8, so keep the pack size a setting (see usable_battery_kwh)"
        ),
    },
    "SPGHB": {
        "model_family": "P7",
        "model_name": "XPENG P7",
        "body_style": "Sedan",
        "body_style_zh": "轿车",
        "miit_model_code": "NHQ7000BEVEG",
        "powertrain": "BEV",
        "drive": "RWD",
        "encodes": ["model_family"],
        "confidence": "confirmed",
        "evidence": (
            "L1NSPGHBXNA145934 decoded by vinstate.com from the MIIT 公告 DB",
            "wheelbase 2998 mm, 4880x1896x1450, 5 seats, 480 km range",
        ),
    },
    "NSGHC": {
        "model_family": "X9",
        "model_name": "XPENG X9",
        "body_style": "MPV",
        "miit_model_code": None,
        "powertrain": "BEV",
        "encodes": ["model_family"],
        "confidence": "confirmed",
        "evidence": (
            "Australian ROVER RVD VTA-066164 (XPENG X9, works model 'H93'): "
            "typical VIN L1NNSGHCXRB123456",
        ),
        "observed_variants": (
            "the X9 nameplate appears under TWO VDS codes: NSGHC (BEV, AU) and "
            "BSHTP (EREV, CN) — the VDS tracks the type approval, not the nameplate",
        ),
    },
    "BSHTP": {
        "model_family": "X9",
        "model_name": "XPENG X9 (EREV)",
        "body_style": "MPV",
        "body_style_zh": "多用途乘用车",
        "miit_model_code": "NHQ6530SHEVHA",
        "powertrain": "EREV",
        "powertrain_zh": "插电式增程混合动力",
        "drive": "RWD",
        "encodes": ["model_family"],
        "confidence": "reported",
        "evidence": (
            "L1NBSHTP8TB041733 decoded by vinstate.com from the MIIT 公告 DB",
            "MIIT announcement batch 398",
            "1.5 L range extender (1499 ml, 110 kW), 345 km EV range, 2750 kg",
        ),
        "caveat": (
            "Australia's ROVER approval for the X9 (VTA-066164) lists VDS NSGHC, "
            "not BSHTP. Same nameplate, different type approval — do not treat "
            "BSHTP and NSGHC as interchangeable."
        ),
    },
    "BSRTY": {
        "model_family": None,             # announcement code only, no trade name
        "model_name": None,
        "body_style": "MPV/SUV (M1, MIIT 65xx series)",
        "miit_model_code": "NHQ6510REEVRB",
        "powertrain": "EREV",
        "encodes": ["model_family"],
        "confidence": "confirmed",
        "evidence": (
            "L1NBSRTY2TB025574 decoded by vinstate.com from the MIIT 公告 DB",
            "range extender from Harbin Dongan Auto Engine Co.",
        ),
    },
}

#: XPENG type approvals published on Australia's ROVER portal
#: (rover.infrastructure.gov.au). These are the only two XPENG approvals that
#: exist there — there is no G9 and no P7.
#:
#: IMPORTANT: ROVER publishes exactly ONE sample "typical VIN" per approval and
#: does NOT publish a VIN position/VDS breakdown table. There is no
#: position-to-variant mapping to scrape anywhere on that portal. Do not go
#: looking for one again.
ROVER_APPROVALS: tuple[dict[str, Any], ...] = (
    {
        "approval": "VTA-064130",
        "record_id": "ef96edf9-d55a-ef11-bfe2-000d3a6a956b",
        "make": "XPeng",
        "model": "G6",
        "marketing_designation": "G6",
        "category": "M / MA - Passenger Vehicle",
        "approval_holder": "Guangzhou Xiaopeng Motors Technology Co. Ltd",
        "typical_vin": "L1NNSGHA9PB044903",
        "vds": "NSGHA",
        "check_digit_valid": True,
        "approval_versions": (
            "20240815: Standard, Long range",
            "20251030: Standard, Long range, B0A0 D3**0A_2WD, B0B0 E4**0A_AWD",
            "20260116: Standard, Long range, B0A0 D3**0A_2WD, B0B0 E4**0A_AWD, "
            "B0A0 C2*B0A 2WD (latest)",
        ),
        #: Maximum net power per variant as published in the RVD engine rows.
        #: This is what makes VIN -> variant possible via the RAV register.
        "engine_kw": (190, 210),
        "variant_by_power_kw": {190: "Standard", 210: "Long range"},
        "note": (
            "typical VIN passes the check digit and its positions 1-8 match a "
            "real AU-market G6 (L1NNSGHA0SB126544), so the prefix is genuine"
        ),
    },
    {
        "approval": "VTA-066164",
        "record_id": "b57e2e9c-994e-f111-bec5-70a8a554a69c",
        "make": "XPENG",
        "model": "X9",
        "works_model": "H93",
        "marketing_designation": "X9",
        "category": "M / MA - Passenger Vehicle",
        "approval_holder": "Guangzhou Xiaopeng Motors Technology Co. Ltd",
        "typical_vin": "L1NNSGHCXRB123456",
        "vds": "NSGHC",
        "check_digit_valid": False,
        "variants": (
            "C21A0A_2WD SR",
            "D21A0A_2WD LR",
            "E31A0A_AWD LR",
            "F31A0A_AWD LR",
        ),
        "engine_kw": (255, 395),
        "variant_by_power_kw": {
            255: "2WD (SR or LR — power does not separate them)",
            395: "AWD LR",
        },
        "note": (
            "typical VIN FAILS the check digit — serial 123456 is a placeholder, "
            "so this row is illustrative, not a real vehicle"
        ),
    },
)

#: Positions 4-8 are a *family* key. Nothing else is derivable from them.
VDS_DOES_NOT_ENCODE: tuple[str, ...] = (
    "trim",
    "battery",
    "battery capacity",
    "drivetrain / AWD vs RWD",
    "motor power",
    "plant",
    "model year",
)

#: Position 11 -> plant. Confidence is recorded honestly.
PLANT_TABLE: dict[tuple[str, str], dict[str, Any]] = {
    ("L1N", "A"): {
        "plant": "Zhaoqing",
        "plant_zh": "肇庆",
        "country": "China",
        "country_code": "CN",
        "confidence": "reported",
        "evidence": "XPeng owners' forum consensus for the older Zhaoqing works",
    },
    ("L1N", "B"): {
        "plant": "Guangzhou",
        "plant_zh": "广州",
        "country": "China",
        "country_code": "CN",
        "confidence": "reported",
        "evidence": (
            "owners' forum reporting; consistent with every CN-built sample here "
            "(AU G6 MY2025, DE G6 MY2025, X9 EREV MY2026)"
        ),
    },
    ("L1N", "D"): {
        "plant": "Graz (Magna Steyr)",
        "plant_zh": "格拉茨（麦格纳斯太尔）",
        "country": "Austria",
        "country_code": "AT",
        "confidence": "confirmed",
        "evidence": (
            "multiple DE/AT owners received plant-D G6/G9 and confirmed build at "
            "Magna Steyr Graz; a Magna build plate photo was posted"
        ),
    },
}

#: ISO 3779 model-year codes. I, O, Q, U and Z are skipped; 1980 == 'A'.
_YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"
_YEAR_BASE = 1980
_YEAR_CYCLE = 30

#: ISO 3779 / GB 16735 check-digit transliteration and positional weights.
_CHECK_TRANSLIT = {
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}
_CHECK_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)

#: Real VINs with independently known vehicles. Used as test vectors and as
#: documentation of how the table was derived. The first entry is a synthetic
#: stand-in of the same shape (the project's own VIN is not published here).
SEED_OBSERVATIONS: tuple[dict[str, Any], ...] = (
    {
        "vin": "L1NNSGHA1SB000001",
        "model": "XPENG G6",
        "market": "AU",
        "steering": "RHD",
        "my": 2025,
        "plant": "B",
        "source": "synthetic placeholder (AU G6 shape; check digit valid)",
    },
    {
        "vin": "L1NNSGHA2SB18XXXX",
        "model": "XPENG G6",
        "market": "DE",
        "my": 2025,
        "plant": "B",
        "redacted": True,
        "source": "xpeng-fahrer.de/t/aufbau-vin-facelift-modell/3740",
    },
    {
        "vin": "L1NNSGHA1TDXXXXXX",
        "model": "XPENG G6 LongRange RWD",
        "market": "DE",
        "my": 2026,
        "plant": "D",
        "redacted": True,
        "source": "xpeng-fahrer.de/t/aufbau-vin-facelift-modell/3740",
    },
    {
        "vin": "L1NNSGHAXTDXXXXXX",
        "model": "XPENG G6 LongRange",
        "market": "DE",
        "my": 2026,
        "plant": "D",
        "redacted": True,
        "source": "xpeng-fahrer.de/t/aufbau-vin-facelift-modell/3740",
    },
    {
        "vin": "L1NNSGHA0SB126544",
        "model": "XPENG G6 Long range",
        "market": "AU",
        "steering": "RHD",
        "my": 2025,
        "plant": "B",
        "source": "real AU G6; RAV register resolves it to VTA-064130, 210 kW, build 04/2025",
    },
    {
        "vin": "L1NNSGHA9PB044903",
        "model": "XPENG G6",
        "market": "AU",
        "my": 2023,
        "plant": "B",
        "source": "Australian ROVER RVD VTA-064130, typical VIN",
    },
    {
        "vin": "L1NNSGHCXRB123456",
        "model": "XPENG X9 (works model H93)",
        "market": "AU",
        "my": 2024,
        "plant": "B",
        "synthetic": True,
        "source": "Australian ROVER RVD VTA-066164, typical VIN (placeholder serial)",
    },
    {
        "vin": "L1NSPGHBXNA145934",
        "model": "XPENG P7 2022 480G",
        "my": 2022,
        "plant": "A",
        "miit": "NHQ7000BEVEG",
        "source": "vinstate.com",
    },
    {
        "vin": "L1NBSHTP8TB041733",
        "model": "XPENG X9 (EREV)",
        "my": 2026,
        "plant": "B",
        "miit": "NHQ6530SHEVHA",
        "source": "vinstate.com",
    },
    {
        "vin": "L1NBSRTY2TB025574",
        "model": "EREV (MIIT NHQ6510REEVRB)",
        "my": 2026,
        "plant": "B",
        "miit": "NHQ6510REEVRB",
        "source": "vinstate.com",
    },
)


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VinInfo:
    """Decoded view of an XPENG VIN. Immutable; use :meth:`to_dict` for JSON."""

    vin: str
    valid: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    wmi: str = ""
    make: str | None = None
    make_zh: str | None = None
    manufacturer: str | None = None
    manufacturer_zh: str | None = None
    region: str | None = None

    vds: str = ""
    check_digit_char: str = ""
    check_digit_expected: str | None = None
    check_digit_valid: bool | None = None

    model_year_code: str = ""
    model_year: int | None = None

    plant_code: str = ""
    plant: str | None = None
    plant_country: str | None = None
    plant_confidence: str | None = None

    serial: str = ""

    model_family: str | None = None
    model_name: str | None = None
    body_style: str | None = None
    powertrain: str | None = None
    miit_model_code: str | None = None
    vds_confidence: str | None = None
    vds_evidence: tuple[str, ...] = ()
    vds_encodes: tuple[str, ...] = ()
    vds_source: str = "unknown"  # "builtin" | "overlay" | "unknown"

    table_version: str = TABLE_VERSION

    # -- convenience ------------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable dict (tuples become lists)."""
        out: dict[str, Any] = {}
        for key, value in self.__dict__.items():
            out[key] = list(value) if isinstance(value, tuple) else value
        return out

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @property
    def display_name(self) -> str:
        """Best human label available, degrading gracefully."""
        if self.model_name:
            return self.model_name
        if self.model_family:
            return f"XPENG {self.model_family}"
        if self.miit_model_code:
            return f"{self.make or 'XPENG'} ({self.miit_model_code})"
        return f"{self.make or 'XPENG'} (unmapped VDS {self.vds or '?'})"

    @property
    def confidence(self) -> str:
        """Weakest link in the chain: vds confidence, else plant, else unknown."""
        return self.vds_confidence or self.plant_confidence or "unknown"

    def __str__(self) -> str:
        return self.display_name


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #


def normalise(vin: str) -> str:
    """Strip whitespace/dashes and upper-case. Do not use for validation."""
    return re.sub(r"[\s\-_.]", "", str(vin or "")).upper()


def is_valid_format(vin: str) -> bool:
    """True if the VIN is 17 chars from the ISO 3779 charset (no I/O/Q)."""
    return bool(VIN_RE.match(normalise(vin)))


def check_digit(vin: str) -> str | None:
    """Compute the ISO 3779 / GB 16735 mod-11 check digit, or None if uncomputable.

    XPENG uses the standard scheme — verified against every VIN in
    :data:`SEED_OBSERVATIONS`, including the 'X' (remainder 10) case.
    """
    vin = normalise(vin)
    if not is_valid_format(vin):
        return None
    total = 0
    for char, weight in zip(vin, _CHECK_WEIGHTS):
        value = _CHECK_TRANSLIT.get(char)
        if value is None:
            return None
        total += value * weight
    remainder = total % 11
    return "X" if remainder == 10 else str(remainder)


def check_digit_ok(vin: str) -> bool | None:
    """True/False if computable, None if the VIN is malformed."""
    expected = check_digit(vin)
    if expected is None:
        return None
    return expected == normalise(vin)[8]


def model_year_from_code(code: str, reference_year: int | None = None) -> int | None:
    """Resolve a position-10 year code to a full year.

    The ISO 3779 cycle repeats every 30 years, so a reference year is needed to
    disambiguate; defaults to the current year.

    Resolution prefers the **latest year not more than one year ahead of the
    reference** — vehicles are not built a decade into the future, so a bare
    "nearest candidate" rule would wrongly read year code ``A`` as 2040 instead
    of 2010. The one-year lookahead accommodates genuine next-model-year
    vehicles. Only if no candidate qualifies does it fall back to the earliest.
    """
    code = (code or "").upper()
    if len(code) != 1 or code not in _YEAR_CODES:
        return None
    index = _YEAR_CODES.index(code)
    ref = reference_year or date.today().year
    # Candidates span four cycles, which comfortably covers any plausible VIN.
    candidates = [_YEAR_BASE + index + _YEAR_CYCLE * k for k in range(4)]
    in_the_past = [year for year in candidates if year <= ref + 1]
    return max(in_the_past) if in_the_past else min(candidates)


def mask_vin(vin: str, keep_prefix: int = 11) -> str:
    """Mask the serial so a VIN can be shared in logs/issues.

    Keeps WMI + VDS + check digit + model year + plant and masks positions
    12-17, which are the uniquely identifying part::

        L1NNSGHA1SB000001 -> L1NNSGHA1SB******
    """
    vin = normalise(vin)
    if len(vin) <= keep_prefix:
        return vin
    return vin[:keep_prefix] + "*" * (len(vin) - keep_prefix)


# --------------------------------------------------------------------------- #
# Decode
# --------------------------------------------------------------------------- #


def decode(
    vin: str,
    *,
    reference_year: int | None = None,
    overlay: Mapping[str, Mapping[str, Any]] | None = None,
    strict_check_digit: bool = False,
) -> VinInfo:
    """Decode an XPENG VIN.

    Args:
        vin: the VIN (whitespace/dashes tolerated, case-insensitive).
        reference_year: used to disambiguate the 30-year model-year cycle.
        overlay: extra/overriding VDS entries, e.g. from :func:`load_overlay`.
            Same shape as :data:`VDS_TABLE`.
        strict_check_digit: when True, a bad check digit sets ``valid=False``
            instead of merely adding a warning. Default is lenient because some
            markets/manufacturers legitimately deviate.

    Returns:
        :class:`VinInfo`. ``valid`` reflects *format*, not whether the model was
        recognised — an unmapped VDS is still a well-formed VIN.
    """
    raw = str(vin or "")
    vin = normalise(raw)

    errors: list[str] = []
    warnings: list[str] = []

    if not vin:
        return VinInfo(vin="", valid=False, errors=("empty VIN",))
    if len(vin) != 17:
        errors.append(f"expected 17 characters, got {len(vin)}")
    if not is_valid_format(vin):
        errors.append("invalid characters (I, O and Q are not allowed in a VIN)")

    # Pad so partial VINs still yield whatever is decodable without blowing up.
    padded = (vin + "*" * 17)[:17]
    wmi, vds = padded[0:3], padded[3:8]
    check_char = padded[8]
    year_code, plant_code, serial = padded[9], padded[10], padded[11:17]

    # -- check digit ------------------------------------------------------- #
    expected = check_digit(padded) if is_valid_format(padded) else None
    cd_ok: bool | None = None
    if expected is not None:
        cd_ok = expected == check_char
        if not cd_ok:
            msg = f"check digit mismatch: got {check_char!r}, expected {expected!r}"
            (errors if strict_check_digit else warnings).append(msg)

    # -- WMI --------------------------------------------------------------- #
    manufacturer_entry = WMI_TABLE.get(wmi)
    if manufacturer_entry is None:
        if is_valid_format(padded):
            warnings.append(f"WMI {wmi!r} is not a known XPENG identifier")
        manufacturer_entry = {}

    # -- model year -------------------------------------------------------- #
    model_year = model_year_from_code(year_code, reference_year)
    if model_year is None and year_code != "*":
        warnings.append(f"unrecognised model-year code {year_code!r} at position 10")

    # -- plant ------------------------------------------------------------- #
    plant_entry = PLANT_TABLE.get((wmi, plant_code), {})
    if not plant_entry and plant_code != "*":
        warnings.append(f"unknown plant code {plant_code!r} at position 11")

    # -- VDS: overlay wins over the builtin table -------------------------- #
    vds_source = "unknown"
    vds_entry: Mapping[str, Any] = {}
    if overlay and vds in overlay:
        vds_entry, vds_source = overlay[vds], "overlay"
    elif vds in VDS_TABLE:
        vds_entry, vds_source = VDS_TABLE[vds], "builtin"
    else:
        warnings.append(
            f"VDS {vds!r} is not in the table — model unknown. "
            f"Report it (open an issue naming the model), or run "
            f"`vin_descriptor.py --learn <vin> --model <name>` and contribute the overlay."
        )

    if vds_source == "overlay":
        # An overlay that disagrees with the builtin table is worth flagging.
        builtin = VDS_TABLE.get(vds)
        if builtin and vds_entry.get("model_family") != builtin.get("model_family"):
            warnings.append(
                f"overlay overrides builtin VDS {vds!r}: "
                f"{builtin.get('model_family')!r} -> {vds_entry.get('model_family')!r}"
            )

    return VinInfo(
        vin=vin,
        valid=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        wmi=wmi,
        make=manufacturer_entry.get("make"),
        make_zh=manufacturer_entry.get("make_zh"),
        manufacturer=manufacturer_entry.get("manufacturer"),
        manufacturer_zh=manufacturer_entry.get("manufacturer_zh"),
        region=manufacturer_entry.get("region"),
        vds=vds,
        check_digit_char=check_char,
        check_digit_expected=expected,
        check_digit_valid=cd_ok,
        model_year_code=year_code,
        model_year=model_year,
        plant_code=plant_code,
        plant=plant_entry.get("plant"),
        plant_country=plant_entry.get("country"),
        plant_confidence=plant_entry.get("confidence"),
        serial=serial,
        model_family=vds_entry.get("model_family"),
        model_name=vds_entry.get("model_name"),
        body_style=vds_entry.get("body_style"),
        powertrain=vds_entry.get("powertrain"),
        miit_model_code=vds_entry.get("miit_model_code"),
        vds_confidence=vds_entry.get("confidence"),
        vds_evidence=tuple(vds_entry.get("evidence", ())),
        vds_encodes=tuple(vds_entry.get("encodes", ())),
        vds_source=vds_source,
    )


def explain(vin: str, *, mask: bool = True, **kwargs: Any) -> str:
    """Return a human-readable, position-by-position breakdown."""
    info = decode(vin, **kwargs)
    shown = mask_vin(info.vin) if mask else info.vin
    serial_shown = ("*" * len(info.serial)) if mask else info.serial
    lines: list[str] = [
        f"VIN   : {shown}" + ("  (serial masked)" if mask else ""),
        f"Valid : {info.valid}"
        + (f"  errors={list(info.errors)}" if info.errors else ""),
        f"Model : {info.display_name}   [{info.confidence} confidence]",
        "",
        f"  pos 1-3   WMI    {info.wmi}   {info.manufacturer or '?'}"
        + (f" / {info.manufacturer_zh}" if info.manufacturer_zh else ""),
        f"  pos 4-8   VDS    {info.vds}   model family = {info.model_family or '?'}"
        f"  (source: {info.vds_source})",
        f"  pos 9     check  {info.check_digit_char}   "
        + (
            f"expected {info.check_digit_expected} -> "
            f"{'OK' if info.check_digit_valid else 'MISMATCH'}"
            if info.check_digit_valid is not None
            else "not computable"
        ),
        f"  pos 10    year   {info.model_year_code}   {info.model_year or '?'}",
        f"  pos 11    plant  {info.plant_code}   {info.plant or '?'}"
        + (f", {info.plant_country}" if info.plant_country else "")
        + (
            f"  [{info.plant_confidence}]"
            if info.plant_confidence and info.plant_confidence != "confirmed"
            else ""
        ),
        f"  pos 12-17 serial {serial_shown}",
    ]

    if info.body_style or info.powertrain or info.miit_model_code:
        lines.append("")
        if info.body_style:
            lines.append(f"  body style   : {info.body_style}")
        if info.powertrain:
            lines.append(f"  powertrain   : {info.powertrain}")
        if info.miit_model_code:
            lines.append(f"  MIIT 公告 code: {info.miit_model_code}")

    if info.vds_evidence:
        lines.append("")
        lines.append("  Evidence:")
        lines.extend(f"    - {item}" for item in info.vds_evidence)

    lines.append("")
    lines.append(
        "  NOTE: positions 4-8 do NOT encode "
        + ", ".join(VDS_DOES_NOT_ENCODE[:4])
        + "."
    )
    lines.append(
        "        For trim/battery/drivetrain use the manufacturer API "
        "(XPENG: /biz/v5/vehicle/v2/myVehicles -> seriesName)."
    )

    if info.warnings:
        lines.append("")
        lines.append("  Warnings:")
        lines.extend(f"    ! {item}" for item in info.warnings)

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Overlay / learning
# --------------------------------------------------------------------------- #


def load_overlay(path: str) -> dict[str, dict[str, Any]]:
    """Load a VDS overlay table from JSON.

    Accepts either a bare mapping ``{"NSGHA": {...}}`` or a wrapper
    ``{"vds": {"NSGHA": {...}}}`` (the shape :func:`learn` emits).
    """
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict) and "vds" in payload and isinstance(payload["vds"], dict):
        payload = payload["vds"]
    if not isinstance(payload, dict):
        raise ValueError("overlay must be a JSON object of VDS -> entry")
    # Normalise keys to the canonical 5-character VDS form.
    return {normalise(key)[:5]: value for key, value in payload.items()}


def save_overlay(path: str, overlay: Mapping[str, Mapping[str, Any]]) -> None:
    """Write an overlay table to JSON (UTF-8, non-ASCII preserved)."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(dict(overlay), handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def merge(*tables: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Shallow-merge overlay tables left-to-right; later tables win."""
    merged: dict[str, dict[str, Any]] = {}
    for table in tables:
        for vds, entry in table.items():
            merged[vds] = {**merged.get(vds, {}), **entry}
    return merged


def learn(
    vin: str,
    *,
    model_name: str | None = None,
    model_family: str | None = None,
    body_style: str | None = None,
    powertrain: str | None = None,
    miit_model_code: str | None = None,
    drive: str | None = None,
    source: str = "manual observation",
    confidence: str = "reported",
    merge_existing: bool = True,
) -> dict[str, Any]:
    """Build a candidate :data:`VDS_TABLE` entry from an observed VIN.

    This is the "build/maintain our own mapping from observed examples" path.
    It never invents fields: anything you omit stays ``None``, because for XPENG
    the VIN genuinely cannot tell you trim or battery.

    Returns:
        ``{"vds": "NSGHA", "entry": {...}, "overlay": {"NSGHA": {...}},
        "pasted": "<python literal to paste into VDS_TABLE>"}``
    """
    info = decode(vin)
    if not info.valid:
        raise ValueError(
            f"cannot learn from VIN {vin!r}: not a well-formed VIN "
            f"({'; '.join(info.errors) or 'unknown error'})"
        )
    if not info.vds or "*" in info.vds:
        raise ValueError(f"cannot learn from VIN {vin!r}: no usable VDS field")

    existing = VDS_TABLE.get(info.vds, {}) if merge_existing else {}
    evidence = list(existing.get("evidence", ()))
    note = f"{model_name or model_family or 'observed vehicle'} ({source})"
    if note not in evidence:
        evidence.append(note)

    entry: dict[str, Any] = {
        "model_family": model_family or existing.get("model_family"),
        "model_name": model_name or existing.get("model_name"),
        "body_style": body_style or existing.get("body_style"),
        "powertrain": powertrain or existing.get("powertrain"),
        "miit_model_code": miit_model_code or existing.get("miit_model_code"),
        "encodes": list(existing.get("encodes", ["model_family"])),
        "confidence": existing.get("confidence") or confidence,
        "evidence": evidence,
    }
    if drive or existing.get("drive"):
        entry["drive"] = drive or existing.get("drive")

    pasted = (
        f'    "{info.vds}": {{\n'
        f'        "model_family": {entry["model_family"]!r},\n'
        f'        "model_name": {entry["model_name"]!r},\n'
        f'        "body_style": {entry["body_style"]!r},\n'
        f'        "powertrain": {entry["powertrain"]!r},\n'
        f'        "miit_model_code": {entry["miit_model_code"]!r},\n'
        f'        "encodes": {entry["encodes"]!r},\n'
        f'        "confidence": {entry["confidence"]!r},\n'
        f'        "evidence": {tuple(entry["evidence"])!r},\n'
        f'    }},'
    )

    return {
        "vds": info.vds,
        "entry": entry,
        "overlay": {info.vds: entry},
        "pasted": pasted,
    }


#: AU-market variant resolution. ROVER publishes the G6's engines per variant,
#: which lets the RAV register's power figure discriminate variants — the one
#: genuine VIN -> variant path that exists for XPENG. Anything not listed here
#: resolves to no candidates rather than a guess.
#:
#: Values are TUPLES because power alone does not always uniquely identify a
#: variant — the X9's 255 kW 2WD engine covers both the SR and LR variants.
AU_VARIANTS_BY_POWER: dict[str, dict[int, tuple[str, ...]]] = {
    "G6": {
        190: ("Standard",),
        210: ("Long range",),
    },
    "X9": {
        255: ("C21A0A_2WD SR", "D21A0A_2WD LR"),
        395: ("E31A0A_AWD LR", "F31A0A_AWD LR"),
    },
}

#: Australian RAV (Register of Approved Vehicles) public search.
RAV_SEARCH_URL = "https://www.rover.infrastructure.gov.au/RAVPublicSearch"


class RavLookupError(RuntimeError):
    """The RAV register could not be reached or answered with an error."""


# Label sits in a ``qtdiv`` heading; its value is the next ``question-label``
# div. Pairing on the labels is more reliable than on element ids, because RAV
# reuses id="make" for three different rows (Approval Holder, VCC, Vehicle Make).
_RAV_PAIR_RE = re.compile(
    r'class="qtdiv[^"]*"[^>]*>\s*(?P<label>[^<]+?)\s*</div>'
    r".*?class=\"question-label\"[^>]*>\s*(?P<value>.*?)\s*</div>",
    re.DOTALL,
)
_RAV_TAG_RE = re.compile(r"<[^>]+>")
_RAV_WS_RE = re.compile(r"\s+")


def _rav_text(fragment: str) -> str:
    """Strip tags and collapse whitespace from an HTML fragment."""
    text = _RAV_TAG_RE.sub("", fragment)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return _RAV_WS_RE.sub(" ", text).strip()


def _rav_pairs(html: str) -> dict[str, str]:
    """Extract label -> value pairs from a RAV result page."""
    return {
        _rav_text(match.group("label")): _rav_text(match.group("value"))
        for match in _RAV_PAIR_RE.finditer(html)
    }


def au_variants_from_power(model: str | None, power_kw: float | None) -> tuple[str, ...]:
    """Map an AU RAV power figure to the variant(s) it can correspond to.

    Returns an empty tuple when the model is unknown or the power figure does
    not discriminate. Never guesses a single variant when several are possible.
    """
    if not model or power_kw is None:
        return ()
    table = AU_VARIANTS_BY_POWER.get(model.upper())
    if not table:
        return ()
    return table.get(int(round(float(power_kw))), ())


def lookup_rav(vin: str, timeout: float = 25.0) -> dict[str, Any]:
    """Look a VIN up in Australia's Register of Approved Vehicles.

    This is the only way to get anything *beyond* the model family for an
    Australian XPENG: the RAV record carries the build date, GVM/tare, seat
    count and **maximum net power**, and power discriminates the G6's variants
    (190 kW = Standard, 210 kW = Long range). Positions 4-8 cannot do this.

    Requires network access and works for Australian-market vehicles only.
    Uses ``urllib`` from the standard library — the module still has no
    third-party dependencies.

    Args:
        vin: 17-character VIN. Exact match; the register does not support
            wildcards or prefix search.
        timeout: socket timeout in seconds.

    Returns:
        A dict with ``found`` plus, when found, the labelled RAV fields:
        ``build_date``, ``power_kw``, ``seats``, ``gvm_kg``, ``tare_kg``,
        ``miro_kg``, ``make``, ``model``, ``approval_number``, ``approval_id``,
        ``approval_holder``, ``vcc``, ``entry_pathway``, ``rav_date_of_entry``,
        ``nves_vehicle_type``, ``co2_g_km``, and ``variant_candidates`` derived
        from ``AU_VARIANTS_BY_POWER``.

    Raises:
        RavLookupError: the register was unreachable or returned a non-200.
        ValueError: the VIN is not well formed (checked locally first).
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    vin = normalise(vin)
    if not is_valid_format(vin):
        raise ValueError(f"{vin!r} is not a well-formed 17-character VIN")

    url = f"{RAV_SEARCH_URL}?{urllib.parse.urlencode({'vinno': vin})}"
    request = urllib.request.Request(
        url,
        headers={
            # Identify the client honestly; the endpoint is anonymous.
            "User-Agent": f"vin_descriptor/{__version__} (+offline XPENG VIN decoder)",
            "Accept": "text/html",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            html = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        raise RavLookupError(f"RAV returned HTTP {err.code} for {mask_vin(vin)}") from err
    except Exception as err:  # noqa: BLE001 - surface any transport failure
        raise RavLookupError(f"RAV unreachable for {mask_vin(vin)}: {err}") from err

    pairs = _rav_pairs(html)

    def pick(*labels: str) -> str | None:
        for label in labels:
            value = pairs.get(label)
            if value:
                return value
        return None

    def number(*labels: str) -> float | None:
        raw = pick(*labels)
        if raw is None:
            return None
        match = re.search(r"-?\d+(?:\.\d+)?", raw.replace(",", ""))
        return float(match.group()) if match else None

    approval_number = pick("Approval Number")
    # Do NOT sniff the page for a "does not contain a record" message: that text
    # ships in every response inside an HTML-escaped client-side JS template for
    # the empty state. Presence of the approval number is the reliable signal —
    # the escaped template uses &quot;class=&quot;question-label&quot;&quot;, which the
    # unescaped regex above cannot match.
    found = bool(approval_number)

    if not found:
        return {"vin": vin, "found": False, "source": RAV_SEARCH_URL}

    model = pick("Vehicle Model")
    power = number("Power")
    approval_id = None
    id_match = re.search(
        r'href="/PublishedApprovals/VTADetails/\?id=([0-9a-f\-]{36})"', html
    )
    if id_match:
        approval_id = id_match.group(1)

    return {
        "vin": vin,
        "found": True,
        "source": RAV_SEARCH_URL,
        "approval_number": approval_number,
        "approval_id": approval_id,
        "approval_holder": pick("Approval Holder"),
        "vcc": pick("VCC"),
        "make": pick("Vehicle Make"),
        "model": model,
        "entry_pathway": pick("Entry Pathway Sub-Category"),
        "rav_date_of_entry": pick("RAV Date of Entry"),
        "authorised_by": pick("Authorised By Name"),
        "build_date": pick("Build Date"),
        "gvm_kg": number("GVM/ATM (kg)"),
        "tare_kg": number("Tare"),
        "miro_kg": number("Mass In Running Order (kg)"),
        "power_kw": power,
        "seats": number("Seats"),
        "nves_vehicle_type": pick("NVES Vehicle Type"),
        "co2_g_km": number("Carbon Dioxide Emissions (g/km)"),
        "variant_candidates": list(au_variants_from_power(model, power)),
    }


# --------------------------------------------------------------------------- #
# Bulk helpers
# --------------------------------------------------------------------------- #


def decode_many(vins: Iterable[str], **kwargs: Any) -> list[VinInfo]:
    """Decode many VINs, preserving order."""
    return [decode(vin, **kwargs) for vin in vins]


def group_by_vds(vins: Iterable[str], **kwargs: Any) -> dict[str, list[str]]:
    """Group (masked) VINs by VDS — useful for spotting unknown families."""
    grouped: dict[str, list[str]] = {}
    for info in decode_many(vins, **kwargs):
        key = info.vds or "?"
        grouped.setdefault(key, []).append(mask_vin(info.vin))
    return grouped


def unknown_vds(vins: Iterable[str], **kwargs: Any) -> list[str]:
    """Return the VDS keys present in ``vins`` that the table cannot resolve."""
    return sorted({i.vds for i in decode_many(vins, **kwargs) if i.vds_source == "unknown"})


# --------------------------------------------------------------------------- #
# Self-test / CLI
# --------------------------------------------------------------------------- #


def selftest(verbose: bool = True) -> bool:
    """Verify the check-digit algorithm and table against real observations."""
    failures: list[str] = []

    def check(condition: bool, label: str) -> None:
        if not condition:
            failures.append(label)
        if verbose:
            print(f"  [{'PASS' if condition else 'FAIL'}] {label}")

    if verbose:
        print(f"vin_descriptor {__version__} selftest (table {TABLE_VERSION})")

    # 1. Check digit must validate on every *complete* seed VIN.
    for observation in SEED_OBSERVATIONS:
        vin = observation["vin"]
        if observation.get("redacted") or observation.get("synthetic"):
            continue
        if not is_valid_format(vin):
            continue
        check(check_digit_ok(vin) is True,
              f"check digit valid for {mask_vin(vin)}")

    # 1b. ...and must REJECT redacted or placeholder VINs. 'X' is a legal VIN
    # character, so these pass format validation and are caught only by the
    # check digit. This is exactly how the ROVER X9 sample was identified as a
    # synthetic placeholder rather than a real vehicle.
    for observation in SEED_OBSERVATIONS:
        vin = observation["vin"]
        if not (observation.get("redacted") or observation.get("synthetic")):
            continue
        check(check_digit_ok(vin) is False,
              f"check digit rejects placeholder VIN {mask_vin(vin)}")

    # 1c. ROVER's G6 typical VIN is a real vehicle and corroborates NSGHA = G6.
    for approval in ROVER_APPROVALS:
        expected_ok = approval["check_digit_valid"]
        label = f"ROVER {approval['approval']} typical VIN check digit"
        if expected_ok:
            check(check_digit_ok(approval["typical_vin"]) is True, label)
        else:
            check(check_digit_ok(approval["typical_vin"]) is False, label)

    # 2. The 'X' (remainder 10) branch must be exercised.
    info = decode("L1NSPGHBXNA145934")
    check(info.check_digit_char == "X" and info.check_digit_valid is True,
          "remainder-10 check digit resolves to 'X'")

    # 3. The synthetic stand-in for our own car.
    ours = decode("L1NNSGHA1SB000001")
    check(ours.valid, "project VIN decodes as valid")
    check(ours.model_family == "G6", "project VIN -> G6")
    check(ours.model_year == 2025, "project VIN -> MY2025")
    check(ours.plant == "Guangzhou", "project VIN -> plant B Guangzhou")
    check(ours.wmi == "L1N" and ours.make == "XPENG", "project VIN -> WMI L1N / XPENG")
    check(ours.serial == "000001", "project VIN -> serial extracted")
    check(ours.check_digit_valid is True, "project VIN check digit valid")

    # 4. The falsification evidence must stay in the table.
    nsgha = VDS_TABLE["NSGHA"]
    check("falsified_claims" in nsgha and len(nsgha["falsified_claims"]) >= 3,
          "NSGHA keeps the falsified-claim warnings")
    check(nsgha["encodes"] == ["model_family"],
          "NSGHA claims to encode model_family only")

    # 5. Model year disambiguation.
    check(model_year_from_code("S", 2026) == 2025, "year code S -> 2025")
    check(model_year_from_code("T", 2026) == 2026, "year code T -> 2026")
    check(model_year_from_code("N", 2026) == 2022, "year code N -> 2022")
    check(model_year_from_code("I", 2026) is None, "year code I is invalid")
    check(model_year_from_code("A", 2026) == 2010,
          "year code A -> 2010, not a future year")
    check(model_year_from_code("Y", 2026) == 2000, "year code Y -> 2000")

    # 6. Malformed input degrades gracefully instead of raising.
    check(decode("").valid is False, "empty VIN is invalid")
    check(decode("TOOSHORT").valid is False, "short VIN is invalid")
    check(decode("L1NNSGHA1SB00000I").valid is False, "VIN with 'I' is invalid")
    check(decode("L1NXXXXX3SB000001").valid, "unknown VDS still valid format")
    check("not in the table" in " ".join(decode("L1NXXXXX3SB000001").warnings),
          "unknown VDS warns rather than guesses")

    # 7. Serial masking.
    check(mask_vin("L1NNSGHA1SB000001") == "L1NNSGHA1SB******", "mask_vin hides serial")

    # 8. Overlay precedence.
    overlay = {"NSGHA": {"model_family": "G6-test", "model_name": "Overridden",
                         "confidence": "reported"}}
    overridden = decode("L1NNSGHA1SB000001", overlay=overlay)
    check(overridden.model_name == "Overridden", "overlay overrides builtin entry")
    check(overridden.vds_source == "overlay", "overlay source is reported")
    check(any("overrides builtin" in w for w in overridden.warnings),
          "overlay/builtin disagreement warns")

    # 9. learn() must not fabricate fields.
    learned = learn("L1NXXXXX3SB000001", model_name="XPENG Test", source="selftest")
    check(learned["entry"]["powertrain"] is None, "learn() does not invent powertrain")
    check(learned["entry"]["miit_model_code"] is None, "learn() does not invent MIIT code")

    # 10. JSON round-trip.
    check(json.loads(ours.to_json())["vin"] == ours.vin, "to_json round-trips")

    if verbose:
        print(f"\n{'ALL PASS' if not failures else str(len(failures)) + ' FAILURE(S)'}")
        for failure in failures:
            print(f"  - {failure}")
    return not failures


def _main(argv: list[str]) -> int:
    args = list(argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--selftest":
        return 0 if selftest() else 1

    as_json = "--json" in args
    args = [a for a in args if a != "--json"]

    if args[0] == "--learn":
        if len(args) < 2:
            print("--learn requires a VIN", file=sys.stderr)
            return 2
        vin = args[1]
        options = dict(zip(args[2::2], args[3::2]))
        result = learn(
            vin,
            model_name=options.get("--model"),
            model_family=options.get("--family"),
            body_style=options.get("--body"),
            powertrain=options.get("--powertrain"),
            miit_model_code=options.get("--miit"),
            source=options.get("--source", "manual observation"),
        )
        print(result["pasted"])
        return 0

    exit_code = 0
    for vin in args:
        info = decode(vin)
        if as_json:
            print(info.to_json())
        else:
            print(explain(vin))
            print()
        if not info.valid:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
