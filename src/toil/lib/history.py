# Copyright (C) 2024 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Contains tools for tracking history.
"""

import logging
import os
import sys
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from typing import Iterable, Optional

from toil.lib.io import get_toil_home

logger = logging.getLogger(__name__)

class HistoryDatabaseSchemaTooNewError(RuntimeError):
    """
    Raised when we would write to the history database, but its schema is too
    new for us to understand.
    """
    pass

@dataclass
class WorkflowSummary:
    """
    Data class holding summary information for a workflow.

    Represents all the attempts to execute one run of a workflow.
    """
    id: str
    name: Optional[str]
    job_store: str
    total_attempts: int
    total_job_attempts: int
    succeeded: bool
    start_time: Optional[float]
    """
    Time when the first workflow attempt started, in seconds since epoch.

    None if there are no attempts recorded.
    """
    runtime: Optional[float]
    """
    Time from the first workflow attempt's start to the last one's end, in seconds.

    None if there are no attempts recorded.
    """
    trs_spec: Optional[str]

@dataclass
class WorkflowAttemptSummary:
    """
    Data class holding summary information for a workflow attempt.

    Helpfully includes the workflow metadata for Dockstore.
    """
    workflow_id: str
    attempt_number: int
    succeeded: bool
    start_time: float
    runtime: float
    submitted_to_dockstore: bool
    batch_system: Optional[str]
    caching: Optional[bool]
    toil_version: Optional[str]
    python_version: Optional[str]
    platform_system: Optional[str]
    platform_machine: Optional[str]
    workflow_job_store: str
    workflow_trs_spec: Optional[str]

@dataclass
class JobAttemptSummary:
    """
    Data class holding summary information for a job attempt within a known
    workflow attempt.
    """
    id: str
    job_name: str
    succeeded: bool
    start_time: float
    runtime: float
    submitted_to_dockstore: bool
    cores: Optional[float]
    cpu_seconds: Optional[float]
    memory_bytes: Optional[int]
    disk_bytes: Optional[int]

class HistoryManager:
    """
    Class responsible for managing the history of Toil runs.
    """

    @classmethod
    def database_path(cls) -> str:
        """
        Get the path at which the database we store history in lives.
        """
        return os.path.join(get_toil_home(), "history.sqlite")

    @classmethod
    def connection(cls) -> sqlite3.Connection:
        """
        Connect to the history database.

        Caller must not actually use the connection without using
        ensure_tables() to protect reads and updates.
        """
        if not os.path.exists(cls.database_path()):
            # Make the database and protect it from snoopers and busybodies
            con = sqlite3.connect(cls.database_path())
            del con
            os.chmod(cls.database_path(), 0o600)

        con = sqlite3.connect(
            cls.database_path(),
            isolation_level="DEFERRED"
        )
        
        if hasattr(con, 'autocommit'):
            # This doesn't much matter given the isolation level setting,
            # but is recommended on Python versions that have it (3.12+)
            con.autocommit = False
        # Set up the connection to use the Row class so that we can look up row values by column name and not just order.
        con.row_factory = sqlite3.Row
        # We use foreign keys
        con.execute("PRAGMA foreign_keys = ON")
        return con

    @classmethod
    def ensure_tables(cls, con: sqlite3.Connection, cur: sqlite3.Cursor) -> None:
        """
        Ensure that tables exist in the database and the schema is migrated to the current version.

        Leaves the cursor in a transaction where the schema version is known to be correct.

        :raises HistoryDatabaseSchemaTooNewError: If the schema is newer than the current version.
        """

        # Python already puts us in a transaction.

        # TODO: Do a try-and-fall-back to avoid sending the table schema for
        # this every time we do anything.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS migrations (
                version INT NOT NULL PRIMARY KEY,
                description TEXT
            )
        """)
        db_version = next(cur.execute("SELECT MAX(version) FROM migrations"))[0]
        if db_version is None:
            db_version = -1

        # This holds pairs of description and command lists.
        # To make a schema change, ADD A NEW PAIR AT THE END, and include
        # statements to adjust existing data.
        migrations = [
            (
                "Make initial tables",
                [
                    """
                    CREATE TABLE workflows (
                        id TEXT NOT NULL PRIMARY KEY,
                        job_store TEXT NOT NULL,
                        name TEXT,
                        trs_spec TEXT
                    )
                    """,
                    # There's no reference constraint from the job attempts to
                    # the workflow attempts because the jobs for a workflow
                    # attempt need to go in before the attempt is known to be
                    # finished or failed/before the attempt is submittable to
                    # Dockstore.
                    #
                    # TODO: Should we force workflow attempts to be reported on
                    # start so that we can have the jobs key-reference them?
                    # And so that we always have a start time for the workflow
                    # as a whole?
                    """
                    CREATE TABLE job_attempts (
                        id TEXT NOT NULL PRIMARY KEY,
                        workflow_id TEXT NOT NULL,
                        workflow_attempt_number INT NOT NULL,
                        job_name TEXT NOT NULL,
                        succeeded INTEGER NOT NULL,
                        start_time REAL NOT NULL,
                        runtime REAL NOT NULL,
                        cores REAL,
                        cpu_seconds REAL,
                        memory_bytes INTEGER,
                        disk_bytes INTEGER,
                        submitted_to_dockstore INTEGER NOT NULL DEFAULT FALSE,
                        FOREIGN KEY(workflow_id) REFERENCES workflows(id)
                    )
                    """,
                    """
                    CREATE INDEX idx_job_attempts_by_workflow_attempt
                    ON job_attempts (workflow_id, workflow_attempt_number)
                    """,
                    """
                    CREATE TABLE workflow_attempts (
                        workflow_id TEXT NOT NULL,
                        attempt_number INTEGER NOT NULL,
                        succeeded INTEGER NOT NULL,
                        start_time REAL NOT NULL,
                        runtime REAL NOT NULL,
                        batch_system TEXT,
                        caching INTEGER,
                        toil_version TEXT,
                        python_version TEXT,
                        platform_system TEXT,
                        platform_machine TEXT,
                        submitted_to_dockstore INTEGER NOT NULL DEFAULT FALSE,
                        PRIMARY KEY(workflow_id,attempt_number),
                        FOREIGN KEY(workflow_id) REFERENCES workflows(id)
                    )
                    """
                ]
            ),
        ]

        if db_version + 1 > len(migrations):
            raise HistoryDatabaseSchemaTooNewError(f"History database version is {db_version}, but known migrations only go up to {len(migrations) - 1}")

        for migration_number in range(db_version + 1, len(migrations)):
            for statement_number, statement in enumerate(migrations[migration_number][1]):
                # Run all the migration commands.
                # We don't use executescript() because (on old Pythons?) it
                # commits the current transactrion first.
                try:
                    cur.execute(statement)
                except sqlite3.OperationalError:
                    logger.exception("Could not execute migration %s statement %s: %s", migration_number, statement_number, statement)
                    raise
            cur.execute("INSERT INTO migrations VALUES (?, ?)", (migration_number, migrations[migration_number][0]))

        # If we did have to migrate, leave everything else we do as part of the migration transaction.

    ##
    # Recording Methods
    ##

    @classmethod
    def record_workflow_creation(cls, workflow_id: str, job_store_spec: str) -> None:
        """
        Record that a workflow is being run.

        Takes the Toil config's workflow ID and the location of the job store.

        Should only be called on the *first* attempt on a job store, not on a
        restart.

        A workflow may have multiple attempts to run it, some of which succeed
        and others of which fail. Probably only the last one should succeed.
        
        :param job_store_spec: The job store specifier for the workflow. Should
            be canonical and always start with the type and a colon. If the
            job store is later moved by the user, the location will not be
            updated.
        """

        logger.info("Recording workflow creation of %s in %s", workflow_id, job_store_spec)

        con = cls.connection()
        cur = con.cursor()
        try:
            cls.ensure_tables(con, cur)
            cur.execute("INSERT INTO workflows VALUES (?, ?, NULL, NULL)", (workflow_id, job_store_spec))
        except:
            con.rollback()
            raise
        else:
            con.commit()

        # If we raise out of here the connection goes away and the transaction rolls back.


    @classmethod
    def record_workflow_metadata(cls, workflow_id: str, workflow_name: str, trs_spec: Optional[str] = None) -> None:
        """
        Associate a name and optionally a TRS ID and version with a workflow run.
        """

        # TODO: Make name of this function less general?

        logger.info("Workflow %s is a run of %s", workflow_id, workflow_name)
        if trs_spec:
            logger.info("Workflow %s has TRS ID and version %s", workflow_id, trs_spec)

        con = cls.connection()
        cur = con.cursor()
        try:
            cls.ensure_tables(con, cur)
            cur.execute("UPDATE workflows SET name = ? WHERE id = ?", (workflow_name, workflow_id))
            if trs_spec is not None:
                cur.execute("UPDATE workflows SET trs_spec = ? WHERE id = ?", (trs_spec, workflow_id))
        except:
            con.rollback()
            raise
        else:
            con.commit()

    @classmethod
    def record_job_attempt(
            cls,
            workflow_id: str,
            workflow_attempt_number: int,
            job_name: str,
            succeeded: bool,
            start_time: float,
            runtime: float,
            cores: Optional[float] = None,
            cpu_seconds: Optional[float] = None,
            memory_bytes: Optional[int] = None,
            disk_bytes: Optional[int] = None
        ) -> None:
        """
        Record that a job ran in a workflow.

        Doesn't expect the provided information to uniquely identify the job
        attempt; assigns the job attempt its own unique ID.

        Thread safe.

        :param job_name: A human-readable name for the job. Not expected to be
            a job store ID or to necessarily uniquely identify the job within
            the workflow.
        :param start_time: Job execution start time ins econds since epoch.
        :param runtime: Job execution duration in seconds.
        :param cores: Number of CPU cores the job was scheduled on.
        :param cpu_seconds: CPU core-seconds actually consumed.
        :param memory_bytes: Peak observed job memory usage.
        :param disk_bytes: Observed job disk usage.
        """

        logger.debug("Workflow %s ran job %s", workflow_id, job_name)

        con = cls.connection()
        cur = con.cursor()
        try:
            cls.ensure_tables(con, cur)
            cur.execute(
                """
                INSERT INTO job_attempts(
                    id,
                    workflow_id,
                    workflow_attempt_number,
                    job_name,
                    succeeded,
                    start_time,
                    runtime,
                    cores,
                    cpu_seconds,
                    memory_bytes,
                    disk_bytes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    workflow_id,
                    workflow_attempt_number,
                    job_name,
                    1 if succeeded else 0,
                    start_time,
                    runtime,
                    cores,
                    cpu_seconds,
                    memory_bytes,
                    disk_bytes,
                )
            )
        except:
            con.rollback()
            raise
        else:
            con.commit()

    @classmethod
    def record_workflow_attempt(
        cls,
        workflow_id: str,
        workflow_attempt_number: int,
        succeeded: bool,
        start_time: float,
        runtime: float,
        batch_system: Optional[str] = None,
        caching: Optional[bool] = None,
        toil_version: Optional[str] = None,
        python_version: Optional[str] = None,
        platform_system: Optional[str] = None,
        platform_machine: Optional[str] = None
    ) -> None:
        """
        Record a workflow attempt (start or restart) having finished or failed.

        :param batch_system: The Python type name of the batch system implementation used.
        :param caching: Whether Toil filestore-level caching was used.
        :param toil_version: Version of Toil used to run the workflow.
        :param python_version: Version of Python used to run the workflow.
        :param platform_system: OS ("Darwin", "Linux", etc.) used to run the workflow.
        :param platform_machine: CPU type ("AMD64", etc.) used to run the workflow leader.
        """

        logger.info("Workflow %s stopped. Success: %s", workflow_id, succeeded)

        con = cls.connection()
        cur = con.cursor()
        try:
            cls.ensure_tables(con, cur)
            cur.execute(
                """
                INSERT INTO workflow_attempts(
                    workflow_id,
                    attempt_number,
                    succeeded,
                    start_time,
                    runtime,
                    batch_system,
                    caching,
                    toil_version,
                    python_version,
                    platform_system,
                    platform_machine
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    workflow_attempt_number,
                    1 if succeeded else 0,
                    start_time,
                    runtime,
                    batch_system,
                    caching,
                    toil_version,
                    python_version,
                    platform_system,
                    platform_machine
                )
            )
        except:
            con.rollback()
            raise
        else:
            con.commit()


    ##
    # Read methods
    ##

    # We would implement a bunch of iterators and allow follow-up queries, but
    # then we'd have to figure out how to make sure we use one connection and
    # cursor and not block ourselves with the database transaction locks.
    #
    # So instead we always fetch all the information asked for and close out
    # the read transaction before returning.
    #
    # This means the caller has to worry about a workflow vanishing or changing
    # between when it was shown to them and when they ask follow-up questions,
    # but it also means we can't deadlock.

    @classmethod
    def get_workflow_trs_spec(cls, workflow_id: str) -> Optional[str]:
        """
        Get the TRS spec for a workflow, or None if it does not have one.
        """

    @classmethod
    def summarize_workflows(cls) -> list[WorkflowSummary]:
        """
        List all known workflows and their summary statistics.
        """

        workflows = []

        con = cls.connection()
        cur = con.cursor()
        try:
            cls.ensure_tables(con, cur)
            cur.execute(
                """
                SELECT
                    workflows.id AS id,
                    workflows.name AS name,
                    workflows.job_store AS job_store,
                    (SELECT count(*) FROM workflow_attempts WHERE workflow_id = workflows.id) AS total_attempts,
                    (SELECT count(*) FROM job_attempts WHERE workflow_id = workflows.id) AS total_job_attempts,
                    (SELECT min(count(*), 1) FROM workflow_attempts WHERE workflow_id = workflows.id AND succeeded = TRUE) AS succeeded,
                    (SELECT min(start_time) FROM workflow_attempts WHERE workflow_id = workflows.id) AS start_time,
                    (SELECT max(start_time + runtime) FROM workflow_attempts WHERE workflow_id = workflows.id) AS end_time,
                    workflows.trs_spec AS trs_spec
                FROM workflows
                ORDER BY start_time DESC
                """
            )
            for row in cur:
                workflows.append(
                    WorkflowSummary(
                        id=row["id"],
                        name=row["name"],
                        job_store=row["job_store"],
                        total_attempts=row["total_attempts"],
                        total_job_attempts=row["total_job_attempts"],
                        succeeded=(row["succeeded"] == 1),
                        start_time=row["start_time"],
                        runtime=(row["end_time"] - row["start_time"]) if row["start_time"] is not None and row["end_time"] is not None else None,
                        trs_spec=row["trs_spec"]
                    )
                )
        except:
            con.rollback()
            raise
        else:
            con.commit()

        return workflows




    @classmethod
    def get_submittable_workflow_attempts(cls, limit: int = sys.maxsize) -> list[WorkflowAttemptSummary]:
        """
        List all workflow attempts not yet submitted to Dockstore.

        :param limit: Get no more than this many.
        """

        attempts = []

        con = cls.connection()
        cur = con.cursor()
        try:
            cls.ensure_tables(con, cur)
            cur.execute(
                """
                SELECT
                    workflow_attempts.workflow_id AS workflow_id,
                    workflow_attempts.attempt_number AS attempt_number,
                    workflow_attempts.succeeded AS succeeded,
                    workflow_attempts.start_time AS start_time,
                    workflow_attempts.runtime AS runtime,
                    workflow_attempts.batch_system AS batch_system,
                    workflow_attempts.caching AS caching,
                    workflow_attempts.toil_version AS toil_version,
                    workflow_attempts.python_version AS python_version,
                    workflow_attempts.platform_system AS platform_system,
                    workflow_attempts.platform_machine AS platform_machine,
                    workflow_attempts.submitted_to_dockstore AS submitted_to_dockstore,
                    workflows.job_store AS workflow_job_store,
                    workflows.trs_spec AS workflow_trs_spec
                FROM workflow_attempts
                    JOIN workflows ON workflow_attempts.workflow_id = workflows.id
                WHERE workflow_attempts.submitted_to_dockstore = FALSE
                    AND workflows.trs_spec IS NOT NULL
                ORDER BY start_time DESC
                LIMIT ?
                """,
                (limit,)
            )
            for row in cur:
                attempts.append(
                    WorkflowAttemptSummary(
                        workflow_id=row["workflow_id"],
                        attempt_number=row["attempt_number"],
                        succeeded=(row["succeeded"] == 1),
                        start_time=row["start_time"],
                        runtime=row["runtime"],
                        batch_system=row["batch_system"],
                        caching=(row["caching"] == 1),
                        toil_version=row["toil_version"],
                        python_version=row["python_version"],
                        platform_system=row["platform_system"],
                        platform_machine=row["platform_machine"],
                        submitted_to_dockstore=(row["submitted_to_dockstore"] == 1),
                        workflow_job_store=row["workflow_job_store"],
                        workflow_trs_spec=row["workflow_trs_spec"]
                    )
                )
        except:
            con.rollback()
            raise
        else:
            con.commit()

        return attempts

    @classmethod
    def get_workflow_attempts_with_submittable_job_attempts(cls, limit: int = sys.maxsize) -> list[WorkflowAttemptSummary]:
        """
        Get all workflow attempts that have job attempts not yet submitted to
        Dockstore.

        The workflow attempts themselves will have finished and been recorded,
        and have TRS IDs.

        :param limit: Get no more than this many.
        """

        attempts = []

        con = cls.connection()
        cur = con.cursor()
        try:
            cls.ensure_tables(con, cur)
            cur.execute(
                """
                SELECT
                    workflow_attempts.workflow_id AS workflow_id,
                    workflow_attempts.attempt_number AS attempt_number,
                    workflow_attempts.succeeded AS succeeded,
                    workflow_attempts.start_time AS start_time,
                    workflow_attempts.runtime AS runtime,
                    workflow_attempts.batch_system AS batch_system,
                    workflow_attempts.caching AS caching,
                    workflow_attempts.toil_version AS toil_version,
                    workflow_attempts.python_version AS python_version,
                    workflow_attempts.platform_system AS platform_system,
                    workflow_attempts.platform_machine AS platform_machine,
                    workflow_attempts.submitted_to_dockstore AS submitted_to_dockstore,
                    workflows.job_store AS workflow_job_store,
                    workflows.trs_spec AS workflow_trs_spec
                FROM (
                    SELECT DISTINCT
                        workflow_id, workflow_attempt_number
                    FROM job_attempts
                    WHERE job_attempts.submitted_to_dockstore = FALSE
                ) AS found_job_attempts
                    JOIN workflows ON found_job_attempts.workflow_id = workflows.id
                    JOIN workflow_attempts ON
                        found_job_attempts.workflow_id = workflow_attempts.workflow_id
                        AND found_job_attempts.workflow_attempt_number = workflow_attempts.attempt_number
                WHERE workflows.trs_spec IS NOT NULL
                LIMIT ?
                """,
                (limit,)
            )
            for row in cur:
                # TODO: Unify row to data class conversion
                attempts.append(
                    WorkflowAttemptSummary(
                        workflow_id=row["workflow_id"],
                        attempt_number=row["attempt_number"],
                        succeeded=(row["succeeded"] == 1),
                        start_time=row["start_time"],
                        runtime=row["runtime"],
                        batch_system=row["batch_system"],
                        caching=(row["caching"] == 1),
                        toil_version=row["toil_version"],
                        python_version=row["python_version"],
                        platform_system=row["platform_system"],
                        platform_machine=row["platform_machine"],
                        submitted_to_dockstore=(row["submitted_to_dockstore"] == 1),
                        workflow_job_store=row["workflow_job_store"],
                        workflow_trs_spec=row["workflow_trs_spec"]
                    )
                )
        except:
            con.rollback()
            raise
        else:
            con.commit()

        return attempts

    @classmethod
    def get_workflow_attempt(cls, workflow_id: str, attempt_number: int) -> Optional[WorkflowAttemptSummary]:
        """
        Get a single (not necessarily unsubmitted, not necessarily TRS-ID-having) workflow attempt summary, if present.
        """

        # TODO: Consolidate with the other 2 ways to query workflow attempts!

        attempts = []

        con = cls.connection()
        cur = con.cursor()
        try:
            cls.ensure_tables(con, cur)
            cur.execute(
                """
                SELECT
                    workflow_attempts.workflow_id AS workflow_id,
                    workflow_attempts.attempt_number AS attempt_number,
                    workflow_attempts.succeeded AS succeeded,
                    workflow_attempts.start_time AS start_time,
                    workflow_attempts.runtime AS runtime,
                    workflow_attempts.batch_system AS batch_system,
                    workflow_attempts.caching AS caching,
                    workflow_attempts.toil_version AS toil_version,
                    workflow_attempts.python_version AS python_version,
                    workflow_attempts.platform_system AS platform_system,
                    workflow_attempts.platform_machine AS platform_machine,
                    workflow_attempts.submitted_to_dockstore AS submitted_to_dockstore,
                    workflows.job_store AS workflow_job_store,
                    workflows.trs_spec AS workflow_trs_spec
                FROM workflow_attempts
                    JOIN workflows ON workflow_attempts.workflow_id = workflows.id
                WHERE workflow_id = ?
                    AND attempt_number = ?
                ORDER BY start_time DESC
                LIMIT 1
                """,
                (workflow_id, attempt_number)
            )
            for row in cur:
                attempts.append(
                    WorkflowAttemptSummary(
                        workflow_id=row["workflow_id"],
                        attempt_number=row["attempt_number"],
                        succeeded=(row["succeeded"] == 1),
                        start_time=row["start_time"],
                        runtime=row["runtime"],
                        batch_system=row["batch_system"],
                        caching=(row["caching"] == 1),
                        toil_version=row["toil_version"],
                        python_version=row["python_version"],
                        platform_system=row["platform_system"],
                        platform_machine=row["platform_machine"],
                        submitted_to_dockstore=(row["submitted_to_dockstore"] == 1),
                        workflow_job_store=row["workflow_job_store"],
                        workflow_trs_spec=row["workflow_trs_spec"]
                    )
                )
        except:
            con.rollback()
            raise
        else:
            con.commit()

        if len(attempts) == 0:
            # Not found
            return None
        else:
            return attempts[0]

    @classmethod
    def get_unsubmitted_job_attempts(cls, workflow_id: str, attempt_number: int) -> list[JobAttemptSummary]:
        """
        List all job attempts in the given workflow attempt not yet submitted to Dockstore.

        Doesn't check to make sure the workflow has a TRS ID.
        """

        attempts = []

        con = cls.connection()
        cur = con.cursor()
        try:
            cls.ensure_tables(con, cur)
            cur.execute(
                """
                SELECT
                    id,
                    job_name,
                    succeeded,
                    start_time,
                    runtime,
                    cores,
                    cpu_seconds,
                    memory_bytes,
                    disk_bytes,
                    submitted_to_dockstore
                FROM job_attempts
                WHERE workflow_id = ?
                AND workflow_attempt_number = ?
                AND submitted_to_dockstore = FALSE
                ORDER BY start_time DESC
                """,
                (workflow_id, attempt_number)
            )
            for row in cur:
                attempts.append(
                    JobAttemptSummary(
                        id=row["id"],
                        job_name=row["job_name"],
                        succeeded=(row["succeeded"] == 1),
                        start_time=row["start_time"],
                        runtime=row["runtime"],
                        cores=row["cores"],
                        cpu_seconds=row["cpu_seconds"],
                        memory_bytes=row["memory_bytes"],
                        disk_bytes=row["disk_bytes"],
                        submitted_to_dockstore=(row["submitted_to_dockstore"] == 1)
                    )
                )
        except:
            con.rollback()
            raise
        else:
            con.commit()

        return attempts

    ###
    # Submission marking methods
    ###

    @classmethod
    def mark_workflow_attempt_submitted(cls, workflow_id: str, attempt_number: int) -> None:
        """
        Mark a workflow attempt as having been successfully submitted to Dockstore.

        Does not mark the workflow attempt's job attempts as submitted.
        """

        con = cls.connection()
        cur = con.cursor()
        try:
            cls.ensure_tables(con, cur)
            cur.execute(
                "UPDATE workflow_attempts SET submitted_to_dockstore = TRUE WHERE workflow_id = ? AND attempt_number = ?",
                (workflow_id, attempt_number)
            )
        except:
            con.rollback()
            raise
        else:
            con.commit()

    @classmethod
    def mark_job_attempts_submitted(cls, job_attempt_ids: list[str]) -> None:
        """
        Mark a collection of job attempts as submitted to Dockstore in a single transaction.
        """

        con = cls.connection()
        cur = con.cursor()
        try:
            cls.ensure_tables(con, cur)
            for job_attempt_id in job_attempt_ids:
                # Do all the marking in one transaction
                cur.execute(
                    "UPDATE job_attempts SET submitted_to_dockstore = TRUE WHERE id = ?",
                    (job_attempt_id,)
                )
        except:
            con.rollback()
            raise
        else:
            con.commit()



