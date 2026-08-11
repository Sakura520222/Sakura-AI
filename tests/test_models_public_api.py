"""Regression tests for the package-level backend.models API."""

from backend import models
from backend.models.database import init_database
from backend.models.security_models import SecurityEventLog


def test_promised_model_exports_are_bound():
    assert models.init_database is init_database
    assert models.SecurityEventLog is SecurityEventLog


def test_models_all_contains_only_bound_names():
    assert [name for name in models.__all__ if not hasattr(models, name)] == []
