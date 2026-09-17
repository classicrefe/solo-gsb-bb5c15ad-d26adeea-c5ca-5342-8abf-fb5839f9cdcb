"""FastAPI 入口：全部数据随请求在本地内存中处理。

* POST /resolve  提交仓库快照 + 根包 + 版本约束 + 目标环境（PEP 508 marker
  在请求环境下求值），返回锁定清单或冲突来源链
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
)

app = FastAPI(
    title="本地依赖解析 API",
    description=(
        "基于 Python packaging（PEP 440/503/508）与回溯搜索的依赖求解器。"
        "所有仓库数据与目标环境随请求提交，本地解析，不访问任何远程索引、"
        "不读取宿主机平台信息。"
    ),
    version="1.1.0",
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
