# SPDX-License-Identifier: Apache-2.0
"""Housekeeping jobs: today, ``maintenance.vacuum``.

Lifted from oaken-mail ``core/maintenance.py`` (cf3b187), on core's one database. A vacuum rewrites
the database to give back the space deleted rows left. It runs as a job (one at a time, with the same
lease, retry and audit trail as any other) and holds the write lock while it works, so writers wait
and readers carry on. It needs roughly the database's own size free on the disk, checked first.
"""

import shutil
from pathlib import Path

from pydantic import BaseModel

from oaken_core.actors import SYSTEM, Actor
from oaken_core.db import Database
from oaken_core.errors import StorageError
from oaken_core.jobs import PRIORITY_BACKFILL, Enqueued, Job, Jobs, JobRunner

VACUUM_KIND = "maintenance.vacuum"
ROOM = 1.1  # free space wanted, as a multiple of the database being rewritten


class VacuumPayload(BaseModel):
    pass


class VacuumResult(BaseModel):
    before: int
    after: int


def file_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def check_room(db: Database) -> None:
    """Raise ``StorageError`` if the disk cannot take a rewrite of ``db``."""
    need = int(file_bytes(db.path) * ROOM)
    free = shutil.disk_usage(db.path.parent).free
    if free < need:
        raise StorageError(f"not enough free disk to vacuum: {db.path.name} needs about {need:,} bytes free and {free:,} are")


def register_maintenance_jobs(runner: JobRunner, db: Database) -> None:
    async def handle(_payload: VacuumPayload, _job: Job) -> VacuumResult:
        check_room(db)
        before = file_bytes(db.path)
        await db.vacuum()
        return VacuumResult(before=before, after=file_bytes(db.path))

    runner.register(VACUUM_KIND, VacuumPayload, handle, concurrency=1, result_model=VacuumResult)


async def enqueue_vacuum(jobs: Jobs, *, actor: Actor = SYSTEM) -> Enqueued:
    """Queue a vacuum. One at a time: asking while one is queued or running returns that job."""
    return await jobs.enqueue(VACUUM_KIND, VacuumPayload(), priority=PRIORITY_BACKFILL, dedupe_key=VACUUM_KIND, actor=actor)
