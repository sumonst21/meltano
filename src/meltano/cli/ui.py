import asyncio
import click
import logging
import os
import secrets
import signal
import subprocess
from click_default_group import DefaultGroup

from . import cli
from .params import project
from meltano.core.db import project_engine
from meltano.core.tracking import GoogleAnalyticsTracker
from meltano.core.utils import truthy
from meltano.core.migration_service import MigrationService
from meltano.api.workers import (
    MeltanoCompilerWorker,
    AirflowWorker,
    APIWorker,
    UIAvailableWorker,
    DbtWorker,
)


logger = logging.getLogger(__name__)


def start_workers(workers):
    def stop_all():
        logger.info("Stopping all background workers...")
        for worker in workers:
            worker.stop()

    # start all workers
    for worker in workers:
        worker.start()

    return stop_all


@cli.group(cls=DefaultGroup, default="start", default_if_no_args=True)
@project(migrate=True)
@click.pass_context
def ui(ctx, project):
    ctx.obj["project"] = project


@ui.command()
@click.option("--reload", is_flag=True, default=False)
@click.option(
    "--bind-port",
    default=5000,
    help="Port to run webserver on",
    envvar="MELTANO_API_PORT",
    type=int,
)
@click.option(
    "--bind",
    default="0.0.0.0",
    help="The hostname (or IP address) to bind on",
    envvar="MELTANO_API_HOSTNAME",
)
@click.pass_context
def start(ctx, reload, bind_port, bind):
    project = ctx.obj["project"]
    tracker = GoogleAnalyticsTracker(project)
    tracker.track_meltano_ui()

    loop = asyncio.get_event_loop()

    # we need to prime the ChildWatcher here so we can
    # call subprocesses asynchronously from threads
    #
    # see https://docs.python.org/3/library/asyncio-subprocess.html#subprocess-and-threads
    # TODO: remove when running on Python 3.8
    asyncio.get_child_watcher()

    workers = []
    if not truthy(os.getenv("MELTANO_DISABLE_AIRFLOW", False)):
        workers.append(AirflowWorker(project))

    workers.append(MeltanoCompilerWorker(project))

    # we need to whitelist the loaders here because not
    # all the loaders support dbt in the first place
    dbt_docs_loader = os.getenv("MELTANO_DBT_DOCS_LOADER", "target-postgres")
    if dbt_docs_loader:
        workers.append(DbtWorker(project, dbt_docs_loader, loop=loop))
    else:
        logging.info(
            "No loader enabled for dbt docs generation, set the MELTANO_DBT_DOCS_LOADER variable to enable one."
        )

    workers.append(UIAvailableWorker("http://localhost:{bind_port}"))
    workers.append(
        APIWorker(
            project,
            f"{bind}:{bind_port}",
            reload=reload or os.getenv("FLASK_ENV") == "development",
        )
    )

    cleanup = start_workers(workers)

    def handle_terminate(signal, frame):
        cleanup()

    signal.signal(signal.SIGTERM, handle_terminate)
    logger.info("All workers started.")


@ui.command()
@click.argument("server_name")
@click.option("--bits", default=256)
@click.option("--overwrite", is_flag=True, default=False)
@click.pass_context
def setup(ctx, server_name, **flags):
    """
    Generates the `ui.cfg` file to keep the server secrets keys.
    """
    project = ctx.obj["project"]
    ui_file_path = project.root_dir("ui.cfg")

    if ui_file_path.exists():
        logging.critical(f"Found secrets in file `{ui_file_path}`, please delete this file to regenerate the secrets.")
        raise click.Abort()

    generate_secret = lambda: secrets.token_hex(int(flags["bits"] / 8))  # in bytes

    config = {
        # "SERVER_NAME": server_name,
        "SESSION_COOKIE_DOMAIN": server_name,
        "SECRET_KEY": generate_secret(),
        "JWT_SECRET_KEY": generate_secret(),
        "SECURITY_PASSWORD_SALT": generate_secret(),
    }

    # Flask doesn't support `configparser` or any other configuration format
    # than plain Python files.
    #
    # Luckily the format is trivial to generate
    with ui_file_path.open("w") as f:
        for k, v in config.items():
            f.write(f"{k} = \"{v}\"\n")
