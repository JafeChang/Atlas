"""Persistence and validation for anonymous frontend interaction events."""

from .labels import read_user_labels
from .store import EventStore, validate_event

__all__ = ["EventStore", "validate_event", "read_user_labels"]
