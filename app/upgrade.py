"""POST /upgrade 的升级求解。

复用 /resolve 的全部语义：同一份仓库快照（marker 已按目标环境求值）、
同一个根包与同一套确定性回溯求解器。升级语义在其上分两步实现：

1. **确定目标版本**：只考虑严格高于 ``current_lock[target]`` 的候选，按
   PEP 440 从高到低逐个钉死（``==版本``）做一次完整解析，第一个可行版本
   即“能完成整体解析的最高版本”；全部不可行则升级无解（API 层映射 409）。
2. **最小化非目标包变化**：目标版本钉死后枚举可行解（分支定界），最小化
   非目标包相对 ``current_lock`` 的新增、删除与版本变化总数；变化数相同
   时沿用 /resolve 既有的确定性次序（MRV 展开、候选 PEP 440 降序）。

第一步同时得到的可行解作为第二步的初始上界（best），保证分支定界至少
返回一个解，且平手时保留“默认确定性次序下的第一个可行解”。
"""

from __future__ import annotations

from dataclasses import dataclass

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from .repository import Candidate, Repository, canonical
from .resolver import (
    DEFAULT_ATTEMPT_LIMIT,
    DEFAULT_DEPTH_LIMIT,
    Requirement,
    ResolutionLimit,
    Resolver,
    State,
    _LIMIT_SENTINEL,
    _combined_spec,
    _req_sort_key,
)


class UpgradeImpossible(Exception):
    """没有任何严格高于当前版本的目标版本能完成整体解析。"""

    def __init__(self, resolver: Resolver, *, limit: bool = False):
        # 携带一个已记录冲突点的求解器，供 API 层构建 409 负载。
        self.resolver = resolver
        self.limit = limit


@dataclass(frozen=True)
class UpgradeResult:
    state: State
    target: str
    target_version: str


def root_requirement(target: str, spec: SpecifierSet, raw: str) -> Requirement:
    """构造一条根级需求（origin=None）。"""
    return Requirement(canonical(target), spec, raw.strip() or "*", None, 0)


def parse_current_lock(
    raw_lock: dict[str, str],
) -> dict[str, Version]:
    """规范化 current_lock：键按 PEP 503 规范化，值按 PEP 440 解析。

    规范化后重名（如 ``A_B`` 与 ``a-b`` 同时出现）视为非法请求；
    无法解析的版本号同样非法（均由 API 层映射为 422）。
    """
    lock: dict[str, Version] = {}
    for raw_name, raw_version in raw_lock.items():
        name = canonical(raw_name)
        text = str(raw_version).strip()
        try:
            version = Version(text)
        except InvalidVersion as exc:
            raise ValueError(
                f"current_lock 中包 {name!r} 的版本 {raw_version!r} 不是合法的 PEP 440 版本"
            ) from exc
        if name in lock and lock[name] != version:
            raise ValueError(
                f"current_lock 中 {name!r} 出现多次且版本不一致"
            )
        lock[name] = version
    return lock


class _MinimumChangeResolver(Resolver):
    """目标版本钉死后，枚举可行解并最小化相对 current_lock 的变化数。

    搜索骨架与父类 ``_solve`` 完全一致（同样的需求折叠、MRV、候选降序、
    写时复制回溯），区别只是：
    * 目标包被钉死为唯一候选（``==target_version``）；
    * 每个包优先尝试 current_lock 中的当前版本，其余候选仍按 PEP 440 降序；
    * 不提前返回首个可行解，而是在叶子处按“非目标包变化数”更新最优解，
      并用该计数做分支定界剪枝。

    候选枚举保持父类的确定性顺序，所以第一个可行解（探测阶段得到的种子）
    按该顺序就是平手时应保留的解；计数相同的后到解不会替换它。
    """

    def __init__(
        self,
        repository: Repository,
        *,
        current: dict[str, Version],
        target: str,
        target_version: Version,
        allow_prereleases: bool = False,
        attempt_limit: int = DEFAULT_ATTEMPT_LIMIT,
        depth_limit: int = DEFAULT_DEPTH_LIMIT,
    ):
        super().__init__(
            repository,
            allow_prereleases=allow_prereleases,
            attempt_limit=attempt_limit,
            depth_limit=depth_limit,
        )
        self.current = current
        self.target = canonical(target)
        self.target_version = target_version
        self.best_state: State | None = None
        self.best_changes: int | None = None

    # ------------------------------------------------------------------ #
    # 候选次序：当前版本优先（钉死目标包），其余严格按 PEP 440 降序
    # ------------------------------------------------------------------ #
    def _ordered_candidates(
        self, subject: str, eligible: list[Candidate]
    ) -> list[Candidate]:
        if subject == self.target:
            return [c for c in eligible if c.version == self.target_version]
        pinned = self.current.get(subject)
        if pinned is None:
            return list(eligible)
        current = [c for c in eligible if c.version == pinned]
        others = [c for c in eligible if c.version != pinned]
        return current + others

    @staticmethod
    def _leaf_changes(selection: dict[str, Version],
                      current: dict[str, Version], target: str) -> int:
        """叶子处的非目标包变化数：新增 + 删除 + 版本变化。"""
        changes = 0
        for name, version in selection.items():
            if name == target:
                continue
            old = current.get(name)
            if old is None or old != version:
                changes += 1
        changes += sum(1 for name in current if name not in selection and name != target)
        return changes

    def _branch_changes(self, selection: dict[str, Version]) -> int:
        """部分解已产生的变化数（只含已选包；删除在叶子补记）。"""
        changes = 0
        for name, version in selection.items():
            if name == self.target:
                continue
            old = self.current.get(name)
            if old is None or old != version:
                changes += 1
        return changes

    def search_minimum(
        self,
        root_name: str,
        root_spec: SpecifierSet,
        root_raw: str,
        extra: list[Requirement],
        seed: State,
    ) -> State:
        """以 seed（首个可行解）为初始上界，返回变化数最小的可行解。"""
        self.best_state = seed
        self.best_changes = self._leaf_changes(
            seed.selection, self.current, self.target
        )
        root_req = Requirement(
            canonical(root_name), root_spec, root_raw.strip() or "*", None, 0
        )
        result = self._opt(State(), [root_req, *extra], depth=0)
        if result is _LIMIT_SENTINEL:
            if self.max_depth > self.depth_limit:
                raise ResolutionLimit(f"依赖链深度超过上限（{self.depth_limit}）")
            raise ResolutionLimit("搜索超过尝试次数上限")
        return self.best_state  # type: ignore[return-value]

    def _opt(self, in_state: State, pending: list[Requirement], *, depth: int):
        state = in_state.clone()
        self.max_depth = max(self.max_depth, depth)
        if depth > self.depth_limit:
            return _LIMIT_SENTINEL

        # --- 第一步：待处理需求按包归组（与父类一致） -------------------
        groups: dict[str, list[Requirement]] = {}
        for r in sorted(pending, key=_req_sort_key):
            groups.setdefault(r.subject, []).append(r)

        # --- 第二步：已选定的包只校验，不重新展开 -----------------------
        for subject in sorted(groups):
            if subject not in state.selection:
                continue
            new_reqs = groups.pop(subject)
            reqs = state.constraints[subject] + new_reqs
            combined = _combined_spec(reqs)
            selected = state.selection[subject]
            for r in new_reqs:
                if r.origin is not None:
                    state.add_edge(
                        (r.origin[0], r.origin[1], subject, r.raw_spec)
                    )
            if not self._version_satisfies(selected, combined):
                return None
            state.append_constraint(subject, reqs)

        # --- 叶子：完整可行解，按变化数更新最优 -------------------------
        if not groups:
            changes = self._leaf_changes(
                state.selection, self.current, self.target
            )
            # 严格小于：平手保留先到解（确定性顺序下即种子/首个可行解）。
            if self.best_changes is None or changes < self.best_changes:
                self.best_changes = changes
                self.best_state = state
            return state

        # 下界剪枝：后续展开只能新增/提高变化数；已达到上界的分支不可能更优
        # （等于上界也不必继续：平手按确定性顺序保留先到解）。
        if self._branch_changes(state.selection) >= (self.best_changes or 0):
            return None

        # --- 第三步：MRV 展开顺序与父类一致 ------------------------------
        def mrv_key(name: str) -> tuple[int, str]:
            combined = _combined_spec(groups[name])
            return (len(self._eligible(name, combined)), name)

        chosen = min(groups, key=mrv_key)
        chosen_reqs = sorted(groups.pop(chosen), key=_req_sort_key)
        combined = _combined_spec(chosen_reqs)
        eligible = self._eligible(chosen, combined)
        ordered = self._ordered_candidates(chosen, eligible)

        state.append_constraint(chosen, chosen_reqs)
        for r in chosen_reqs:
            if r.origin is not None:
                state.add_edge(
                    (r.origin[0], r.origin[1], chosen, r.raw_spec)
                )
        # 优化器不构建冲突来源链，chosen_for 无需维护。

        if not ordered:
            return None

        rest = [r for reqs in groups.values() for r in reqs]

        # --- 第四步：当前版本优先、其余 PEP 440 降序，逐分支枚举 --------
        for cand in ordered:
            self.attempts += 1
            if self.attempts > self.attempt_limit:
                return _LIMIT_SENTINEL

            branch = state.clone()
            branch.selection[chosen] = cand.version

            introduced = [
                Requirement(
                    dep.name,
                    dep.spec,
                    dep.raw_spec,
                    (chosen, str(cand.version)),
                    order,
                )
                for order, dep in enumerate(cand.deps)
            ]
            result = self._opt(
                branch, introduced + rest, depth=depth + 1
            )
            if result is _LIMIT_SENTINEL:
                return _LIMIT_SENTINEL
        return None


def execute_upgrade(
    repository: Repository,
    root_name: str,
    *,
    current: dict[str, Version],
    target: str,
    allow_prereleases: bool = False,
    attempt_limit: int = DEFAULT_ATTEMPT_LIMIT,
    depth_limit: int = DEFAULT_DEPTH_LIMIT,
) -> UpgradeResult:
    """执行升级；无可行升级抛 UpgradeImpossible，超上限抛 ResolutionLimit。"""
    root_name = canonical(root_name)
    target = canonical(target)
    current_version = current[target]

    def make_resolver() -> Resolver:
        return Resolver(
            repository,
            allow_prereleases=allow_prereleases,
            attempt_limit=attempt_limit,
            depth_limit=depth_limit,
        )

    # “严格高于当前版本”的候选仍须遵守与 /resolve 相同的预发布策略
    # （allow_prereleases 或约束谓词显式放行）；直接用仓库原始候选会让
    # 钉死探针里的 ==x.rc1 谓词按 pip 语义意外放行预发布。
    higher = make_resolver()._eligible(
        target, SpecifierSet(f">{current_version}")
    )

    # 第一步：目标版本从高到低，钉死探测整体可解析性。
    seed_state: State | None = None
    chosen_target: Candidate | None = None
    for cand in higher:
        pin = root_requirement(target, SpecifierSet(f"=={cand.version}"),
                               f"=={cand.version}")
        probe = make_resolver()
        try:
            state = probe.resolve(
                root_name, SpecifierSet(), "*", extra=[pin]
            )
        except ResolutionLimit:
            # 超限时用当前探测状态构建 limit 报告（409）。
            raise UpgradeImpossible(probe, limit=True)
        if state is not None:
            seed_state = state
            chosen_target = cand
            break

    if seed_state is None or chosen_target is None:
        # 记录失败原因用于 409 冲突负载：对目标施加 >当前版本 的根级下界，
        # 求解器会沿来源链记录 no_candidates / incompatible 冲突点。
        conflict_resolver = make_resolver()
        bound = root_requirement(
            target,
            SpecifierSet(f">{current_version}"),
            f">{current_version}",
        )
        try:
            conflict_resolver.resolve(
                root_name, SpecifierSet(), "*", extra=[bound]
            )
        except ResolutionLimit:
            raise UpgradeImpossible(conflict_resolver, limit=True)
        raise UpgradeImpossible(conflict_resolver)

    # 第二步：固定目标版本，枚举可行解，最小化非目标包变化数。
    pin = root_requirement(
        target,
        SpecifierSet(f"=={chosen_target.version}"),
        f"=={chosen_target.version}",
    )
    optimizer = _MinimumChangeResolver(
        repository,
        current=current,
        target=target,
        target_version=chosen_target.version,
        allow_prereleases=allow_prereleases,
        attempt_limit=attempt_limit,
        depth_limit=depth_limit,
    )
    best = optimizer.search_minimum(
        root_name, SpecifierSet(), "*", [pin], seed_state
    )
    return UpgradeResult(
        state=best,
        target=target,
        target_version=str(chosen_target.version),
    )
