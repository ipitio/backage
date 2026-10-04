"""Shared JSON types and publication errors."""

JsonValue = dict[str, "JsonValue"] | list["JsonValue"] | str | int | float | bool | None


class PublicationError(ValueError):
    """A generated endpoint cannot be safely published."""
