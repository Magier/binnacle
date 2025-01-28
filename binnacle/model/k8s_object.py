from __future__ import annotations  # for forward references

from enum import Enum, auto
from typing import Annotated, Any

from loguru import logger
from pydantic import BaseModel, Field, field_validator, model_validator
from strenum import LowercaseStrEnum, PascalCaseStrEnum, StrEnum
from typing_extensions import Self  # for compatiability with Python <3.11

from binnacle.model.primitives import (
    Asset,
    Attribute,
    FieldConfig,
    IpAddress,
    PodCidr,
    Port,
    Relation,
    RelationPart,
    SpecialField,
    TargetPort,
    Thing,
)
from binnacle.model.utils import map_kind_to_resource_type


class Cluster(Thing):
    name: str
    objects: list[Thing] = []


class OwnerReference(Attribute):
    name: str
    kind: str
    uid: str | None = None


class Label(Attribute):
    key: str
    data: str | bool

    @property
    def value(self):
        return f"{self.key}:{self.data}"

    @field_validator("data")
    @classmethod
    def ensure_data_is_str(cls, value: Any) -> str:
        match value:
            # in K8s the boolean values are lower-case
            case bool():
                return str(value).lower()
            case _:
                return value


class RecommendedLabel(StrEnum):
    Name = "app.kubernetes.io/name"
    Instance = "app.kubernetes.io/instance"
    Version = "app.kubernetes.io/version"
    component = "app.kubernetes.io/component"
    PartOf = "app.kubernetes.io/part-of"
    ManagedBy = "app.kubernetes.io/managed-by"
    # owner/contact person?


class K8sObject(Thing):
    kind: str = "?"
    name: str | SpecialField
    # api_version: str
    resource_type: str | None = None  # for rbac and maybe other mechanisms
    annotations: dict[str, str] | None = Field(None, alias="annotation", exclude=True)
    labels: dict[str, Label] = Field({}, alias="lbl")
    ownership_references: list[OwnerReference] = []  # not present in K8s object if empty
    part_of: MicroService | None = Field(None, exclude=True)

    def __init__(self, **data):
        labels = data.pop("label", [])
        super().__init__(**data)

        # the parsing logic doesn't take the target type into account. A single label will be a regular string, not a list
        if isinstance(labels, str):
            labels = [labels]

        for lbl in labels:
            key, value = lbl.split(":")
            if key in self.labels:
                logger.warning(f"Overwriting label '{key}' on object {self.name}")
            self.labels[key] = Label(key=key, data=value)

        # TODO: properly set ID to the kind/name instead of the ID from the database
        # self.id = f"{self.kind}/{self.name}"
        self.resource_type = map_kind_to_resource_type(self.kind)


class DaemonEndpoint(BaseModel):
    kind: str
    port: int

    @property
    def value(self):
        return self.port


class KubeletEndpoint(DaemonEndpoint):
    kind: str = "kubelet"


class OperatingSystem(LowercaseStrEnum):
    Linux = auto()


class ClusterNode(K8sObject):
    kind: str = "Node"
    name: str
    version: str | None = None
    # status: str | None = None
    os_image: str | None = None
    pod_cidr: PodCidr | None = None
    container_runtime: str | None = None
    kernel_version: str | None = None
    roles: str | None = None
    taints: list[str] | None = None
    # status: NodeStatus | None = None
    internal_ip: IpAddress | None = None
    external_ip: IpAddress | None = None
    hostname: str | None = None
    kernel_version: str | None = None
    kubelet_version: str | None = None
    kube_proxy_version: str | None = None
    os: OperatingSystem | None = None
    os_image: str | None = None


class Namespace(K8sObject):
    kind: str = "Namespace"
    # ns: str | SpecialField | None = None  # temporary workaround so it can be queried as part of then environment relation in a implication
    objects: list[Thing] = []

    def __init__(self, **data):
        super().__init__(**data)
        # self.ns = self.name

    # class Config:
    #     fields = {"objects": {FieldConfig.Exclude: True}}


class MicroService(Thing):
    name: str
    kind: str = "micro-service"
    namespace: str | None = None  # maybe a microservice could span across namespaces?
    pods: list[Pod] = []
    part_of: Namespace | None = Field(None, exclude=True)
    objects: list[K8sObject] = []


class NamespacedObject(K8sObject):
    # TODO properly handle the special case of the namespace
    namespace: Annotated[str | SpecialField | None, FieldConfig.Required] = Field(
        "default", alias="ns"  # special case: if the field is '$' then it will be treated as a variable
    )
    part_of: MicroService | None = Field(None, exclude=True)


class LabelSelection(Relation):
    selector: Annotated[NamespacedObject | None, RelationPart.Source] = None
    target: Annotated[NamespacedObject | None, RelationPart.Target] = None


class Manages(Relation):
    manager: Annotated[Workload | None, RelationPart.Source] = None
    resource: Annotated[K8sObject | None, RelationPart.Target] = None


class ContainerPort(Port):
    protocol: str | None = None  # can be TCP, UDP or SCTP
    host_ip: str | None = None
    host_port: int | None = Field(None, alias="host-port-number")


class SecretKeySelector(Attribute):
    secret_name: str
    secret_key: str
    is_optional: bool | None = None

    @property
    def value(self) -> str:
        # the '?' should signalt the that it's optional
        sfx = "?" if self.is_optional else ""
        return f"{self.secret_name}:{self.secret_key}{sfx}"

    def __str__(self):
        # this is the key used within the manifests
        return "secretKeyRef"


class EnvVarSource(Attribute):
    secret_key_ref: SecretKeySelector | None = None
    config_map_key_ref: str | None = None  # not supported yet
    field_ref: str | None = None  # not supported yet
    resource_field_ref: str | None = None  # not supported yet

    @property
    def value(self) -> str | None:
        val = self.config_map_key_ref
        if self.secret_key_ref is not None:
            val = self.secret_key_ref.secret_name

        return val or self.field_ref or self.resource_field_ref

    def __str__(self):
        # this key is used within the manifests,
        # the EnvVarSource is just an transient grouper for the various fields
        return "valueFrom"


class EnvVar(Attribute):
    name: str
    var_value: str | None = None
    value_from: EnvVarSource | None = None

    @model_validator(mode="after")
    def exactly_one_value_fields_must_be_set(cls, self: Self):
        set_value_fields = [int(p is not None) for p in [self.var_value, self.value_from]]
        assert sum(set_value_fields) <= 1, "no more than one type of value field must be set for EnvVar!"
        return self

    @property
    def value(self) -> str:
        if self.var_value is not None:
            val = self.var_value
        elif self.value_from is not None:
            val = self.value_from.value
        else:
            val = "?"
        # use "key=value" as the value itself, because the name itself can be the same for env vars across entities
        return f"{self.name}={val}"


class EnvSource(Thing):
    name: str  # TODO this name is a k8s.LocalObjectReference ... so model this as a relation
    is_optional: bool | None = None


class SecretEnvSource(EnvSource):
    pass


class ConfigMapEnvSource(EnvSource):
    pass


class EnvFromSource(Attribute):
    prefix: str | None = None
    secret_ref: SecretEnvSource | None = None
    config_map_ref: ConfigMapEnvSource | None = None  

    def get_source_name(self):
        if self.secret_ref is not None:
            return self.secret_ref.name
        else:
            return "dunno what source :("


class SensitiveData(Asset):
    pass


class ServiceAccountToken(SensitiveData):
    pass


class VolumeSource(Thing):
    pass


class OtherVolumeSource(VolumeSource):
    name: str
    type: str


class HostPathType(PascalCaseStrEnum):
    Unset = ""
    # If nothing exists at the given path, an empty directory will be created there
    # as needed with file mode 0755, having the same group and ownership with Kubelet.
    DirectoryOrCreate = auto()
    # A directory must exist at the given path
    Directory = auto()
    # If nothing exists at the given path, an empty file will be created there
    # as needed with file mode 0644, having the same group and ownership with Kubelet.
    FileOrCreate = auto()
    # A file must exist at the given path
    File = auto()
    # A UNIX socket must exist at the given path
    Socket = auto()
    # A character device must exist at the given path
    CharDevice = auto()
    # A block device must exist at the given path
    BlockDevice = auto()


class HostPath(Attribute):  # is k8s:HostPathVolumeSource
    path: str | None = Field(None, exclude=True)  # exclude it because it's the value itself
    type: HostPathType | None = Field(
        HostPathType.Directory, alias="host-path-type"
    )  # None is the same as HostPathType.Unset

    @property
    def value(self):
        return self.path


class Volume(Thing):
    name: str
    host_path: HostPath | None = None
    source: VolumeSource | None = (
        None  
    )


class VolumeMount(Relation):
    name: str  # matches name of a volume
    mount_path: str
    is_read_only: bool | None = None
    sub_path: str | None = None
    sub_path_expr: str | None = None
    user: Annotated[Container | None, FieldConfig.Required, RelationPart.Source] = None
    volume: Annotated[Volume | None, FieldConfig.Required, RelationPart.Target] = None


class ObjectReference(Thing):
    name: str
    kind: str | None = None
    namespace: str | None = None


class ContainerSecurityContext(Attribute):
    privileged: bool | None = None


class Container(Thing):
    name: str = Field(..., alias="container-name")
    image_name: str
    image_pull_policy: str | None = None
    command: str | None = None
    args: str | None = None
    env_vars: list[EnvVar] | None = Field([], alias="env-var")
    env_from_sources: list[EnvFromSource] | None = Field(
    # env_from_sources: Annotated[list[EnvFromSource] | None, {FieldConfig.Selector: ""}] = Field(
        [], alias="env-from-source"
    )
    ports: list[ContainerPort] | None = Field([], alias="container-port")
    volume_mounts: Annotated[list[VolumeMount] | None, {FieldConfig.Selector: "name"}] = Field(
        [], alias="volume-mount-name"
    )
    security_context: ContainerSecurityContext | None = Field(None, alias="container-security-context")

    def __init__(self, **data):
        super().__init__(**data)
        if self.image_pull_policy is None:
            tag = self._get_image_tag()
            self.image_pull_policy = "Always" if tag is None or tag == "latest" else "IfNotPresent"

        # set back-reference to the volume-mount
        for vm in self.volume_mounts:
            vm.user = self

    def _get_image_tag(self):
        if ":" not in self.image_name:
            return None
        img_name, tag = self.image_name.split(":", 1)
        return tag


class PodSecurityContext(Attribute):
    host_network: bool | None = None
    host_pid: bool | None = None
    host_ipc: bool | None = None
    host_users: bool | None = None
    run_as_user: int | None = None
    run_as_group: int | None = None
    run_as_non_root: bool | None = None
    shared_process_namespace: bool | None = None
    supplemental_groups: list[int] | None = None
    fs_group: int | None = None
    fs_group_change_policy: str | None = None  # this actually maps to k8s.PodFSGroupChangePolicy
    se_linux_options: list[str] | None = None  # this actually maps to k8s.SELinuxOptions
    sysctls: list[str] | None = Field(None, alias="sysctl")  # this actually maps to k8s.Sysctl
    seccomp_profile: str | None = None


class PodPhase(PascalCaseStrEnum):
    Pending = auto()
    Running = auto()
    Succeeded = auto()
    Failed = auto()
    Unknown = auto()  # deprecated in v 1.21 (not used since 2015)


class Pod(NamespacedObject):
    kind: str = "Pod"
    containers: Annotated[
        list[Container] | None, {FieldConfig.IsRelatedBy: "contains:container"}
    ] = []  # it's required by k8s, but Binnacle can work without the container info
    automount_service_account_token: bool | None = None  # overrides the flag in ServiceAccount
    service_account_name: str | None = None
    # mounts: list[ServiceAccountToken]   # TODO rework this relationship
    volume_mounts: list[VolumeMount] = []
    volumes: Annotated[list[Volume], {FieldConfig.Selector: "name"}] = Field([], alias="volume-name")
    node_name: str | None = None
    security_context: Annotated[PodSecurityContext | None, FieldConfig.Exclude] = Field(
        None, alias="pod-security-context"
    )
    image_pull_secrets: list[ObjectReference] | None = Field(None, alias="secret")
    env_secret_key_refs: list[str] | None = None
    host_network: bool | None = None
    host_ip: IpAddress | None = None  # `host_ip` and `pod_ip` are the same if `.spec.hostNetwork` is True
    pod_ip: IpAddress | None = None
    phase: PodPhase | None = Field(None, alias="pod-phase")  # part of the pod's status


class Workload(NamespacedObject):
    kind: str = "wl*"
    pods: list[Pod] = Field([], exclude=True)
    selector: dict | None = None


class Deployment(Workload):
    kind: str = "Deployment"
    replicas: int = 1


class ReplicaSet(Workload):
    kind: str = "ReplicaSet"
    owner: Deployment | None = None
    replicas: int = 1


class StatefulSet(Workload):
    kind: str = "StatefulSet"
    replicas: int = 1


class DaemonSet(Workload):
    kind: str = "DaemonSet"
    update_strategy: str | None = None


class ServiceType(str, Enum):
    ClusterIP = "ClusterIP"
    NodePort = "NodePort"
    LoadBalancer = "LoadBalancer"
    ExternalName = "ExternalName"

    def __str__(self) -> str:
        return self.value


class ServicePort(Port):
    protocol: str | None = None
    node_port: int | None = None  # The port on each node on which this service is exposed.
    target_port: int | str | TargetPort | None = None  # target port on pods selected by this service.
    # If it's a string, then it's the port-name, otherwise the port-number
    app_protocol: str | None = (
        None  # used as a hint for implementations to offer richer behavior for protocols that they understand.
    )

    @field_validator("target_port")
    @classmethod
    def ensure_target_port_is_of_type_port(cls, value: int | str | Port) -> Port:
        match value:
            case str():
                return TargetPort(name=value)
            case int():
                return TargetPort(number=value)
            case _:
                return value


class Service(NamespacedObject):
    kind: str = "Service"
    type: ServiceType = Field(ServiceType.ClusterIP, alias="service-type")
    # is the IP address of the service and is usually assigned randomly by the master.
    cluster_ip: str | None = (
        None  # the string "None" is also a valid value in K8s and designates it as a "headless service"
    )
    # external_name: str
    # external_ips: list[str] = None
    ports: list[ServicePort] = Field([], alias="service-port")
    # If empty or not present, the service is assumed to have an
    # external process managing its endpoints, which Kubernetes will not
    # modify. Only applies to types ClusterIP, NodePort, and LoadBalancer.
    selector: dict | None = None
    managed_pods: list[Pod] = Field([], exclude=True)


class SecretType(Enum):
    Opaque = "Opaque"
    ServiceAccountToken = "kubernetes.io/service-account-token"
    DockerConfig = "kubernetes.io/dockercfg"
    DockerConfigJson = "kubernetes.io/dockerconfigjson"
    BasicAuth = "kubernetes.io/basic-auth"
    SshAuth = "kubernetes.io/ssh-auth"
    Tls = "kubernetes.io/tls"
    BootstrapToken = "bootstrap.kubernetes.io/token"

    def __str__(self) -> str:
        return self.value


class Secret(NamespacedObject):
    kind: str = "Secret"
    secret_type: str = str(SecretType.Opaque)  # definition of custom types is allowed
    data: dict[str, str] | None = Field(None, exclude=True)
    sa_name: str | None = Field(None, alias="service-account-name")


class ServiceAccount(NamespacedObject):
    kind: str = "ServiceAccount"
    automount_service_account_token: bool | None = None  # overrides the flag in ServiceAccount
    secrets: Annotated[list[ObjectReference], {FieldConfig.Selector: "name"}] = Field(
        [], alias="secret-name"
    )  # list of secrets allowed to be used by pods running using this ServiceAccount


class BindingEntity(Thing):
    kind: str = Field(..., exclude=True)
    name: str
    api_group: str | None = Field(None, exclude=True)

    def __init__(self, **data):
        super().__init__(**data)

        if self.api_group is None:
            self.api_group = ""


class RbacSubject(BindingEntity):
    ns: str | None = None
    # `kind` of object being referenced. Values defined by this API group are "User", "Group", and "ServiceAccount".
    # If the Authorizer does not recognized the kind value, the Authorizer should report an error.

    def __init__(self, **data):
        # api group defaults to:
        # - "" for ServiceAccount subjects.
        # - "rbac.authorization.k8s.io" for User and Group subjects.
        # from https://github.com/kubernetes/kubernetes/blob/master/pkg/apis/rbac/types.go

        # namespace
        # if object is non-namespaced (e.g. User or Group) and this value is not empty, the auth. should report an error
        super().__init__(**data)
        if self.kind.lower() == "serviceaccount":
            self.kind = "service-account"
        elif self.kind in ["user", "group"]:
            # Ensure namespace is not set, as this is invalid
            self.ns = None

    def __str__(self) -> str:
        # special case: the subject itself is only a proxy for other objects
        return self.kind


class RoleRef(BindingEntity):
    kind: str = "Role"

    # TODO api group is mandatory
    def __str__(self) -> str:
        # special case: the subject itself is only a proxy for other objects
        return self.kind


class RoleBinding(NamespacedObject):
    kind: str = "RoleBinding"
    subjects: list[BindingEntity] = []
    role_ref: BindingEntity | None = None


class RbacRule(Thing):
    api_groups: list[str] = Field(..., alias="api-group")
    verbs: list[str] = Field(..., alias="verb")
    resources: list[str] | None = Field(None, alias="resource-type")
    resource_names: list[str] | None = Field(None, alias="resource-name")


class Role(NamespacedObject):
    kind: str = "Role"
    rules: Annotated[list[RbacRule], {FieldConfig.IsRelatedBy: "ruleset:set"}] = []


class SecretVolumeSource(VolumeSource):
    secret_name: str
    default_mode: int | None = None
    items: list | None = None
    is_optional: bool | None = None


class VolumeProjectionSource(Thing):
    pass


class ProjectedVolumeSource(VolumeSource):
    sources: list[VolumeProjectionSource] = []
    default_mode: int = 0


class ServiceAccountTokenProjection(VolumeProjectionSource):
    audience: str | None = None
    expiration_seconds: int
    path: str


class SecretProjection(VolumeProjectionSource):
    secret_name: str
    items: list = []
    is_optional: bool | None = None


class OtherProjection(VolumeProjectionSource):
    value: str


VolumeMount.model_rebuild()
K8sObject.model_rebuild()
# K8sObject.model_rebuild(**locals())
Namespace.model_rebuild()
MicroService.model_rebuild()
