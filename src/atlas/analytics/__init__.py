"""Persistence and validation for anonymous frontend interaction events."""

from .store import EventStore, validate_event

__all__ = ["EventStore", "validate_event"]
