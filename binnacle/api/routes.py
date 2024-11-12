import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError

from binnacle.adapters.knowledge_base import AbstractKnowledgeBase
from binnacle.adapters.typedb.typedb_knowledge_graph import TypeDbKnowledgeGraph
from binnacle.model import api as api_model
from binnacle.model.cluster import KubeConfigEntry
from binnacle.services import cluster_service as cluster_svc
from binnacle.services import project_service as project_svc
from binnacle.services.unit_of_work import (
    AbstractDatabaseUnitOfWork,
    AbstractUnitOfWork,
    KubernetesUnitOfWork,
    TypeDbUnitOfWork,
)
from binnacle.settings import Settings, get_settings

router = APIRouter()


def get_active_project_name(
    settings: Annotated[Settings, Depends(get_settings)],
    request: api_model.NewProjectRequest | None = None,
):
    """Check for a project name configured in the request or default to the one in the settings

    :param settings: injected dependency to the global settings
    :param request: an optional NewProjectRequest, defaults to None
    :return: the name of the selected project or the default db_name in the settings
    """
    return request.name if request else settings.db_name


def get_kb(
    active_project: Annotated[str, Depends(get_active_project_name)],
    settings: Annotated[Settings, Depends(get_settings)],
    project: str | None = None,
) -> AbstractKnowledgeBase:
    if project is None:
        project = active_project
    kb = TypeDbKnowledgeGraph(db_name=project, url=settings.db_url)
    return kb


def get_db_uow(
    kb: Annotated[AbstractKnowledgeBase, Depends(get_kb)],
) -> AbstractDatabaseUnitOfWork:
    uow = TypeDbUnitOfWork(kb=kb)
    return uow


def get_k8s_uow(
    settings: Annotated[Settings, Depends(get_settings)],
) -> KubernetesUnitOfWork:
    return KubernetesUnitOfWork(settings.kubeconfig_path)


@router.get("/clusters")
def list_clusters(
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[str]:
    clusters = cluster_svc.get_cluster_contexts(config_path=settings.kubeconfig_path)
    return clusters


@router.post(
    "/clusters",
    status_code=status.HTTP_201_CREATED,
)
def register_cluster(
    entry: KubeConfigEntry,
    settings: Annotated[Settings, Depends(get_settings)],
):
    cluster_svc.add_cluster(entry, config_path=settings.kubeconfig_path)


@router.delete("/clusters/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_cluster(
    cluster_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
):
    if not cluster_svc.remove_context(cluster_id, config_path=settings.kubeconfig_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'No cluster named "{cluster_id}" was registered before deleting!',
        )


@router.get("/clusters/{cluster_id}/ns")
def list_clusters(
    cluster_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[str]:
    namespaces = cluster_svc.get_namespaces_in_cluster(cluster_id, config_path=settings.kubeconfig_path)
    ns = [ns.name for ns in namespaces]
    return ns


@router.get("/projects")
def list_projects(
    uow: Annotated[AbstractDatabaseUnitOfWork, Depends(get_db_uow)],
):
    return project_svc.get_projects(uow)


@router.put(
    "/projects",
    status_code=status.HTTP_201_CREATED,
)
def create_project(
    db_uow: Annotated[AbstractDatabaseUnitOfWork, Depends(get_db_uow)],
    k8s_uow: Annotated[KubernetesUnitOfWork, Depends(get_k8s_uow)],
    request: api_model.NewProjectRequest,
    infra: bool = False,
):
    k8s_uow.add_scope(cluster=request.cluster, namespaces=request.namespaces, infra=infra)

    try:
        project_svc.populate(k8s_uow, db_uow, request.name)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.delete("/projects/{project_name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_name: str,
    uow: Annotated[AbstractDatabaseUnitOfWork, Depends(get_db_uow)],
) -> None:
    project_svc.delete_project(uow, project_name)
