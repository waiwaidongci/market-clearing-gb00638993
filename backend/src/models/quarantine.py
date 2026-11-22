from datetime import datetime
from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now

class Quarantine(Base):
    """One row per (group content, release state) of a quarantined group.

    A held group is stored under its bad-content signature.  Release retires the
    held row of the same group and writes a released row under the corrected
    signature inside one transaction.  A clearing run reads the whole ledger in a
    single statement, so it sees a group wholly before or wholly after release.
    """

    __tablename__ = 'quarantine_groups'
    __table_args__ = (UniqueConstraint('signature'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[str] = mapped_column(String(16), index=True)
    signature: Mapped[str] = mapped_column(String(64), index=True)
    keys: Mapped[list] = mapped_column(JSON)
    anchors: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default='held', index=True)
    destination: Mapped[str | None] = mapped_column(String(200))
    issues: Mapped[list] = mapped_column(JSON, default=list)
    rows: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(String(500))
    author: Mapped[str] = mapped_column(String(64))
    dataset_id: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=now)
