"""PhraseLibrary — manages curated vent phrases with pain_direction classification.

Stores phrases in SQLite (vent_phrases table), supports local voting,
custom phrases, and filtering by pain_direction.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from flowcraft_core.storage.database import Database

logger = logging.getLogger(__name__)

# Import preset phrases and PainDirection
from flowcraft_core.feedback.templates import (
    PRESET_PHRASES, PresetPhrase, PainDirection,
    get_preset_phrases, get_phrases_by_pain_direction,
    get_pain_direction_label,
)


@dataclass
class Phrase:
    """A vent phrase record from the database."""
    id: str
    text: str
    lang: str  # "zh" | "en"
    category: str  # "humorous" | "sarcastic" | "direct" | "custom"
    pain_direction: str  # see PainDirection
    guides_user_to: str = ""
    vote_count: int = 0
    is_custom: bool = False
    is_active: bool = True
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "lang": self.lang,
            "category": self.category,
            "pain_direction": self.pain_direction,
            "pain_direction_label": get_pain_direction_label(self.pain_direction, self.lang),
            "guides_user_to": self.guides_user_to,
            "vote_count": self.vote_count,
            "is_custom": self.is_custom,
            "is_active": self.is_active,
            "created_at": self.created_at,
        }


class PhraseLibrary:
    """Manages the vent phrase library with SQLite persistence."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def initialize_presets(self) -> int:
        """Seed the database with preset phrases if table is empty."""
        existing = self._db.fetch_one("SELECT COUNT(*) as cnt FROM vent_phrases", ())
        if existing and existing["cnt"] > 0:
            return 0

        now = datetime.now(timezone.utc).isoformat()
        count = 0
        for p in PRESET_PHRASES:
            phrase_id = f"vp_{uuid.uuid4().hex[:12]}"
            self._db.insert_json("vent_phrases", {
                "id": phrase_id,
                "text": p.text,
                "lang": p.lang,
                "category": p.category,
                "pain_direction": p.pain_direction,
                "guides_user_to": p.guides_user_to,
                "vote_count": 0,
                "is_custom": False,
                "is_active": True,
                "created_at": now,
            })
            count += 1

        logger.info("Seeded %d preset vent phrases", count)
        return count

    # ── Queries ───────────────────────────────────────────────

    def get_top_phrases(
        self, lang: str = "zh", limit: int = 10, pain_direction: str | None = None
    ) -> list[Phrase]:
        """Get top phrases sorted by vote_count (desc)."""
        if pain_direction:
            rows = self._db.fetch_all(
                "SELECT * FROM vent_phrases WHERE lang = ? AND is_active = 1 "
                "AND pain_direction = ? ORDER BY vote_count DESC LIMIT ?",
                (lang, pain_direction, limit),
            )
        else:
            rows = self._db.fetch_all(
                "SELECT * FROM vent_phrases WHERE lang = ? AND is_active = 1 "
                "ORDER BY vote_count DESC LIMIT ?",
                (lang, limit),
            )
        return [self._row_to_phrase(dict(r)) for r in rows]

    def get_phrases_grouped(
        self, lang: str = "zh", limit_per_group: int = 3
    ) -> dict[str, list[Phrase]]:
        """Get phrases grouped by pain_direction for UI display."""
        groups: dict[str, list[Phrase]] = {}
        for pd in PainDirection.ALL:
            phrases = self.get_top_phrases(lang=lang, limit=limit_per_group, pain_direction=pd)
            if phrases:
                groups[pd] = phrases
        return groups

    def get_phrase(self, phrase_id: str) -> Phrase | None:
        """Get a single phrase by ID."""
        row = self._db.fetch_one("SELECT * FROM vent_phrases WHERE id = ?", (phrase_id,))
        if not row:
            return None
        return self._row_to_phrase(dict(row))

    def list_all_phrases(self, lang: str | None = None) -> list[Phrase]:
        """List all phrases, optionally filtered by language."""
        if lang:
            rows = self._db.fetch_all(
                "SELECT * FROM vent_phrases WHERE lang = ? AND is_active = 1 "
                "ORDER BY vote_count DESC", (lang,))
        else:
            rows = self._db.fetch_all(
                "SELECT * FROM vent_phrases WHERE is_active = 1 ORDER BY lang, vote_count DESC", ())
        return [self._row_to_phrase(dict(r)) for r in rows]

    # ── Mutations ─────────────────────────────────────────────

    def vote(self, phrase_id: str) -> Phrase | None:
        """Increment vote count for a phrase. Returns updated phrase."""
        row = self._db.fetch_one("SELECT * FROM vent_phrases WHERE id = ?", (phrase_id,))
        if not row:
            return None
        r = dict(row)
        new_count = (r.get("vote_count", 0) or 0) + 1
        self._db.update("vent_phrases", "id", phrase_id, {"vote_count": new_count})
        r["vote_count"] = new_count
        return self._row_to_phrase(r)

    def add_custom_phrase(
        self, text: str, lang: str = "zh", category: str = "custom",
        pain_direction: str = "general", guides_user_to: str = "",
    ) -> Phrase:
        """Add a user-custom phrase."""
        phrase_id = f"vp_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        self._db.insert_json("vent_phrases", {
            "id": phrase_id,
            "text": text,
            "lang": lang,
            "category": category,
            "pain_direction": pain_direction,
            "guides_user_to": guides_user_to,
            "vote_count": 0,
            "is_custom": True,
            "is_active": True,
            "created_at": now,
        })
        logger.info("Added custom phrase: %s", phrase_id)
        return self.get_phrase(phrase_id)  # type: ignore[return-value]

    def deactivate_phrase(self, phrase_id: str) -> bool:
        """Soft-delete a phrase (set is_active=False)."""
        row = self._db.fetch_one("SELECT id FROM vent_phrases WHERE id = ?", (phrase_id,))
        if not row:
            return False
        self._db.update("vent_phrases", "id", phrase_id, {"is_active": False})
        return True

    # ── Helpers ────────────────────────────────────────────────

    @staticmethod
    def _row_to_phrase(row: dict[str, Any]) -> Phrase:
        return Phrase(
            id=row.get("id", ""),
            text=row.get("text", ""),
            lang=row.get("lang", "zh"),
            category=row.get("category", "default"),
            pain_direction=row.get("pain_direction", "general"),
            guides_user_to=row.get("guides_user_to", ""),
            vote_count=row.get("vote_count", 0) or 0,
            is_custom=bool(row.get("is_custom", False)),
            is_active=bool(row.get("is_active", True)),
            created_at=row.get("created_at", ""),
        )
