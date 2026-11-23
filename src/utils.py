LIKE_ESCAPE = "\\"


def escape_like(value: str) -> str:
    """Escape LIKE/ILIKE wildcards so user input matches literally (use with ESCAPE '\\')."""
    return (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
