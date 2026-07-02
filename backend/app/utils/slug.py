from slugify import slugify


def make_slug(name: str) -> str:
    """Create a URL-safe slug from a company name."""
    return slugify(name, max_length=200)
