# ── Greek letter rank system ──────────────────────────────────────────────────
# Ranks rise exponentially. After Omega, combinations begin (Alpha-Alpha, etc.)
# Displayed as Greek characters e.g. α, β, α-α

GREEK_LETTERS = [
    ("Alpha", "α"),
    ("Beta", "β"),
    ("Gamma", "γ"),
    ("Delta", "δ"),
    ("Epsilon", "ε"),
    ("Zeta", "ζ"),
    ("Eta", "η"),
    ("Theta", "θ"),
    ("Iota", "ι"),
    ("Kappa", "κ"),
    ("Lambda", "λ"),
    ("Mu", "μ"),
    ("Nu", "ν"),
    ("Xi", "ξ"),
    ("Omicron", "ο"),
    ("Pi", "π"),
    ("Rho", "ρ"),
    ("Sigma", "σ"),
    ("Tau", "τ"),
    ("Upsilon", "υ"),
    ("Phi", "φ"),
    ("Chi", "χ"),
    ("Psi", "ψ"),
    ("Omega", "ω"),
]

BASE = 24  # number of Greek letters
EXP_BASE = 1.4  # exponential growth factor
EXP_SCALE = 50  # base points for rank 1


def _threshold(rank_index: int) -> int:
    """Points required to reach rank at given 0-based index."""
    if rank_index == 0:
        return 0
    return int(EXP_SCALE * (EXP_BASE**rank_index))


def _rank_name(rank_index: int) -> str:
    """Return the English name for a rank index (e.g. 0=Alpha, 24=Alpha-Alpha)."""
    if rank_index < BASE:
        return GREEK_LETTERS[rank_index][0]

    # Build combination name by treating rank_index as a base-24 number
    parts = []
    n = rank_index
    while n >= 0:
        parts.append(GREEK_LETTERS[n % BASE][0])
        n = n // BASE - 1
        if n < 0:
            break
    return "-".join(reversed(parts))


def _rank_symbol(rank_index: int) -> str:
    """Return the Greek character(s) for a rank index (e.g. 0=α, 24=α-α)."""
    if rank_index < BASE:
        return GREEK_LETTERS[rank_index][1]

    parts = []
    n = rank_index
    while n >= 0:
        parts.append(GREEK_LETTERS[n % BASE][1])
        n = n // BASE - 1
        if n < 0:
            break
    return "-".join(reversed(parts))


def get_rank(points: int) -> dict:
    """
    Return the current rank for a given points total.
    Returns dict with: index, name, symbol, threshold, next_threshold, progress_pct
    """
    index = 0
    while _threshold(index + 1) <= points:
        index += 1

    current_threshold = _threshold(index)
    next_threshold = _threshold(index + 1)
    progress = points - current_threshold
    needed = next_threshold - current_threshold
    progress_pct = min(100, int((progress / needed) * 100)) if needed > 0 else 100

    return {
        "index": index,
        "name": _rank_name(index),
        "symbol": _rank_symbol(index),
        "threshold": current_threshold,
        "next_threshold": next_threshold,
        "next_name": _rank_name(index + 1),
        "next_symbol": _rank_symbol(index + 1),
        "progress_pct": progress_pct,
        "points_to_next": next_threshold - points,
    }
