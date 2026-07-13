"""South African ID number validation (13 digits, Luhn checksum)."""
from datetime import date


def validate_sa_id(value: str):
    """Return (ok, error_message_or_None, dob_or_None)."""
    if not value:
        return False, "ID number is required.", None
    s = "".join(ch for ch in value if ch.isdigit())
    if len(s) != 13:
        return False, "ID number must be exactly 13 digits.", None
    # YYMMDD prefix
    yy, mm, dd = int(s[0:2]), int(s[2:4]), int(s[4:6])
    today = date.today()
    century = 2000 if (2000 + yy) <= today.year else 1900
    try:
        dob = date(century + yy, mm, dd)
    except ValueError:
        return False, "ID number contains an invalid date of birth.", None
    # Luhn check
    total = 0
    for i, ch in enumerate(s):
        n = int(ch)
        if i % 2 == 0:
            total += n
        else:
            d = n * 2
            total += d if d < 10 else d - 9
    if total % 10 != 0:
        return False, "ID number checksum is invalid.", None
    return True, None, dob