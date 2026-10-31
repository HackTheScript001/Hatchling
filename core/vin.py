"""VIN decoding: maker, country and model year (ISO 3779 / 49 CFR 565)."""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Optional

# World manufacturer identifiers for the Ford family (first 3 characters).
WMI = {
    "1FA": ("Ford", "USA"), "1FB": ("Ford", "USA"), "1FC": ("Ford", "USA"), "1FD": ("Ford", "USA"),
    "1FM": ("Ford", "USA"), "1FT": ("Ford", "USA"), "1FU": ("Ford", "USA"), "1FV": ("Ford", "USA"),
    "1ZV": ("Ford", "USA (AutoAlliance)"), "2FA": ("Ford", "Canada"), "2FM": ("Ford", "Canada"),
    "2FT": ("Ford", "Canada"), "3FA": ("Ford", "Mexico"), "3FM": ("Ford", "Mexico"), "3FT": ("Ford", "Mexico"),
    "3FE": ("Ford", "Mexico"), "WF0": ("Ford", "Germany"), "WF1": ("Ford", "Germany"), "SFA": ("Ford", "UK"),
    "VS6": ("Ford", "Spain"), "NM0": ("Ford", "Turkey"), "MAJ": ("Ford", "India"), "MNB": ("Ford", "Thailand"),
    "6FP": ("Ford", "Australia"), "LVS": ("Ford", "China (Changan Ford)"), "9BF": ("Ford", "Brazil"),
    "PR8": ("Ford", "Malaysia"), "AFA": ("Ford", "South Africa"), "X9F": ("Ford", "Russia"),
    "1LN": ("Lincoln", "USA"), "2LM": ("Lincoln", "Canada"), "3LN": ("Lincoln", "Mexico"), "5LM": ("Lincoln", "USA"),
    "LJX": ("Lincoln", "China"),
    "1ME": ("Mercury", "USA"), "2ME": ("Mercury", "Canada"), "3ME": ("Mercury", "Mexico"), "4M2": ("Mercury", "USA"),
    "JM1": ("Mazda", "Japan"), "JM3": ("Mazda", "Japan"), "JMZ": ("Mazda", "Japan (Europe spec)"),
    "4F2": ("Mazda", "USA"), "4F4": ("Mazda", "USA"), "3MZ": ("Mazda", "Mexico"), "3MV": ("Mazda", "Mexico"),
}

YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"  # 1980..2009, then repeats from 2010

_TRANSLIT = {**{str(d): d for d in range(10)},
             **dict(zip("ABCDEFGH", range(1, 9))), **dict(zip("JKLMN", range(1, 6))), "P": 7, "R": 9,
             **dict(zip("STUVWXYZ", range(2, 10)))}
_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


@dataclass
class VinInfo:
    vin: str
    make: str
    country: str
    year: Optional[int]
    check_digit_ok: Optional[bool]   # None where the check digit isn't required (most non-NA VINs)

    @property
    def summary(self) -> str:
        y = f"{self.year} " if self.year else ""
        return f"{y}{self.make} ({self.country})"


def model_year(vin: str, today: Optional[datetime.date] = None) -> Optional[int]:
    c = vin[9].upper()
    if c not in YEAR_CODES:
        return None
    idx = YEAR_CODES.index(c)
    year_now = (today or datetime.date.today()).year
    # the 30-year cycle repeats; pick the newest year that isn't more than a year ahead
    candidates = [1980 + idx + 30 * k for k in range(4)]
    valid = [y for y in candidates if y <= year_now + 1]
    if not valid:
        return None
    # North American VINs: a letter in position 7 means 2010+ cycle, a digit means 1980-2009
    if vin[0] in "12345" and vin[6].isalpha():
        later = [y for y in valid if y >= 2010]
        return later[-1] if later else valid[-1]
    if vin[0] in "12345" and vin[6].isdigit():
        earlier = [y for y in valid if y < 2010]
        return earlier[-1] if earlier else valid[-1]
    return valid[-1]


def check_digit_ok(vin: str) -> Optional[bool]:
    try:
        total = sum(_TRANSLIT[c] * w for c, w in zip(vin.upper(), _WEIGHTS))
    except KeyError:
        return False
    expect = total % 11
    return vin[8].upper() == ("X" if expect == 10 else str(expect))


def decode(vin: str) -> Optional[VinInfo]:
    vin = (vin or "").strip().upper()
    if len(vin) != 17 or any(c in "IOQ" for c in vin):
        return None
    make, country = WMI.get(vin[:3], (None, None))
    if make is None:
        return VinInfo(vin, "Unknown make", vin[:3], model_year(vin) if vin[0] in "12345" else None, None)
    na = vin[0] in "12345"
    # Only North American VINs are required to carry the model year in position 10.
    return VinInfo(vin, make, country, model_year(vin) if na else None, check_digit_ok(vin) if na else None)
