import os
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from itertools import chain
from multiprocessing import Queue
from pathlib import Path
from typing import Generator, Iterable, List, Union

from loguru import logger
from plum import dispatch
from typedb.driver import (
    ConceptMap,
    SessionType,
    TransactionType,
    TypeDB,
    TypeDBDriver,
    TypeDBDriverException,
    TypeDBOptions,
    TypeDBSession,
)

from binnacle import model
from binnacle.adapters.knowledge_base import AbstractKnowledgeBase
from binnacle.adapters.typedb.object_to_typeql import get_insert_query, to_typeql
from binnacle.adapters.typedb.typedb_to_object import (
    RelationData,
    map_entity_to_domain_object,
    map_relation,
)
from binnacle.utils import measure


def is_db_ready(db_host: str, db_name: str) -> bool:
    try:
        with TypeDB.core_driver(db_host) as driver:
            with driver.session(db_name, SessionType.SCHEMA) as session:
                _db_name = session.database().name()
                return _db_name == db_name
    except Exception as exc:
        print("exception whin checking TypeDB availability", +exc)
        return False


# using the 'localhost' default address led to session being very slow
SCHEMA_DIR = "./schema/"


def reset_database(db_host: str, db_name: str) -> None:
    with TypeDB.core_driver(db_host) as driver:
        delete_database(driver, db_name)


def delete_database(driver: TypeDBDriver, db_name: str) -> None:
    dbs = driver.databases()
    if dbs.contains(db_name):
        dbs.get(db_name).delete()


class TypeDbKnowledgeGraph(AbstractKnowledgeBase):
    def __init__(self, db_name: str, url: str):
        super().__init__(url)
        self.url = url
        self.db_name = db_name
        self.session = None

    def get_databases(self) -> list[str]:
        """List all databases in TypeDB

        :param url: the URL to connect to TypeDB
        :return: a list of database names available in TypeDB
        """
        driver = TypeDB.core_driver(self.url)

        db_names = [db.name for db in driver.databases.all()]
        return db_names

    def create_database(self, name: str):
        try:
            self.write_schema(SCHEMA_DIR, name)
        except TypeDBDriverException as e:
            logger.error(f"Could not write schema: {e}")
            raise RuntimeError("Unable to initialize the knowledge base! Is the server running?")

    def delete_database(self, db_name: str) -> None:
        driver = TypeDB.core_driver(self.url)
        dbs = driver.databases
        if dbs.contains(db_name):
            dbs.get(db_name).delete()

    def write_schema(self, schema_path: str | Path, db_name: str) -> None:
        driver = TypeDB.core_driver(self.url)
        self._recreate_db(driver, db_name)
        schema_path = Path(schema_path)

        schema_files = schema_path.glob("**/*.tql") if schema_path.is_dir() else (schema_path,)

        with driver.session(db_name, SessionType.SCHEMA) as session:
            schema = consolidate_schemas(schema_files)
            with session.transaction(TransactionType.WRITE) as write_tx:
                write_tx.query.define(schema)
                write_tx.commit()

    def _recreate_db(self, driver: TypeDBDriver, db_name: str) -> None:
        logger.debug("Cleaning KG")
        self.delete_database(db_name)
        driver.databases.create(db_name)

    def insert_via_stream(self, queue: Queue):
        """
        Inserts the provided kubernetes objects to the databases via the provided session.
        :param queue: the queue via which the objects will be served
        :return: the number of inserted objects
        """
        # (temporarily) create a new driver here, so it can be used in another process
        driver = TypeDB.core_driver(self.url)
        with driver.session(self.db_name, SessionType.DATA) as session:
            num_inserts = self._insert_k8s_objects_from_queue(session, queue)
            logger.info(f"Inserted {num_inserts} into TypeDB '{self.db_name}'")

    def start_session(self) -> TypeDBSession | None:
        driver = TypeDB.core_driver(self.url)
        try:
            self.session = driver.session(self.db_name, SessionType.DATA)
            return self.session
        except TypeDBDriverException as exc:
            logger.error(exc)
            if self.db_name not in self.get_databases():
                raise ValueError(f"The database with the name '{self.db_name}' does not exist!")
            raise RuntimeError(f"Unable to connect to the knowledge base! Is the server running?")

    def __enter__(self):
        return self.start_session(self)

    def __exit__(self, *args, **kwargs):
        if self.session is not None:
            self.session.close()
            self.session = None

    def _insert_k8s_objects_from_queue(self, session, queue: Queue) -> int:
        """
        Inserts the provided kubernetes objects to the databases via the provided session.
        :param session: the database session used for the transactions.
        :param k8s_objects: a collection of kubernetes objects, which will be inserted.
        :return: the number of inserted objects
        """
        cnt = 0
        while True:
            obj = queue.get()
            if obj is None:
                break
            elif isinstance(obj, Exception):
                # forward any exceptions coming from other process
                raise obj
            cnt += 1
            queries = get_insert_query(obj)
            if isinstance(queries, str):
                queries = [queries]
            self.insert(queries, session=session)
        return cnt

    @dispatch  # type: ignore
    def insert(self, queries: List[str], *, session: TypeDBSession) -> None:
        """Execute several TypeQL queries on the database to insert the specified entities, relations and attributes.

        :param queries: a list of queries, that will be executed
        :param session: a session used for the transaction
        """
        with session.transaction(TransactionType.WRITE) as transaction:
            for q in queries:
                logger.debug("Inserting " + q)
                transaction.query.insert(q)
            transaction.commit()

    @dispatch  # type: ignore
    def insert(self, objects: List[model.DomainObject], *, session: TypeDBSession) -> List[str]:
        """Convert the list of things and relations to their corresponding TypeQL representation and insert them.

        :param objects: a list of objects to be inserted
        :param session: a session used for the transaction
        :return: the queries used to insert the objects
        """
        queries = [q for obj in objects for q in get_insert_query(obj)]
        self.insert(queries, session=session)
        return queries

    @dispatch  # type: ignore
    def delete(self, queries: List[str], *, session: TypeDBSession) -> None:
        """Execute several TypeQL queries on the database to delete the specified entities, relations and attributes.

        :param queries: a list of queries, that will be executed
        :param session: a session used for the transaction
        """
        with session.transaction(TransactionType.WRITE) as transaction:
            for q in queries:
                logger.debug("deleting " + q)
                transaction.query.delete(q)
            transaction.commit()

    @dispatch  # type: ignore
    def delete(self, objects: List[Union[model.Thing, model.Relation]], *, session: TypeDBSession) -> List[str]:
        """Convert the list of things and relations to their corresponding delete queries and execute them.

        :param objects: a list of objects that will be deleted
        :param session: a session used for the transaction
        """
        # TODO temporary workaround; get delete query directly from object instead of from the insert query
        insert_queries = [q for obj in objects for q in get_insert_query(obj)]
        delete_queries = transform_insert_to_delete_queries(insert_queries)
        self.delete(delete_queries, session=session)

    @dispatch
    def query(self, query: str, *, session: TypeDBSession) -> List[model.DomainObject]:
        """Execute the TypeQL query on the database to retrieve data

        :param query: the executed TypeQL query
        :param session: a session used for the transaction
        :return: a list of TypeDB results
        """
        return self.query([query], session=session)

    @dispatch
    def query(self, queries: List[str], *, session: TypeDBSession) -> List[model.DomainObject]:
        """Execute all TypeQL queries on the database to retrieve data

        :param query: the executed TypeQL queries
        :param session: a session used for the transaction
        :return: a list of TypeDB results
        """
        opts = TypeDBOptions()
        opts.infer = True
        results = []
        with session.transaction(TransactionType.READ, options=opts) as rx:
            for query in queries:
                logger.debug(f'Querying "{query}"')
                res_iter = rx.query.match(query)
                results += parse_rows(res_iter, rx=rx)

        elements = flatten_and_deduplicate_elements(results)
        return convert_objects(elements)


def consolidate_schemas(schema_files: list[Path]) -> str:
    """Combine schemas defined in diferent files to a single schema

    :param schema_files: a list of paths to the various schema files
    :return: the resulting schema itself
    """
    schemas = []

    for i, tql_file in enumerate(schema_files):
        with open(tql_file, "r") as f:
            logger.debug(f"Reading schema {tql_file.name}")
            schema = f.read()

            # a schema file starts with a 'define' keyword,
            # drop it for all subsequent files
            if i > 0:
                schema = schema.replace("define", "", 1)
            schemas.append(schema)

    return os.linesep.join(schemas)


def parse_rows(rows: Iterable, rx) -> Generator:
    i = 0
    for i, r in enumerate(rows, 1):
        yield _parse_row(r, rx)
    logger.debug(f" --> parsed {i} rows from TypeDB")


@dataclass
class TypeDbAttribute:
    key: str
    value: str | float | int
    owners: set[str]


def _parse_row(concept_map: ConceptMap, tx) -> tuple[dict, dict, list[TypeDbAttribute]]:
    attrs = []
    entities = {}
    relations = {}

    for concept in concept_map.concepts():
        if concept.is_type():  # skip the sentinel value
            continue
        obj_type = concept.get_type()
        type_name = obj_type.get_label().name

        if concept.is_attribute():
            attrs.append(
                TypeDbAttribute(
                    key=type_name,
                    value=concept.get_value(),
                    owners=set(o.get_iid() for o in concept.get_owners(tx)),
                )
            )
        elif concept.is_relation():
            edges = []
            id = concept.get_iid()
            # a relation consists of multiple edges, which point to the various players
            for role_type, players in concept.get_players(tx).items():
                edge_label = role_type.get_label().name
                for thing in players:
                    edges.append(
                        {
                            "relationship": edge_label,
                            "target": thing.get_iid(),
                            "target_type": thing.get_type().get_label().name,
                        }
                    )

            # relations[id] = TypeDbRelation(id, kind=type_name, edges=edges, is_inferred=concept.is_inferred())
            relations[id] = {
                "id": id,
                "kind": type_name,
                "edges": edges,
                "is_inferred": concept.is_inferred(),
            }
        elif concept.is_entity():
            id = concept.get_iid()
            if id in entities:
                logger.warning(f"'{type_name}' with id {id} is already in the mapping")
            entities[id] = {"kind": type_name, "id": id}
        else:
            logger.warning(f"Encountered an unknown concept {obj_type} when paring the query results!")

    return entities, relations, attrs


@measure
def convert_objects(args: tuple[dict, dict]) -> list[model.Relation | model.Thing]:
    """Convert all 'things' received from TypeDb to the corresponding objects in the domain model.

    :param args: a tuple with all entities as dict and a list of relations
    :return: a list with the converted objects
    """
    entity_dicts, relation_dicts = args

    entities = [map_entity_to_domain_object(e) for e in entity_dicts.values()]
    relation_data = [map_relation(r) for r in relation_dicts.values()]

    resolved_objects = resolve_relations(entities, relation_data)
    return resolved_objects


def resolve_relations(entities: list[model.Thing], relation_data: list[RelationData]) -> list[model.Relation | model.Thing]:
    """Resolve the relation datas with references references to other things or relations
    to their actual instances, if present in the query result.
    All references are tried to be resolved in the order given as parameter.
    If a referred object is not yet instantiated the relation referencing it will be put on the backlog.
      As soon as theunresolved

    :param entities: a list of entity instances
    :param relation_data: a list of data for every relation, include references to other instances
    :return: a list of all the entities and resolved relations
    """
    index = {e.id: e for e in entities}

    backlog = defaultdict(set)
    queue = deque(relation_data)

    while len(queue) > 0:
        r = queue.popleft()
        success = True
        # try to resolve all references, if not, backlog relation for later re-try
        for ref in reversed(r.refs):
            if ref.target in index:
                r.refs.remove(ref)
                r.attributes[ref.relationship] = index[ref.target]
            else:
                success = False
                backlog[ref.target].add(r)

        if success:
            index[r.id] = r.ctor(**r.attributes)

            # check again, if any entries in the backlog can now be further resolved
            if r.id in backlog:
                # add unique entries back to queue
                [queue.append(i) for i in backlog[r.id] if i not in queue]
                del backlog[r.id]

    # try to parse all objects, which can't fully be resolved, maybe the relationship is not required
    for r in chain.from_iterable(backlog.values()):
        if r.id not in index:
            index[r.id] = r.ctor(**r.attributes)

    return list(index.values())


@measure
def flatten_and_deduplicate_elements(res_iter: list) -> tuple[dict, dict]:
    """Remove duplicate entries from the given list.
    Duplicates are identified just by their iid. Nothing else is taken into account.

    :param res_iter: the iterable containing tuples of all the elements and their relations.
    An element in turn is a mapping of several entity IDs to the entities themselves.
    :return: a copy of the list with only unique elements
    """
    entities, relations, attributes = defaultdict(dict), defaultdict(dict), defaultdict(dict)
    for res_entities, res_relations, res_attrs in res_iter:
        for e_id, e in res_entities.items():
            # if there are any additional attributes available, merge them into the known element
            entities[e_id].update(e)

        for r_id, r in res_relations.items():
            if r_id in relations:
                if relations[r_id] != r:
                    logger.error("Relation mismatch! Overwriting previous element")
            else:
                relations[r_id] = r

        # add just the new attributes
        # attributes.extend(i for i in res_attrs if i not in attributes)
        for attr in res_attrs:
            key = (attr.key, attr.value)
            existing_attr = attributes.get(key, None)
            if existing_attr is None:
                attributes[key] = attr
            else:
                attributes[key].owners |= existing_attr.owners

    # place the attributes on the corresponding entities;
    # do this at the end because order of concepts in the concept_map is not guaranteed
    for data, attr in attributes.items():
        for owner_id in attr.owners:
            if owner_id in entities:
                attrs = entities[owner_id].get("attributes", [])
                attrs.append(data)
                entities[owner_id]["attributes"] = attrs
            elif owner_id in relations:
                attrs = relations[owner_id].get("attributes", [])
                attrs.append(data)
                relations[owner_id]["attributes"] = attrs
            else:  # ignore owners which were not explicitely queried
                pass

    return entities, relations


@measure
def transform_insert_to_delete_queries(insert_queries: list[str]) -> list[str]:
    """Transforms a list of insert statements into the correspending delete statements.

    :param insert_queries: the list of insert statements, for which the delete counterpart will be created
    :return: the corresponding delete statements
    """
    delete_queries = []
    for query in insert_queries:
        # ignore the optional 'match' part everything before the insert statement
        match_statement, insert_statement = query.split("insert ")

        # use the insert part 1:1 to match it for the delete statement, along with the previous match statement
        if match_statement == "":
            match_statement = f"match {insert_statement}"
        else:
            match_statement += insert_statement

        statements = [match_statement]

        object_definitions = (obj for s in insert_statement.split(";") if (obj := _find_object_definition(s)) is not None)
        statements.append(f"delete {'; '.join(object_definitions)};")

        delete_queries.append(" ".join(statements))
    return delete_queries


def _find_object_definition(string: str) -> str | None:
    """Extract the object definition from (a part of) a TypeQl statement.
    An object definiton has the pattern '$var isa <entity>'.
    If present, all three parts will be extracted.

    :param string: the (partial) TypeQL statement
    :return: the extracted object definition if found, or None
    """
    if "isa" in string:
        res = re.search(r"(\$[\w\-]+) .*(isa) ([\w\-]+)", string)
        if res is not None:
            return " ".join(res.groups())
    return None


@dispatch  # type: ignore
def for_insert(object: model.Thing) -> str:
    tql = to_typeql(object)
    return f"insert {tql}"


@dispatch  # type: ignore
def for_insert(object: model.Relation) -> str:
    # relations require a `match block`
    tql = to_typeql(object)
    return tql
