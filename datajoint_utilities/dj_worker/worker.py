"""
Mechanism to set up and manage "workers" to operate a DataJoint pipeline
Each "worker" is run in a while-loop with the total run-duration configurable via
command line argument '--duration' (if not set, runs perpetually)
    - the loop will not begin a new cycle after this period of time (in seconds)
    - the loop will run perpetually if duration<0 or if duration==None
    - the script will not be killed _at_ this limit, it will keep executing,
      and just stop repeating after the time limit is exceeded
Some populate settings (e.g. 'limit', 'max_calls') can be set to process some number of jobs at
a time for every iteration of the loop, instead of all jobs. This allows for the controll of the processing to
propagate through the pipeline more horizontally or vertically.
"""
import inspect
import re
import time
from datetime import datetime
import datajoint as dj
import pandas as pd

from .. import dict_to_uuid
from .worker_schema import (
    RegisteredWorker,
    WorkerLog,
    ErrorLog,
    get_process_name,
    is_djtable,
)

logger = dj.logger

_populate_settings = {
    "display_progress": True,
    "reserve_jobs": True,
    "suppress_errors": True,
}


if dj.__version__ > "0.13.7":
    _populate_settings["return_success_count"] = True


class DataJointWorker:
    """
    A decorator class for running and managing the populate jobs
    """

    def __init__(
        self,
        worker_name,
        worker_schema_name,
        *,
        run_duration=-1,
        sleep_duration=60,
        max_idled_cycle=-1,
        autoclear_error_patterns=[],
        db_prefix=[""],
    ):
        self.name = worker_name
        self._worker_schema = dj.schema(worker_schema_name)
        self._worker_schema(RegisteredWorker)
        self._worker_schema(WorkerLog)
        self._worker_schema(ErrorLog)

        self._autoclear_error_patterns = autoclear_error_patterns
        self._run_duration = run_duration if run_duration is not None else -1
        self._sleep_duration = sleep_duration
        self._max_idled_cycle = (
            max_idled_cycle if "return_success_count" in _populate_settings else -1
        )
        self._db_prefix = [db_prefix] if isinstance(db_prefix, str) else db_prefix

        self._processes_to_run = []
        self._pipeline_modules = {}
        self._idled_cycle_count = None
        self._run_start_time = None

    def __call__(self, process, **kwargs):
        self.add_step(process, **kwargs)

    def add_step(self, callable, position_=None, **kwargs):
        """
        Add a new process to the list of processes to be executed by this worker
        """
        index = len(self._processes_to_run) if position_ is None else position_
        if is_djtable(callable):
            schema_name = callable.database
            if not schema_name:
                return
            self._processes_to_run.insert(index, ("dj_table", callable, kwargs))
            if schema_name not in self._pipeline_modules:
                self._pipeline_modules[schema_name] = dj.create_virtual_module(
                    schema_name, schema_name
                )

        elif inspect.isfunction(callable) or inspect.ismethod(callable):
            self._processes_to_run.insert(index, ("function", callable, kwargs))

        else:
            raise NotImplemented(
                f"Unable to handle processing step of type {type(callable)}"
            )

    def register_worker(self):
        """
        Register the worker and its associated processes into the RegisteredWorker table
        """
        process_entries = []
        for index, (process_type, process, kwargs) in enumerate(self._processes_to_run):
            entry = {
                "worker_name": self.name,
                "process": get_process_name(process, self._db_prefix),
                "process_index": index,
                "full_table_name": process.full_table_name
                if is_djtable(process)
                else "",
                "key_source_sql": process.key_source.proj().make_sql()
                if is_djtable(process)
                else None,
                "process_kwargs": kwargs,
            }
            entry["process_config_uuid"] = dict_to_uuid(entry)
            process_entries.append(entry)

        worker_config_uuid = dict_to_uuid(
            {e["process_index"]: e["process_config_uuid"] for e in process_entries}
        )
        if RegisteredWorker & {
            "worker_name": self.name,
            "worker_config_uuid": worker_config_uuid,
        }:
            return

        worker_entry = {
            "worker_name": self.name,
            "registration_time": datetime.utcnow(),
            "worker_kwargs": {
                "worker_name": self.name,
                "worker_schema_name": self._worker_schema.database,
                "run_duration": self._run_duration,
                "sleep_duration": self._sleep_duration,
                "max_idled_cycle": self._max_idled_cycle,
                "autoclear_error_patterns": self._autoclear_error_patterns,
                "db_prefix": self._db_prefix,
            },
            "worker_config_uuid": worker_config_uuid,
        }

        with RegisteredWorker.connection.transaction:
            with dj.config(safemode=False):
                (RegisteredWorker & {"worker_name": self.name}).delete()
            RegisteredWorker.insert1(worker_entry)
            RegisteredWorker.Process.insert(process_entries)
        logger.info(f"Worker registered: {self.name}")

    def _run_once(self):
        """
        Run all processes in order, once
        """
        success_count = 0 if "return_success_count" in _populate_settings else 1
        for process_type, process, kwargs in self._processes_to_run:
            WorkerLog.log_process_job(
                process, worker_name=self.name, db_prefix=self._db_prefix
            )
            if process_type == "dj_table":
                status = process.populate(**{**_populate_settings, **kwargs})
                if "return_success_count" in _populate_settings:
                    success_count += status[0]
            elif process_type == "function":
                try:
                    process(**kwargs)
                except Exception as e:
                    if hasattr(e, "key") and isinstance(e.key, dict):
                        key = e.key
                    else:
                        key = dict(error_time=datetime.utcnow())
                    ErrorLog.log_exception(key, process, str(e))

        _clean_up(
            self._pipeline_modules.values(),
            additional_error_patterns=self._autoclear_error_patterns,
            db_prefix=self._db_prefix,
        )

        WorkerLog.delete_old_logs()
        ErrorLog.delete_old_logs()

        return success_count

    def _keep_running(self):
        """
        Determine whether or not to keep running all the processes again and again
        """
        exceed_run_duration = not (
            time.time() - self._run_start_time < self._run_duration
            or self._run_duration < 0
        )
        exceed_max_idled_cycle = 0 < self._max_idled_cycle < self._idled_cycle_count
        return not (exceed_run_duration or exceed_max_idled_cycle)

    def run(self):
        """
        Run all processes in a continual loop until the terminating condition is met (see "_keep_running()")
        """
        logger.info(f"Starting DataJoint Worker: {self.name}")
        self._run_start_time = time.time()
        self._idled_cycle_count = 0
        while self._keep_running():
            try:
                success_count = self._run_once()
            except Exception as e:
                logger.error(str(e))
            else:
                self._idled_cycle_count += bool(not success_count)
            time.sleep(self._sleep_duration)
        logger.info(f"Stopping DataJoint Worker: {self.name}")


def _clean_up(pipeline_modules, additional_error_patterns=[], db_prefix=""):
    """
    Routine to clear entries from the jobs table that are:
    + generic-type error jobs
    + stale "reserved" jobs
    """
    _generic_errors = [
        "%Deadlock%",
        "%Lock wait timeout%",
        "%MaxRetryError%",
        "%KeyboardInterrupt%",
        "InternalError: (1205%",
        "%SIGTERM%",
        "%LostConnectionError%",
    ]

    for pipeline_module in pipeline_modules:
        # clear generic error jobs
        (
            pipeline_module.schema.jobs
            & 'status = "error"'
            & [f'error_message LIKE "{e}"' for e in _generic_errors]
        ).delete()
        # clear additional error patterns
        additional_error_query = (
            pipeline_module.schema.jobs
            & 'status = "error"'
            & [f'error_message LIKE "{e}"' for e in additional_error_patterns]
        )

        with pipeline_module.schema.jobs.connection.transaction:
            for error_entry in additional_error_query.fetch(as_dict=True):
                ErrorLog.log_error_job(
                    error_entry,
                    schema_name=pipeline_module.schema.database,
                    db_prefix=db_prefix,
                )
            additional_error_query.delete()

        # clear stale "reserved" jobs
        current_connections = [
            v[0]
            for v in dj.conn().query(
                "SELECT id FROM information_schema.processlist WHERE id <> CONNECTION_ID() ORDER BY id"
            )
        ]
        if current_connections:
            current_connections = (
                f'({", ".join([str(c) for c in current_connections])})'
            )
            stale_jobs = (
                pipeline_module.schema.jobs
                & 'status = "reserved"'
                & f"connection_id NOT IN {current_connections}"
            )
            (pipeline_module.schema.jobs & stale_jobs.fetch("KEY")).delete()


def get_workflow_progress(db_prefixes):
    """
    Function to retrieve the processing status of a workflow comprised of all schemas with the specified prefix(es)
    Return a dataframe with all Imported/Computed tables, and their corresponding processing status:
        + total
        + in queue
        + reserved
        + error
        + ignore
        + remaining

    Example usage:
        db_prefixes = ['my_project_']
        workflow_status = get_workflow_progress(db_prefixes)

    (Note: not topologically sorted)
    """
    pipeline_modules = {
        n: dj.create_virtual_module(n, n)
        for n in dj.list_schemas()
        if re.match("|".join(db_prefixes), n)
    }

    if hasattr(list(pipeline_modules.values())[0].schema, "progress"):
        workflow_status = []
        for pipeline_module in pipeline_modules.values():
            progress = pipeline_module.schema.progress()
            progress.index = progress.index.map(
                lambda x: f"{pipeline_module.schema.database}.{x}"
            )
            workflow_status.append(progress)
        workflow_status = pd.concat(workflow_status)
    else:
        job_status_df = {"reserved": [], "error": [], "ignore": []}
        for pipeline_module in pipeline_modules.values():
            for job_status in ("reserved", "error", "ignore"):
                status_df = (
                    dj.U("table_name")
                    .aggr(
                        pipeline_module.schema.jobs & f'status = "{job_status}"',
                        **{job_status: "count(table_name)"},
                    )
                    .fetch(format="frame")
                )
                status_df.index = status_df.index.map(
                    lambda x: f"{pipeline_module.schema.database}.{x}"
                )
                job_status_df[job_status].append(status_df)

        for k, v in job_status_df.items():
            job_status_df[k] = pd.concat(v)

        process_tables = {}
        for pipeline_module in pipeline_modules.values():
            pipeline_module.schema.spawn_missing_classes(context=process_tables)

        process_tables = {
            process.full_table_name.replace("`", ""): process
            for process in process_tables.values()
            if process.table_name.startswith("_")
        }

        workflow_status = pd.DataFrame(list(process_tables), columns=["table_name"])
        workflow_status.set_index("table_name", inplace=True)

        workflow_status["total"] = [
            len(process_tables[t].key_source) for t in workflow_status.index
        ]
        workflow_status["in_queue"] = [
            len(process_tables[t].key_source - process_tables[t].proj())
            for t in workflow_status.index
        ]

        workflow_status = workflow_status.join(
            job_status_df["reserved"]
            .join(job_status_df["error"], how="outer")
            .join(job_status_df["ignore"], how="outer"),
            how="left",
        )

        workflow_status.fillna(0, inplace=True)

        workflow_status["remaining"] = (
            workflow_status.in_queue
            - workflow_status.reserved
            - workflow_status.error
            - workflow_status.ignore
        )

    def _format_table_name(full_table_name):
        schema_name, table_name = full_table_name.split(".")
        schema_name = re.sub("|".join(db_prefixes), "", schema_name)
        table_name = dj.utils.to_camel_case(table_name)
        return f"{schema_name}.{table_name}"

    workflow_status.index = workflow_status.index.map(_format_table_name)

    return workflow_status
