from abc import ABC, abstractmethod
from functools import partial
from typing import Iterable, List, Type

from loguru import logger

from binnacle.adapters.k8s.api_discovery import (
    check_authentication,
    load_cluster_objects,
    load_kube_config,
)
from binnacle.adapters.k8s.cluster_management import (
    load_or_create_kubeconfig,
    save_kubeconfig,
)
from binnacle.adapters.knowledge_base import AbstractKnowledgeBase
from binnacle.adapters.typedb.object_to_typeql import TqlBuilder
from binnacle.adapters.typedb.typedb_knowledge_graph import TypeDbKnowledgeGraph
from binnacle.model.cluster import KubeConfig
from binnacle.model.primitives import DomainObject, Relation, Thing
from binnacle.settings import get_settings


class Query:
    pass


class AbstractUnitOfWork(ABC):
    def __init__(self, *args):
        raise NotImplementedError

    def __enter__(self, *args):
        return self

    def __exit__(self, *args):
        pass


class AbstractDatabaseUnitOfWork(AbstractUnitOfWork):
    """A single transaction against a knowledge base.
    This can consist of multiple individual queries, but represent a single 'business transation'
    """

    kb: AbstractKnowledgeBase

    @abstractmethod
    def get_databases(self):
        raise NotImplementedError

    @abstractmethod
    def create_database(self, name: str):
        raise NotImplementedError

    @abstractmethod
    def delete_database(self, name: str):
        raise NotImplementedError

    @abstractmethod
    def temp(self, obj, *arg):
        raise NotImplementedError

    @abstractmethod
    def get(self, *objects: Iterable, **kwargs):
        raise NotImplementedError

    @abstractmethod
    def query_str(self, queries: str | list[str], **kwargs):
        raise NotImplementedError

    @abstractmethod
    def query(self, queries: DomainObject | List[DomainObject], **kwargs) -> Query:
        raise NotImplementedError


class TypeDbUnitOfWork(AbstractDatabaseUnitOfWork):
    def __init__(self, db_name: str | None = None, db_user: str | None = None, db_password: str | None = None, kb: AbstractKnowledgeBase | None = None) -> None:
        """Instantiate the TypeDb specific Unit of Work.
        It manages the reference to the knowledge base.
        Either the knowledgbase name or direct a reference to knowledge base are required

        :param db_name: the name of database in the knowledge base, defaults to None
        :param kb: _description_, defaults to None
        """
        if kb is None:
            # TODO check this
            # if db_name is None:
            #     db_name = get_default_context_name()
            settings = get_settings()
            kb = TypeDbKnowledgeGraph(db_name, url=settings.db_url, user=db_user, password=db_password)
        self.kb = kb
        self._staged_objects = []
        self.temporary_objects = []

        if kb is None and db_name is None:
            raise ValueError("Either name of the database or a reference to the knowledge base is required")

        self.session = None

    def __enter__(self, *args):
        self.session = self.kb.start_session()
        return self

    def __exit__(self, *args):
        if self.kb is not None and self.session is not None and len(self.temporary_objects) > 0:
            self.kb.delete(self.temporary_objects, session=self.session)

    def get_databases(self):
        return self.kb.get_databases()

    def create_database(self, name: str) -> bool:
        self.kb.create_database(name)

    def delete_database(self, name: str) -> bool:
        self.kb.delete_database(name)

    def temp(self, obj: DomainObject) -> DomainObject:
        """Add a temporary object into the knowledge base for 'what-if' queries

        :param obj: the object, which will be added for just this transaction
        :return: the created object itself
        """
        self._staged_objects.append(obj)
        return obj

    def insert(self, *objects: Iterable[Type[DomainObject]], **kwargs):
        self.kb.insert(list(objects), session=self.session)

    def get(self, *objects: Iterable[Type[DomainObject]], **kwargs) -> list[DomainObject]:
        """Retrieve all objects from the knowledge base matching the given types.
        :return: a list of all objects and relations from the knowledge base
        """
        queries = TqlBuilder(*objects).as_get_query(**kwargs, as_single_query=False)

        # add all temporary objects in a single batch right before the actual query
        # of nothing is retrieved, there is no point in adding them in the first place
        self.temporary_objects += self._staged_objects  # signal that these objects need to be deleted again
        if len(self._staged_objects) > 0:
            self.kb.insert(self._staged_objects, session=self.session)
            self._staged_objects = []

        res = self.kb.query(queries, session=self.session)
        return res

    def query_str(self, queries: str | List[str]) -> list[DomainObject]:
        """Retrieve all objects from the knowledge base matching the given queries.

        :param queries: the TypeQL query run against the knowledge base
        :return: a list of all objects and relations from the knowledge base
        """
        if isinstance(queries, str):
            queries = [queries]
        # add all temporary objects in a single batch right before the actual query
        # of nothing is retrieved, there is no point in adding them in the first place
        res = self.kb.query(queries, session=self.session)
        return res

    def query(self, obj: DomainObject | List[DomainObject]) -> Query:
        return []


class KubernetesUnitOfWork(AbstractUnitOfWork):
    def __init__(self, config_path: str, cluster: str | None = None, *args):
        if cluster is None:
            config = load_or_create_kubeconfig(config_path)
            if config.current_context is None or config.current_context == "":
                raise RuntimeError(f"No valid cluster is set or pre-configured in kubeconfig at '{config_path}'. " "Please specify a target cluster with the --cluster argument.")
            cluster = config.current_context
        self.cluster = cluster
        self.namespaces: list[str] | None = None
        self.config_path = config_path

    def __enter__(self, *args):
        return super().__enter__(*args)

    def __exit__(self, *args):
        return super().__exit__(*args)

    def is_ready(self) -> bool:
        if self.cluster is None:
            return False

        # ensure to load the config for the Kubernetes client
        load_kube_config(config_path=self.config_path, used_context=self.cluster)
        has_access = check_authentication()
        return has_access

    def add_scope(self, cluster: str | None, namespaces: list[str], infra: bool = False) -> None:
        if cluster is not None:  # if no cluster is specified, use the active one
            self.cluster = cluster
        self.namespaces = namespaces
        self.include_infra = infra

    def get_object_loader(self, cluster: str | None):
        if cluster is None:
            cluster = self.cluster
        environment_reader = partial(
            load_cluster_objects,
            self.config_path,
            cluster,
            self.namespaces,
            include_infrastructure=self.include_infra,
        )
        return environment_reader

    def use_cluster(self, cluster: str) -> KubeConfig:
        """Update the current context to the given name in the kubeconfig.

        :param cluster: the name of the activated context
        :raises ValueError: if no context with the given name is available
        :return: a reference to the loaded kubeconfig
        """
        config = load_or_create_kubeconfig(file_path=self.config_path)

        if not any(ctx.name == cluster for ctx in config.contexts):
            raise ValueError(f'No cluster named "{cluster}" registered')

        # simply set and write the kube-config, so it will be re-used when loaded
        config.current_context = cluster
        logger.info(f"Settting active cluster to {cluster}")
        save_kubeconfig(config)

        return config
