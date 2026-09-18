"""仓库快照的规范化与解析。

使用 packaging（PEP 440 / PEP 503）完成名称规范化、版本与约束解析，
并按请求给出的目标环境对 PEP 508 marker 求值（仅保留激活的依赖）。
非法快照或 marker 在这里抛出 SnapshotError，由 API 层映射为 HTTP 422。
"""

from __future__ import annotations

from collections.abc import Mapping

from packaging.requirements import InvalidRequirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .markers import (
    MarkerError,
    build_environment,
    marker_active,
    parse_marker,
)


def canonical(name: str) -> str:
    """PEP 503 规范化包名（小写，点/下划线/连续连字符折叠为单个 '-'）。"""
    return canonicalize_name(name)


class SnapshotError(ValueError):
    """快照或请求中存在非法（无法按 PEP 440/503 解析的）数据。"""


class Dependency:
    __slots__ = ("name", "raw_spec", "spec")

    def __init__(self, name: str, raw_spec: str, spec: SpecifierSet):
        self.name = canonical(name)
        self.raw_spec = raw_spec.strip() or "*"
        self.spec = spec

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"Dependency({self.name!r}, {self.raw_spec!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Dependency):
            return NotImplemented
        return self.name == other.name and self.spec == other.spec

    def __hash__(self) -> int:
        return hash((self.name, str(self.spec)))


class Candidate:
    __slots__ = ("version", "deps")

    def __init__(self, version: Version, deps: list[Dependency]):
        self.version = version
        self.deps = deps


class Repository:
    """按规范化包名索引的不可变快照；候选版本从高到低排列。"""

    def __init__(self, packages: dict[str, list[Candidate]]):
        self._packages = packages

    def candidates(self, name: str) -> list[Candidate]:
        return self._packages.get(canonical(name), [])

    def known(self, name: str) -> bool:
        return canonical(name) in self._packages


def _parse_specifier(raw: str, *, owner: str) -> SpecifierSet:
    text = (raw or "").strip()
    try:
        return SpecifierSet(text)
    except (InvalidSpecifier, InvalidRequirement) as exc:
        raise SnapshotError(f"包 {owner!r} 的版本约束 {raw!r} 不是合法的 PEP 440 约束: {exc}") from exc


def build_repository(
    raw: dict[str, list[dict]], target_environment: Mapping[str, str] | None = None
) -> Repository:
    """把 API 输入构造成 Repository。

    入参为纯字典（API 层对 Pydantic 模型 model_dump 后传入），形如::

        {"pkg": [{"version": "1.0",
                  "dependencies": [{"name": "dep", "specifier": ">=1.0"]}]}]}

    ``target_environment`` 提供 ``python_version`` / ``sys_platform`` /
    ``platform_machine`` 三个键；每条依赖的 PEP 508 marker 在该环境下求值，
    **仅激活的依赖**会进入快照（环境值全部来自请求，不读取宿主机）。
    marker 语法非法或引用越界变量时抛 SnapshotError（API 层映射为 422）。

    候选列表顺序无关，内部按 PEP 440 版本降序排列，保证解析确定性。
    """
    try:
        env = build_environment(target_environment) if target_environment else None
    except MarkerError as exc:
        raise SnapshotError(str(exc)) from exc
    packages: dict[str, list[Candidate]] = {}

    for raw_name, raw_candidates in raw.items():
        cname = canonical(raw_name)
        bucket: dict[Version, Candidate] = {}

        for item in raw_candidates:
            version_text = item["version"].strip()
            try:
                version = Version(version_text)
            except InvalidVersion as exc:
                raise SnapshotError(
                    f"包 {cname!r} 的版本 {item['version']!r} 不是合法的 PEP 440 版本"
                ) from exc

            deps: list[Dependency] = []
            owner = f"{cname}=={version}"
            for dep in item.get("dependencies", []):
                dep_name = dep["name"]
                spec_text = dep.get("specifier", "")
                spec = _parse_specifier(spec_text, owner=owner)
                try:
                    marker = parse_marker(
                        dep.get("marker", ""),
                        location=(
                            f"包 {owner!r} 对 {canonical(dep_name)!r} 的依赖"
                        ),
                    )
                    active = marker_active(marker, env) if env is not None else True
                except MarkerError as exc:
                    raise SnapshotError(str(exc)) from exc
                if not active:
                    # 未激活依赖：不进入约束、回溯、冲突来源链与边。
                    continue
                deps.append(Dependency(dep_name, spec_text.strip(), spec))

            candidate = Candidate(version, deps)
            if version in bucket:
                other = bucket[version]
                if other.deps != deps:
                    raise SnapshotError(
                        f"包 {cname!r} 的版本 {version} 出现多次且依赖声明不一致"
                    )
                # 完全重复则忽略
                continue
            bucket[version] = candidate

        # PEP 440 降序：候选总是从高到低尝试。
        packages[cname] = [bucket[v] for v in sorted(bucket, reverse=True)]

    return Repository(packages)


def parse_root_specifier(raw: str) -> SpecifierSet:
    return _parse_specifier(raw, owner="<root request>")


def parse_current_lock(raw: dict[str, str]) -> dict[str, Version]:
    """校验并规范化 POST /upgrade 的 current_lock：PEP 503 包名 -> PEP 440 版本。

    非法版本、或同一规范化包名对应多个不同版本时抛 SnapshotError
    （API 层映射为 422）。
    """
    lock: dict[str, Version] = {}
    for name, text in raw.items():
        cname = canonical(name)
        version_text = str(text).strip()
        try:
            version = Version(version_text)
        except InvalidVersion as exc:
            raise SnapshotError(
                f"current_lock 中包 {cname!r} 的版本 {text!r} 不是合法的 PEP 440 版本"
            ) from exc
        if cname in lock and lock[cname] != version:
            raise SnapshotError(
                f"current_lock 中包 {cname!r} 出现了多个不同版本"
            )
        lock[cname] = version
    return lock
