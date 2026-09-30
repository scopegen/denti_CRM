"""Tooth numbers in FDI notation, the standard in India: adult teeth 11 to 48, milk teeth 51 to 85."""

ADULT_TEETH = frozenset(q * 10 + p for q in (1, 2, 3, 4) for p in range(1, 9))
MILK_TEETH = frozenset(q * 10 + p for q in (5, 6, 7, 8) for p in range(1, 6))
ALL_TEETH = ADULT_TEETH | MILK_TEETH


def check_teeth(teeth: list[int]) -> list[int]:
    """Sorted, without repeats. Raises ValueError for a number that is not a tooth."""
    unknown = [t for t in teeth if t not in ALL_TEETH]
    if unknown:
        raise ValueError(f"{unknown[0]} is not a tooth number (FDI numbering, 11 to 48 or 51 to 85).")
    return sorted(set(teeth))
