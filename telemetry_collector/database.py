"""SQLAlchemy engine, session, and ORM models for collector persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.sql import func

DB_POOL_SIZE = 2
DB_MAX_OVERFLOW = 3
DB_POOL_TIMEOUT_SECONDS = 10
DB_POOL_RECYCLE_SECONDS = 300

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by every collector ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TelemetryRun(Base):
    """Registered build and its materialized current telemetry state."""

    __tablename__ = "telemetry_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    country_code: Mapped[str] = mapped_column(Text, nullable=False)
    pipeline: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(Text)
    release_id: Mapped[str | None] = mapped_column(Text)
    run_kind: Mapped[str] = mapped_column(Text, nullable=False)
    owner_hf_id: Mapped[str] = mapped_column(Text, nullable=False)
    owner_hf_username: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    current_stage: Mapped[str] = mapped_column(Text, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resources: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    work: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    producers: Mapped[list[TelemetryProducer]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class TelemetryProducer(Base):
    """One telemetry-emitting process registered for a build."""

    __tablename__ = "telemetry_producers"

    run_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "telemetry_runs.run_id",
            name="fk_telemetry_producers_run_id_telemetry_runs",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    producer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )

    run: Mapped[TelemetryRun] = relationship(back_populates="producers")
    events: Mapped[list[TelemetryEvent]] = relationship(
        back_populates="producer",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class TelemetryEvent(Base):
    """An idempotent telemetry event belonging to a registered producer."""

    __tablename__ = "telemetry_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "producer_id"],
            ["telemetry_producers.run_id", "telemetry_producers.producer_id"],
            name="fk_telemetry_events_run_id_producer_id_telemetry_producers",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "run_id",
            "producer_id",
            "sequence",
            name="telemetry_events_run_id_producer_id_sequence_key",
        ),
    )

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "telemetry_runs.run_id",
            name="fk_telemetry_events_run_id_telemetry_runs",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    producer_id: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    emitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    stage_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    resources: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    producer: Mapped[TelemetryProducer] = relationship(back_populates="events")


Index(
    "telemetry_runs_country_registered_idx",
    TelemetryRun.country_code,
    TelemetryRun.registered_at.desc(),
    TelemetryRun.run_id.desc(),
)
Index(
    "telemetry_events_run_order_idx",
    TelemetryEvent.run_id,
    TelemetryEvent.producer_id,
    TelemetryEvent.sequence,
)
Index(
    "telemetry_producers_run_registered_idx",
    TelemetryProducer.run_id,
    TelemetryProducer.registered_at,
    TelemetryProducer.producer_id,
)


def normalize_database_url(database_url: str) -> URL:
    """Return a PostgreSQL URL using SQLAlchemy's synchronous Psycopg dialect."""

    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("DATABASE_URL must identify a PostgreSQL database.")
    return url.set(drivername="postgresql+psycopg")


def create_database_engine(database_url: str) -> Engine:
    """Create the collector's bounded, health-checked SQLAlchemy engine."""

    return create_engine(
        normalize_database_url(database_url),
        pool_size=DB_POOL_SIZE,
        max_overflow=DB_MAX_OVERFLOW,
        pool_timeout=DB_POOL_TIMEOUT_SECONDS,
        pool_pre_ping=True,
        pool_recycle=DB_POOL_RECYCLE_SECONDS,
    )


def create_session_factory(engine: Engine) -> sessionmaker:
    """Create the short-lived session factory used by the repository."""

    return sessionmaker(bind=engine, expire_on_commit=False)
