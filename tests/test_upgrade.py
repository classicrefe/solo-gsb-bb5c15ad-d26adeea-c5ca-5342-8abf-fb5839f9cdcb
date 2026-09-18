"""POST /upgrade 求解器层测试：最高可行目标版本 + 最小化非目标包变化。"""

from __future__ import annotations

import pytest

from app.repository import build_repository
from app.upgrade import UpgradeImpossible, execute_upgrade, parse_current_lock

ENV = {
    "python_version": "3.11",
    "sys_platform": "linux",
    "platform_machine": "x86_64",
}


def c(version, deps=None):
    return {"version": version, "dependencies": deps or []}


def d(name, spec="", marker=""):
    out = {"name": name, "specifier": spec}
    if marker:
        out["marker"] = marker
    return out


def upgrade(raw_repo, raw_lock, target, root="app", *, allow_prereleases=False,
            env=None, attempt_limit=None):
    repo = build_repository(raw_repo, env or ENV)
    current = parse_current_lock(raw_lock)
    kwargs = {"allow_prereleases": allow_prereleases}
    if attempt_limit is not None:
        kwargs["attempt_limit"] = attempt_limit
    return execute_upgrade(repo, root, current=current, target=target, **kwargs)


def locked_of(result):
    return {k: str(v) for k, v in sorted(result.state.selection.items())}


def test_strictly_higher_only():
    repo = {"app": [c("1.0")], "x": [c("1.0")]}
    with pytest.raises(UpgradeImpossible):
        upgrade(repo, {"app": "1.0"}, "app")


def test_highest_feasible_target_skips_infeasible_top():
    repo = {
        "app": [c("1.0", [d("lib")])],
        "lib": [
            c("3.0", [d("util", "==9")]),
            c("2.0", [d("util")]),
            c("1.0", [d("util")]),
        ],
        "util": [c("1.0")],
    }
    res = upgrade(repo, {"app": "1.0", "lib": "1.0", "util": "1.0"}, "lib")
    assert res.target_version == "2.0"
    assert locked_of(res) == {"app": "1.0", "lib": "2.0", "util": "1.0"}


def test_minimum_change_prefers_kept_versions():
    repo = {
        "app": [c("1.0", [d("lib")])],
        "lib": [c("2.0", [d("util")]), c("1.0", [d("util")])],
        "util": [c("3.0"), c("1.0")],
    }
    res = upgrade(repo, {"app": "1.0", "lib": "1.0", "util": "1.0"}, "lib")
    assert locked_of(res)["util"] == "1.0"


def test_optimizer_finds_strictly_better_solution_than_first_dfs_solution():
    """默认降序 DFS 的首个解选 a 3.0（拖入 c/d 且漂移 b）；
    a 2.5 保持 b==1.0、不引入新包，是严格更优解，必须由枚举找到。"""
    repo = {
        "app": [c("1.0", [d("lib")])],
        "lib": [
            c("3.0", [d("a", ">=2")]),
            c("1.0", [d("a")]),
        ],
        "a": [
            c("3.0", [d("c"), d("d")]),
            c("2.5", [d("b")]),
            c("2.0", [d("b", ">=2")]),
            c("1.0", [d("b")]),
        ],
        "b": [c("2.0"), c("1.0")],
        "c": [c("1.0")],
        "d": [c("1.0")],
    }
    res = upgrade(
        repo,
        {"app": "1.0", "lib": "1.0", "a": "1.0", "b": "1.0"},
        "lib",
    )
    assert res.target_version == "3.0"
    assert locked_of(res) == {
        "app": "1.0", "a": "2.5", "b": "1.0", "lib": "3.0"
    }


def test_removal_counts_as_change():
    repo = {
        "app": [c("1.0", [d("lib")])],
        "lib": [c("2.0", []), c("1.0", [d("gone")])],
        "gone": [c("1.0")],
    }
    res = upgrade(
        repo, {"app": "1.0", "lib": "1.0", "gone": "1.0"}, "lib"
    )
    assert locked_of(res) == {"app": "1.0", "lib": "2.0"}


def test_tie_keeps_deterministic_order():
    """两条路径变化数相同（各引一个全新包）：保留确定性降序的先到解。"""
    repo = {
        "app": [c("1.0", [d("lib")])],
        "lib": [
            c("2.0", [d("zzz")]),
            c("1.9", [d("aaa")]),
            c("1.0"),
        ],
        "zzz": [c("1.0")],
        "aaa": [c("1.0")],
    }
    res = upgrade(repo, {"app": "1.0", "lib": "1.0"}, "lib")
    assert res.target_version == "2.0"
    assert locked_of(res) == {"app": "1.0", "lib": "2.0", "zzz": "1.0"}


def test_root_can_be_target():
    repo = {
        "app": [c("2.0", [d("x")]), c("1.0")],
        "x": [c("1.0")],
    }
    res = upgrade(repo, {"app": "1.0"}, "app")
    assert res.target_version == "2.0"
    assert locked_of(res) == {"app": "2.0", "x": "1.0"}


def test_prerelease_gating_for_higher_candidates():
    repo = {
        "app": [c("1.0", [d("lib")])],
        "lib": [c("2.0rc1"), c("1.0")],
    }
    with pytest.raises(UpgradeImpossible):
        upgrade(repo, {"app": "1.0", "lib": "1.0"}, "lib")
    res = upgrade(
        repo, {"app": "1.0", "lib": "1.0"}, "lib", allow_prereleases=True
    )
    assert res.target_version == "2.0rc1"


def test_marker_filtering_reused():
    repo = {
        "app": [c("1.0", [d("lib")])],
        "lib": [
            c("2.0", [d("win-only", marker="sys_platform == 'win32'")]),
            c("1.0"),
        ],
        "win-only": [c("1.0")],
    }
    linux = upgrade(
        repo, {"app": "1.0", "lib": "1.0"}, "lib", env=ENV
    )
    assert locked_of(linux) == {"app": "1.0", "lib": "2.0"}

    win_env = {"python_version": "3.11", "sys_platform": "win32",
               "platform_machine": "AMD64"}
    win = upgrade(
        repo, {"app": "1.0", "lib": "1.0"}, "lib", env=win_env
    )
    assert locked_of(win) == {"app": "1.0", "lib": "2.0", "win-only": "1.0"}


def test_attempt_limit_surfaces_as_upgrade_impossible_limit():
    """尝试上限在探测阶段触发时，携带 limit 标记（路由映射 409/limit）。"""
    repo = {"app": [c("1.0")]}
    with pytest.raises(UpgradeImpossible) as exc:
        upgrade(repo, {"app": "0.9"}, "app", attempt_limit=0)
    assert exc.value.limit is True


def test_invalid_lock_version_raises_value_error():
    with pytest.raises(ValueError):
        parse_current_lock({"app": "not-a-version"})


def test_duplicate_canonical_lock_key_raises():
    with pytest.raises(ValueError):
        parse_current_lock({"a_b": "1.0", "a-b": "2.0"})
