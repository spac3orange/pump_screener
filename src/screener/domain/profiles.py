from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

PROFILE_NAME_MAX = 64
PROFILE_DESCRIPTION_MAX = 300
DEFAULT_PROFILE_NAME = "Default"
_FORBIDDEN_NAME_CHARS = frozenset("<>&")


class ProfileError(ValueError):
    """Profile operation refused; the message is safe to show to an admin."""


class ProfileNameTaken(ProfileError):
    pass


class ProfileNotFound(ProfileError):
    pass


class ProfileProtected(ProfileError):
    pass


class ProfileValidationError(ProfileError):
    pass


@dataclass(slots=True, frozen=True)
class ProfileVersion:
    id: UUID
    profile_id: UUID
    version_no: int
    config: dict[str, Any]
    config_schema_version: int
    config_hash: str
    created_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class MonitoringProfile:
    id: UUID
    name: str
    description: str | None
    is_system: bool
    current: ProfileVersion


def normalize_profile_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise ProfileValidationError("Имя профиля не может быть пустым.")
    if len(name) > PROFILE_NAME_MAX:
        raise ProfileValidationError(f"Имя профиля длиннее {PROFILE_NAME_MAX} символов.")
    for ch in name:
        if unicodedata.category(ch) in ("Cc", "Cf") or ch in _FORBIDDEN_NAME_CHARS:
            raise ProfileValidationError(
                "Имя содержит недопустимые символы (< > & или управляющие)."
            )
    return name


def normalize_description(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = "".join(ch for ch in raw.strip() if unicodedata.category(ch) != "Cc" or ch == "\n")
    if not text or text == "-":
        return None
    if len(text) > PROFILE_DESCRIPTION_MAX:
        raise ProfileValidationError(
            f"Описание длиннее {PROFILE_DESCRIPTION_MAX} символов."
        )
    return text
