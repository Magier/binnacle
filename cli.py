import multiprocessing as mp
import sys
from enum import auto
from pathlib import Path

import typer
from loguru import logger
from strenum import UppercaseStrEnum

sys.path.append(str(Path(__file__).resolve().parent))
from binnacle.api.main import start as start_api_server
from binnacle.services.cluster_service import get_default_context_name
from binnacle.services.project_service import populate as populate_knowledge_base
from binnacle.services.project_service import reset_knowledge_base
from binnacle.services.unit_of_work import KubernetesUnitOfWork, TypeDbUnitOfWork
from binnacle.settings import get_settings

app = typer.Typer()


@app.command()
def reset(
    db_host: str = typer.Argument("--db-host", envvar="DB_HOST"),
    db_name: str = typer.Option(get_default_context_name, "--db-name", envvar="DB_NAME"),
):
    typer.echo(f"Resetting knowledge base {db_name}")
    try:
        reset_knowledge_base(db_host, db_name)
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)


@app.command()
def populate(
    db_name: str = typer.Option(get_default_context_name, "--db-name", envvar="DB_NAME"),
    cluster: str = typer.Option(get_default_context_name, "--cluster"),
    namespace: list[str] = typer.Option(None, "--ns", "-n"),
    infra: bool = typer.Option(False, "--infra", "-i"),
):
    try:
        db_uow = TypeDbUnitOfWork(db_name)
        settings = get_settings()
        typer.echo(f'Populate knowledge base "{db_name}"')
        k8s_uow = KubernetesUnitOfWork(settings.kubeconfig_path, cluster)
        k8s_uow.add_scope(cluster=cluster, namespaces=namespace, infra=infra)
        populate_knowledge_base(k8s_uow, db_uow, db_name)
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)


class FileFormat(UppercaseStrEnum):
    PNG = auto()
    SVG = auto()
    YAML = auto()


# @app.command(help="Generate a diagram of the domain model")
# def create_domain_diagram(dest: Path = typer.Option("docs/domain_model.png", "-d", dir_okay=True, file_okay=True)):
#     import erdantic as erd  #  import it locally, so it's not a hard dependency

#     domain_objects = []

#     q = Queue()
#     q.put(model.DomainObject)
#     while not q.empty():
#         obj = q.get()
#         domain_objects.append(obj)

#         for sub in [cls for cls in obj.__subclasses__()]:
#             q.put(sub)

#     diagram = erd.create(*domain_objects)

#     dest_path = dest / "domain_model.png" if dest.is_dir() else dest
#     diagram.draw(dest_path)


@app.command()
def serve(port: int = typer.Option(8000, "--api-port")):
    start_api_server(port=port)


@app.callback()
def main(debug: bool = False):
    if not debug:
        # set the default logging level for the terminal to 'INFO'
        logger.remove()  # remove the pre-configured default handler
        logger.add(sys.stderr, level="INFO")


if __name__ == "__main__":
    mp.set_start_method("spawn")  # force same start method for Linux and Windows
    app()
