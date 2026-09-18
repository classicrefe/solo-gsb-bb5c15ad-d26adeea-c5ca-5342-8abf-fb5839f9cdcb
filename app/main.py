"""FastAPI 入口：全部数据随请求在本地内存中处理。

* POST /resolve  提交仓库快照 + 根包 + 版本约束 + 目标环境（PEP 508 marker
  在请求环境下求值），返回锁定清单或冲突来源链
* POST /upgrade  在现有锁定清单（current_lock）基础上升级单个包：
  复用 /resolve 的快照/根包/环境/marker 语义，选择可行的最高目标版本，
  并最小化非目标包的变化
* GET  /healthz  健康检查
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .repository import (
    SnapshotError,
    build_repository,
    canonical,
    parse_root_specifier,
)
from .resolver import ResolutionLimit, Resolver
from .schemas import (
    InvalidSnapshotResponse,
    ResolveRequest,
    ResolveResponse,
    UnresolvedResponse,
    UpgradeRequest,
    UpgradeResponse,
)
from .upgrade import UpgradeImpossible, execute_upgrade, parse_current_lock

app = FastAPI(
    title="本地依赖解析 API",
    description=(
        "基于 Python packaging（PEP 440/503/508）与回溯搜索的依赖求解器。"
        "所有仓库数据与目标环境随请求提交，本地解析，不访问任何远程索引、"
        "不读取宿主机平台信息。"
    ),
    version="1.2.0",
)


def _conflict_payload(raw: dict) -> dict:
    """把 Resolver.build_conflicts() 的内部结构转为对外 JSON。"""
    return {
        "status": raw["status"],
        "message": raw["message"],
        "conflicts": [
            {
                "subject": c.subject,
                "kind": c.kind,
                "message": c.message,
                "constraints": [
                    {
                        "chain": [
                            {
                                "package": link.package,
                                "version": link.version,
                                "requires": link.requires,
                            }
                            for link in chain
                        ]
                    }
                    for chain in c.chains
                ],
                "partial_selection": c.partial_selection,
            }
            for c in raw["_conflicts"]
        ],
    }


@app.get("/healthz", summary="健康检查")
def healthz() -> dict:
    return {"status": "ok"}


@app.post(
    "/resolve",
    summary="解析依赖并生成确定性锁定清单",
    response_model=ResolveResponse,
    responses={
        409: {"model": UnresolvedResponse, "description": "约束无解或超过搜索上限"},
        422: {"model": InvalidSnapshotResponse, "description": "快照或请求非法"},
    },
)
def resolve(request: ResolveRequest):
    root_canonical = canonical(request.root)
    payload = request.model_dump()

    try:
        repo = build_repository(payload["repository"], payload["target_environment"])
        root_spec = parse_root_specifier(request.specifier)
    except SnapshotError as exc:
        return JSONResponse(
            status_code=422,
            content={"status": "invalid_snapshot", "message": str(exc)},
        )

    if not repo.known(root_canonical):
        return JSONResponse(
            status_code=422,
            content={
                "status": "invalid_snapshot",
                "message": f"根包 {root_canonical!r} 不存在于仓库快照中",
            },
        )

    resolver = Resolver(repo, allow_prereleases=request.allow_prereleases)
    try:
        state = resolver.resolve(root_canonical, root_spec, request.specifier)
    except ResolutionLimit:
        return JSONResponse(
            status_code=409,
            content=_conflict_payload(resolver.build_conflicts(limit=True)),
        )

    if state is not None:
        return resolver.build_output(state, root_canonical)

    return JSONResponse(
        status_code=409,
        content=_conflict_payload(resolver.build_conflicts()),
    )


def _build_changes(
    locked: dict[str, str], current: dict[str, str]
) -> list[dict]:
    """对比升级前后锁定清单，生成按包名排序的变化列表。"""
    changes: list[dict] = []
    for name in sorted(set(locked) | set(current)):
        old = current.get(name)
        new = locked.get(name)
        if old == new:
            continue
        changes.append({"package": name, "from": old, "to": new})
    return changes


@app.post(
    "/upgrade",
    summary="在现有锁定清单上升级单个包（最高可行版本，最小化其余变化）",
    response_model=UpgradeResponse,
    responses={
        409: {"model": UnresolvedResponse, "description": "不存在严格高于当前版本的可行升级"},
        422: {"model": InvalidSnapshotResponse, "description": "快照或请求非法"},
    },
)
def upgrade(request: UpgradeRequest):
    root_canonical = canonical(request.root)
    target_canonical = canonical(request.target)
    payload = request.model_dump()

    try:
        current = parse_current_lock(payload["current_lock"])
        repo = build_repository(payload["repository"], payload["target_environment"])
    except (SnapshotError, ValueError) as exc:
        return JSONResponse(
            status_code=422,
            content={"status": "invalid_snapshot", "message": str(exc)},
        )

    if target_canonical not in current:
        return JSONResponse(
            status_code=422,
            content={
                "status": "invalid_snapshot",
                "message": (
                    f"升级目标 {target_canonical!r} 不存在于 current_lock 中；"
                    f"现有锁定包：{', '.join(sorted(current)) or '（空）'}"
                ),
            },
        )

    if not repo.known(root_canonical):
        return JSONResponse(
            status_code=422,
            content={
                "status": "invalid_snapshot",
                "message": f"根包 {root_canonical!r} 不存在于仓库快照中",
            },
        )

    try:
        result = execute_upgrade(
            repo,
            root_canonical,
            current=current,
            target=target_canonical,
            allow_prereleases=request.allow_prereleases,
        )
    except UpgradeImpossible as exc:
        return JSONResponse(
            status_code=409,
            content=_conflict_payload(
                exc.resolver.build_conflicts(limit=exc.limit)
            ),
        )
    except ResolutionLimit:
        # 最小化枚举阶段超上限：用其自身记录的冲突点给出 limit 负载。
        return JSONResponse(
            status_code=409,
            content={"status": "limit", "message": "搜索超过尝试次数上限",
                     "conflicts": []},
        )

    locked = {pkg: str(ver) for pkg, ver in sorted(result.state.selection.items())}
    current_text = {pkg: str(ver) for pkg, ver in current.items()}
    return {
        "status": "ok",
        "root": root_canonical,
        "root_version": str(result.state.selection[root_canonical]),
        "target": result.target,
        "target_version": result.target_version,
        "locked": locked,
        "changes": _build_changes(locked, current_text),
    }
