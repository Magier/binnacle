from enum import auto
from typing import Annotated

from pydantic import Field, model_validator
from strenum import PascalCaseStrEnum, UppercaseStrEnum
from typing_extensions import Self  # for compatiability with Python <3.11

from binnacle.model.k8s_object import (
    ClusterNode,
    K8sObject,
    MicroService,
    NamespacedObject,
    ObjectReference,
    Pod,
    Service,
)
from binnacle.model.primitives import (
    Attribute,
    Cidr,
    FieldConfig,
    IpAddress,
    LabelSelector,
    Port,
    Relation,
    RelationPart,
    Thing,
)


class Internet(Thing):
    kind: str = "Internet"


class Protocol(UppercaseStrEnum):
    TCP = auto()
    UDP = auto()
    SCTP = auto()


class IngressPathType(PascalCaseStrEnum):
    Exact = auto()
    Prefix = auto()
    ImplementationSpecific = auto()


class ServiceBackendPort(Attribute):
    name: str | None = Field(None, alias="port-name")
    number: int | None = Field(None, alias="port-number")

    @property
    def value(self):
        return self.name if self.name is not None else str(self.number)

    @model_validator(mode="after")
    def name_and_number_fields_are_mutually_exclusive(cls, self: Self) -> Self:
        # either 'service' or 'resource' must be set!
        if self.name is not None:
            assert self.number is None, "'number' can't be set along with 'name'"
        else:
            assert self.number is not None, "either 'name' or 'number' must be set"

        return self


class BackendType(PascalCaseStrEnum):
    Service = auto()
    Resource = auto()


class IngressBackend(Thing):
    backend_type: str  # the key in K8s when referencing the specific backend (i.e. 'Service' or 'Resource')
    # # 'service' and 'resource' are mutually exclusive


class IngressServiceBackend(IngressBackend):
    backend_type: str = BackendType.Service
    name: str
    port: ServiceBackendPort = Field(..., alias="service-backend-port")  # the field can't be inherited, thus it has to be explicitely specified


class IngressResourceBackend(IngressBackend):
    backend_type: str = BackendType.Resource
    reference: ObjectReference


class IngressPath(Relation):
    path: str


class HttpIngressPath(IngressPath):
    path_type: IngressPathType | None = Field(None, alias="ingress-path-type")


class IngressRule(Relation):
    path: Annotated[IngressPath, RelationPart.Source]
    backend: Annotated[IngressBackend, RelationPart.Target]
    host: str | None = None


class Ingress(NamespacedObject):
    kind: str = "Ingress"
    ingress_class_name: str | None = None
    rules: Annotated[list[IngressRule] | None, {FieldConfig.IsRelatedBy: "ingress-ruleset:set"}] = Field(None, alias="ingress-rule")
    default_backend: IngressBackend | None = None  # handles requests that don't match any rules


class NetworkRelation(Relation):
    source: Annotated[MicroService | Pod | ClusterNode | Service | Ingress, None, FieldConfig.Required, RelationPart.Source] = None
    destination: Annotated[MicroService | Pod | ClusterNode | Service | Ingress, None, FieldConfig.Required, RelationPart.Target] = None


class NamespaceSelector(LabelSelector):
    pass


class PodSelector(LabelSelector):
    pass


class NetworkPolicyType(PascalCaseStrEnum):
    Ingress = auto()
    Egress = auto()


class IpBlock(Attribute):
    cidr: Cidr
    exceptions: list[Cidr] | None = Field([], alias="except")  # are CIDR sub-blocks must be within the given CIDR

    @property
    def value(self):
        return self.cidr


class NetworkPolicyPeer(Thing):
    pod_selector: PodSelector | None = None
    namespace_selector: NamespaceSelector | None = None
    ip_block: IpBlock | None = None


class NetworkPolicyPort(Thing):
    # differentiating between port and port_name is because of typedb
    # TODO : maybe try to make Union of port and port_name as it's w/ K8s.
    port: int | None = Field(None, alias="port-number")
    port_name: str | None = None
    protocol: str | None = None
    end_port: int | None = None


class NetworkPolicyRule(Relation):
    # if empty or missing: this rule matches all ports (traffic not restricted by port).
    ports: Annotated[list[NetworkPolicyPort] | None, RelationPart.Target] = Field(None, alias="target-port")
    peers: Annotated[list[NetworkPolicyPeer] | None, RelationPart.Target] = Field(None, alias="peer")
    affected_pod: Pod | None = None


class NetworkPolicyIngressRule(NetworkPolicyRule):
    peers: Annotated[list[NetworkPolicyPeer] | None, RelationPart.Source] = Field(None, alias="peer")


class NetworkPolicyEgressRule(NetworkPolicyRule):
    pass


class NetworkPolicy(NamespacedObject):
    kind: str = "network-policy"
    pod_selector: PodSelector = PodSelector(select_all=True)
    policy_types: list[NetworkPolicyType] = Field([], alias="policy-type")
    ingress_rules: Annotated[  # rules are modeled via intermediare `ruleset`
        list[NetworkPolicyIngressRule] | None, {FieldConfig.IsRelatedBy: "network-policy-ruleset:set"}
    ] = None
    egress_rules: Annotated[list[NetworkPolicyEgressRule] | None, {FieldConfig.IsRelatedBy: "network-policy-ruleset:set"}] = None
