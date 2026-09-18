"""HTTP 请求 / 响应的 Pydantic 模型。

所有数据随请求传入，服务端不读取任何远程索引、不做磁盘缓存。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# PEP 503 允许的发行包名：字母数字点/连字符/下划线，首尾为字母或数字
# （单字符包名也合法）。
NAME_PATTERN = r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$"


class DependencyIn(BaseModel):
    """一条声明式依赖（requires_dist 风格，支持 PEP 440 约束 + PEP 508 marker）。"""

    model_config = {"extra": "forbid"}

    name: str = Field(
        ...,
        min_length=1,
        pattern=NAME_PATTERN,
        description="被依赖的发行包名（按 PEP 503 规范化后匹配）。",
    )
    specifier: str = Field(
        "",
        description="PEP 440 版本约束，如 '>=1.0,<2.0'；空字符串表示不约束。",
    )
    marker: str = Field(
        "",
        description=(
            "PEP 508 环境标记表达式（不含分号），如 \"sys_platform == 'linux'\"；"
            "仅允许引用 python_version / sys_platform / platform_machine，"
            "空字符串表示无条件激活。"
        ),
    )


class TargetEnvironmentIn(BaseModel):
    """marker 求值所用的目标环境；三项必填且全部取自请求（不读宿主机）。"""

    model_config = {"extra": "forbid"}

    python_version: str = Field(
        ...,
        min_length=1,
        description="目标解释器版本（PEP 440），如 '3.11'、'3.12.4'。",
    )
    sys_platform: str = Field(
        ...,
        min_length=1,
        description="目标操作系统，对应 sys_platform，如 'linux'、'win32'、'darwin'。",
    )
    platform_machine: str = Field(
        ...,
        min_length=1,
        description="目标 CPU 架构，对应 platform.machine()，如 'x86_64'、'aarch64'、'AMD64'。",
    )


class CandidateIn(BaseModel):
    """仓库快照中的一个候选版本。"""

    model_config = {"extra": "forbid"}

    version: str = Field(..., min_length=1, description="PEP 440 版本号。")
    dependencies: list[DependencyIn] = Field(
        default_factory=list,
        description="该版本声明的依赖（输出会做确定性排序，与声明顺序无关）。",
    )


class ResolveRequest(BaseModel):
    """POST /resolve 的请求体。"""

    model_config = {"extra": "forbid"}

    repository: dict[str, list[CandidateIn]] = Field(
        ...,
        description=(
            "仓库快照：键为包名（按 PEP 503 规范化匹配，大小写/下划线/点/连字符等价），"
            "值为候选版本列表；列表顺序无关，内部按 PEP 440 版本降序排列。"
        ),
    )
    root: str = Field(
        ...,
        min_length=1,
        pattern=NAME_PATTERN,
        description="根包名称；必须存在于 repository 中。",
    )
    target_environment: TargetEnvironmentIn = Field(
        ...,
        description=(
            "必填的目标环境，用于求解决策与每条依赖的 PEP 508 marker；"
            "值完全取自请求，服务端不读取宿主机信息。"
        ),
    )
    specifier: str = Field(
        "",
        description="对根包的 PEP 440 版本约束，默认取最高版本。",
    )
    allow_prereleases: bool = Field(
        False,
        description="是否允许预发布版本（dev/a/b/rc）。默认关闭。",
    )


class UpgradeRequest(BaseModel):
    """POST /upgrade 的请求体：/resolve 的快照/根包/环境语义 + 现有锁定与升级目标。"""

    model_config = {"extra": "forbid"}

    repository: dict[str, list[CandidateIn]] = Field(
        ...,
        description="仓库快照，语义与 POST /resolve 完全一致。",
    )
    root: str = Field(
        ...,
        min_length=1,
        pattern=NAME_PATTERN,
        description="根包名称（与 /resolve 相同）；必须存在于 repository 中。",
    )
    target_environment: TargetEnvironmentIn = Field(
        ...,
        description="目标环境与 marker 语义，与 POST /resolve 完全一致。",
    )
    allow_prereleases: bool = Field(
        False,
        description="是否允许预发布版本（dev/a/b/rc）。默认关闭。",
    )
    current_lock: dict[str, str] = Field(
        ...,
        min_length=1,
        description=(
            "现有的锁定清单：规范化包名 -> PEP 440 版本。升级以此为变化比较基线；"
            "规范化后重名或存在无法按 PEP 440 解析的版本均返回 422。"
        ),
    )
    target: str = Field(
        ...,
        min_length=1,
        pattern=NAME_PATTERN,
        description=(
            "要升级的单个包（按 PEP 503 规范化匹配）；必须存在于 current_lock，"
            "否则返回 422。"
        ),
    )


# --------------------------------------------------------------------- #
# 响应模型
# --------------------------------------------------------------------- #
class EdgeOut(BaseModel):
    model_config = {"extra": "forbid"}

    from_: str = Field(..., alias="from", description="声明方，'root' 表示根请求。")
    to: str = Field(..., description="被依赖方（规范化包名）。")
    specifier: str = Field(..., description="声明的原始约束；空约束显示为 '*'。")


class ResolveResponse(BaseModel):
    model_config = {"extra": "forbid", "populate_by_name": True}

    status: Literal["ok"]
    root: str
    root_version: str
    locked: dict[str, str] = Field(
        ..., description="确定性锁定清单：规范化包名 -> 版本。"
    )
    edges: list[EdgeOut] = Field(..., description="依赖边（已排序）。")


class ChangeOut(BaseModel):
    """单个包相对 current_lock 的变化。"""

    model_config = {"extra": "forbid"}

    package: str = Field(..., description="规范化包名。")
    from_: str | None = Field(
        ...,
        alias="from",
        description="current_lock 中的版本；新增包时为 null。",
    )
    to: str | None = Field(
        ...,
        description="升级后锁定的版本；包被删除时为 null。",
    )


class UpgradeResponse(BaseModel):
    model_config = {"extra": "forbid", "populate_by_name": True}

    status: Literal["ok"]
    root: str
    root_version: str
    target: str = Field(..., description="被升级的包（规范化名）。")
    target_version: str = Field(..., description="升级后的目标版本。")
    locked: dict[str, str] = Field(
        ..., description="升级后的完整锁定清单：规范化包名 -> 版本。"
    )
    changes: list[ChangeOut] = Field(
        ..., description="相对 current_lock 的变化，按包名排序。"
    )


class ChainLinkOut(BaseModel):
    """来源链中的一跳。"""

    model_config = {"extra": "forbid"}

    package: str | None = Field(
        ..., description="提出约束的包名；null 表示请求本身（根需求）。"
    )
    version: str | None = Field(
        ..., description="提出约束的已选版本；根需求时为 null。"
    )
    requires: str = Field(..., description="该跳施加的原始约束文本。")


class ConstraintChainOut(BaseModel):
    model_config = {"extra": "forbid"}

    chain: list[ChainLinkOut] = Field(
        ..., description="根需求 -> ... -> 冲突包的一条约束来源链。"
    )


class ConflictOut(BaseModel):
    model_config = {"extra": "forbid"}

    subject: str = Field(..., description="冲突所针对的包名（规范化）。")
    kind: Literal["incompatible", "no_candidates"]
    message: str
    constraints: list[ConstraintChainOut] = Field(
        ..., description="对该包所有互不兼容的约束及其来源链。"
    )
    partial_selection: dict[str, str] = Field(
        ..., description="冲突点上已确定的选择快照（规范化名 -> 版本）。"
    )


class UnresolvedResponse(BaseModel):
    model_config = {"extra": "forbid"}

    status: Literal["conflict", "limit"]
    message: str
    conflicts: list[ConflictOut]


class InvalidSnapshotResponse(BaseModel):
    model_config = {"extra": "forbid"}

    status: Literal["invalid_snapshot"]
    message: str
