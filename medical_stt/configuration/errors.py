"""Configuration error type.

Kept in its own module so every layer (settings model, validation, catalogs,
bootstrap, and the GUI) can raise or catch it without importing the rest of the
configuration package -- and without an import cycle.
"""
from __future__ import annotations


class ConfigError(ValueError):
    """Raised when settings.yaml / environment values fail validation."""
