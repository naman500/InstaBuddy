"""Models mirroring the on-disk ``metadata.json`` schema.

The schema is versioned from day one so that a future OCR / transcription /
embedding pipeline can evolve it without breaking already-ingested content.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"
APPLICATION_VERSION = "1.0.0"


class MetadataSource(BaseModel):
    platform: str = "instagram"
    url: str
    shortcode: str


class MetadataCreator(BaseModel):
    username: str


class MetadataContent(BaseModel):
    type: str
    caption: str | None = None
    created_at: datetime | None = None


class MetadataMediaEntry(BaseModel):
    filename: str
    type: str
    sha256: str
    file_size: int = 0


class MetadataIngestion(BaseModel):
    downloaded_at: datetime
    application_version: str = APPLICATION_VERSION
    job_id: str | None = None
    downloaded_by: str | None = None


class MetadataProcessing(BaseModel):
    """Placeholders for the future LLM pipeline.

    These flags exist in V1 so that later stages can be recorded without a
    schema migration. Nothing sets them to ``True`` yet.
    """

    ocr_completed: bool = False
    transcription_completed: bool = False
    frames_extracted: bool = False
    embeddings_generated: bool = False


class PostMetadata(BaseModel):
    """The complete ``metadata.json`` document."""

    schema_version: str = SCHEMA_VERSION
    source: MetadataSource
    creator: MetadataCreator
    content: MetadataContent
    media: list[MetadataMediaEntry] = Field(default_factory=list)
    ingestion: MetadataIngestion
    processing: MetadataProcessing = Field(default_factory=MetadataProcessing)

    def to_json(self) -> str:
        """Serialize with indentation, ready to write to disk."""
        return self.model_dump_json(indent=2)
