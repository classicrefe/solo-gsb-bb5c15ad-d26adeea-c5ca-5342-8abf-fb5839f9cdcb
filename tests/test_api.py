"""FastAPI 路由层测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def payload(**overrides):
    base = {
        "repository": {
            "app": [
                {"version": "1.0.0",
                 "dependencies": [{"name": "libx", "specifier": ">=1.0"}]},
            ],
            "libx": [
                {"version": "1.0", "dependencies": []},
                {"version": "2.0",
                 "dependencies": [{"name": "liby", "specifier": ""}]},
            ],
            "liby": [{"version": "1.1"}, {"version": "0.9"}],
        },
        "root": "app",
        "specifier": "",
        "allow_prereleases": False,
        "target_environment": {
            "python_version": "3.11",
            "sys_platform": "linux",
            "platform_machine": "x86_64",
        },
    }
    base.update(overrides)
    return base


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_resolve_ok():
    r = client.post("/resolve", json=payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["root_version"] == "1.0.0"
    assert body["locked"] == {"app": "1.0.0", "libx": "2.0", "liby": "1.1"}
    assert body["edges"][0] == {"from": "root", "to": "app", "specifier": "*"}
    assert {"from": "libx==2.0", "to": "liby", "specifier": "*"} in body["edges"]


def test_resolve_conflict_409():
    repo = {
        "app": [{"version": "1.0",
                 "dependencies": [
                     {"name": "left", "specifier": ""},
                     {"name": "right", "specifier": ""},
                 ]}],
        "left": [{"version": "1.0",
                  "dependencies": [{"name": "core", "specifier": "==1.0"}]}],
        "right": [{"version": "1.0",
                   "dependencies": [{"name": "core", "specifier": "==2.0"}]}],
        "core": [{"version": "1.0"}, {"version": "2.0"}],
    }
    r = client.post("/resolve", json=payload(repository=repo))
    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "conflict"
    conflict = body["conflicts"][0]
    assert conflict["subject"] == "core"
    # 每条约束都带来源链，且链从根需求开始
    for entry in conflict["constraints"]:
        chain = entry["chain"]
        assert chain[0]["package"] is None
        assert chain[-1]["requires"] in {"==1.0", "==2.0"}
    assert conflict["partial_selection"]["app"] == "1.0"


def test_resolve_unknown_root_422():
    r = client.post("/resolve", json=payload(root="missing"))
    assert r.status_code == 422
    assert r.json()["status"] == "invalid_snapshot"


def test_resolve_bad_version_422():
    repo = {"app": [{"version": "oops", "dependencies": []}]}
    r = client.post("/resolve", json=payload(repository=repo))
    assert r.status_code == 422
    assert "PEP 440" in r.json()["message"]


def test_resolve_bad_specifier_422():
    repo = {"app": [{"version": "1.0",
                     "dependencies": [{"name": "x", "specifier": ">>>1"}]}]}
    r = client.post("/resolve", json=payload(repository=repo))
    assert r.status_code == 422


def test_pydantic_validation_error_on_extra_field():
    r = client.post("/resolve", json=payload(unknown_field=1))
    assert r.status_code == 422


def test_prerelease_toggle():
    repo = {
        "app": [{"version": "1.0",
                 "dependencies": [{"name": "lib", "specifier": ""}]}],
        "lib": [{"version": "2.0rc1"}, {"version": "1.9"}],
    }
    r1 = client.post("/resolve", json=payload(repository=repo))
    assert r1.json()["locked"]["lib"] == "1.9"
    r2 = client.post(
        "/resolve", json=payload(repository=repo, allow_prereleases=True)
    )
    assert r2.json()["locked"]["lib"] == "2.0rc1"


# --------------------------------------------------------------------- #
# PEP 508 marker / target_environment
# --------------------------------------------------------------------- #
LINUX_X64_PY311 = {
    "python_version": "3.11",
    "sys_platform": "linux",
    "platform_machine": "x86_64",
}


def _marker_repo():
    return {
        "app": [
            {"version": "2.0",
             "dependencies": [
                 {"name": "linux-only", "specifier": "",
                  "marker": "sys_platform == 'linux'"},
                 {"name": "win-only", "specifier": "",
                  "marker": "sys_platform == 'win32'"},
                 {"name": "arm-accel", "specifier": "",
                  "marker": "platform_machine == 'aarch64'"},
                 {"name": "modern-core", "specifier": ">=2",
                  "marker": "python_version >= '3.10'"},
                 {"name": "legacy-core", "specifier": "",
                  "marker": "python_version < '3.10'"},
             ]}
        ],
        "linux-only": [{"version": "1.0"}],
        "win-only": [{"version": "1.0"}],
        "arm-accel": [{"version": "1.0"}],
        "modern-core": [{"version": "2.0"}, {"version": "3.0"}],
        "legacy-core": [{"version": "0.9"}],
    }


def test_markers_linux_environment():
    r = client.post("/resolve", json=payload(
        repository=_marker_repo(), target_environment=LINUX_X64_PY311))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locked"] == {
        "app": "2.0", "linux-only": "1.0", "modern-core": "3.0"
    }
    tos = {e["to"] for e in body["edges"]}
    assert tos == {"app", "linux-only", "modern-core"}


def test_markers_same_snapshot_windows_environment():
    """跨环境：同一仓库快照仅改 target_environment，锁定结果随之变化。"""
    env = {"python_version": "3.9", "sys_platform": "win32",
           "platform_machine": "AMD64"}
    r = client.post("/resolve", json=payload(
        repository=_marker_repo(), target_environment=env))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locked"] == {
        "app": "2.0", "legacy-core": "0.9", "win-only": "1.0"
    }
    tos = {e["to"] for e in body["edges"]}
    assert not (tos & {"linux-only", "arm-accel", "modern-core"})


def test_markers_cross_environment_deterministic():
    body1 = client.post("/resolve", json=payload(
        repository=_marker_repo(), target_environment=LINUX_X64_PY311)).json()
    body2 = client.post("/resolve", json=payload(
        repository=_marker_repo(),
        target_environment=dict(reversed(list(LINUX_X64_PY311.items()))))).json()
    assert body1 == body2


def test_marker_inactive_unsatisfiable_dependency_ignored():
    repo = {
        "app": [{"version": "1.0", "dependencies": [
            {"name": "win-thing", "specifier": ">=42",
             "marker": "sys_platform == 'win32'"},
            {"name": "real", "specifier": "==1.0"},
        ]}],
        "real": [{"version": "1.0"}],
    }
    r = client.post("/resolve", json=payload(
        repository=repo, target_environment=LINUX_X64_PY311))
    assert r.status_code == 200, r.text
    assert r.json()["locked"] == {"app": "1.0", "real": "1.0"}


def test_marker_active_conflict_reports_chain_409():
    repo = {
        "app": [{"version": "1.0", "dependencies": [
            {"name": "left", "specifier": "",
             "marker": "sys_platform == 'linux'"},
            {"name": "right", "specifier": "",
             "marker": "sys_platform == 'linux'"},
        ]}],
        "left": [{"version": "1.0", "dependencies": [
            {"name": "core", "specifier": "==1.0"}]}],
        "right": [{"version": "1.0", "dependencies": [
            {"name": "core", "specifier": "==2.0"}]}],
        "core": [{"version": "1.0"}, {"version": "2.0"}],
    }
    r = client.post("/resolve", json=payload(
        repository=repo, target_environment=LINUX_X64_PY311))
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["subject"] == "core"

    # 切到 win32：两条约束来源均未激活，冲突消失。
    r2 = client.post("/resolve", json=payload(
        repository=repo,
        target_environment={"python_version": "3.11",
                            "sys_platform": "win32",
                            "platform_machine": "AMD64"}))
    assert r2.status_code == 200
    assert r2.json()["locked"] == {"app": "1.0"}


def test_invalid_marker_syntax_422_locates_package_version_dependency():
    repo = {
        "app": [{"version": "1.2.3", "dependencies": [
            {"name": "some-dep", "specifier": ">=1",
             "marker": "python_version >>> '3.9'"}]}]
    }
    r = client.post("/resolve", json=payload(
        repository=repo, target_environment=LINUX_X64_PY311))
    assert r.status_code == 422, r.text
    message = r.json()["message"]
    assert r.json()["status"] == "invalid_snapshot"
    assert "app==1.2.3" in message
    assert "some-dep" in message
    assert "PEP 508" in message


def test_marker_out_of_scope_variable_422():
    repo = {
        "app": [{"version": "1.0", "dependencies": [
            {"name": "x", "specifier": "",
             "marker": "implementation_name == 'cpython'"}]}]
    }
    r = client.post("/resolve", json=payload(
        repository=repo, target_environment=LINUX_X64_PY311))
    assert r.status_code == 422
    message = r.json()["message"]
    assert "implementation_name" in message
    assert "app==1.0" in message and "'x'" in message


def test_marker_extra_variable_422():
    repo = {
        "app": [{"version": "1.0", "dependencies": [
            {"name": "x", "specifier": "", "marker": "extra == 'speedups'"}]}]
    }
    r = client.post("/resolve", json=payload(
        repository=repo, target_environment=LINUX_X64_PY311))
    assert r.status_code == 422
    assert "extra" in r.json()["message"]


def test_target_environment_required_422():
    body = payload()
    del body["target_environment"]
    r = client.post("/resolve", json=body)
    assert r.status_code == 422


def test_target_environment_fields_required_422():
    body = payload()
    body["target_environment"] = {"python_version": "3.11",
                                  "sys_platform": "linux"}
    r = client.post("/resolve", json=body)
    assert r.status_code == 422


def test_target_environment_no_extra_fields_422():
    body = payload()
    body["target_environment"]["os_name"] = "posix"
    r = client.post("/resolve", json=body)
    assert r.status_code == 422


def test_target_environment_bad_python_version_422():
    env = dict(LINUX_X64_PY311, python_version="not-a-version")
    r = client.post("/resolve", json=payload(target_environment=env))
    assert r.status_code == 422
    assert "python_version" in r.json()["message"]


def test_target_environment_blank_value_422():
    env = dict(LINUX_X64_PY311, sys_platform="   ")
    r = client.post("/resolve", json=payload(target_environment=env))
    assert r.status_code == 422
    assert "target_environment" in r.json()["message"]


def test_dependency_marker_extra_field_forbidden_422():
    repo = {
        "app": [{"version": "1.0", "dependencies": [
            {"name": "x", "specifier": "", "markers": "sys_platform=='linux'"}]}]
    }
    r = client.post("/resolve", json=payload(repository=repo))
    assert r.status_code == 422


def test_environment_values_taken_from_request_not_host():
    """宿主为 linux 也必须按请求的 win32 环境求值（不读取真实平台）。"""
    repo = {
        "app": [{"version": "1.0", "dependencies": [
            {"name": "win-only", "specifier": "",
             "marker": "sys_platform == 'win32'"},
        ]}],
        "win-only": [{"version": "1.0"}],
    }
    env = {"python_version": "3.11", "sys_platform": "win32",
           "platform_machine": "AMD64"}
    r = client.post("/resolve", json=payload(
        repository=repo, target_environment=env))
    assert r.status_code == 200, r.text
    assert "win-only" in r.json()["locked"]


# --------------------------------------------------------------------- #
# POST /upgrade
# --------------------------------------------------------------------- #
UPGRADE_ENV = {
    "python_version": "3.11",
    "sys_platform": "linux",
    "platform_machine": "x86_64",
}


def upgrade_body(**overrides):
    base = {
        "repository": {
            "app": [{"version": "1.0",
                     "dependencies": [{"name": "lib"}]}],
            "lib": [
                {"version": "2.0",
                 "dependencies": [{"name": "util"}]},
                {"version": "1.0",
                 "dependencies": [{"name": "util"}]},
            ],
            "util": [{"version": "1.0"}],
        },
        "root": "app",
        "target_environment": UPGRADE_ENV,
        "allow_prereleases": False,
        "current_lock": {"app": "1.0", "lib": "1.0", "util": "1.0"},
        "target": "lib",
    }
    base.update(overrides)
    return base


def test_upgrade_basic():
    r = client.post("/upgrade", json=upgrade_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["target"] == "lib"
    assert body["target_version"] == "2.0"
    assert body["root_version"] == "1.0"
    assert body["locked"] == {"app": "1.0", "lib": "2.0", "util": "1.0"}
    # 目标自身的变化计入 changes；保留当前版本的 util 不出现
    assert body["changes"] == [
        {"package": "lib", "from": "1.0", "to": "2.0"},
    ]


def test_upgrade_response_has_no_edges():
    body = client.post("/upgrade", json=upgrade_body()).json()
    assert "edges" not in body


def test_upgrade_changes_sorted_by_package_name():
    repo = {
        "app": [{"version": "1.0",
                 "dependencies": [{"name": "lib"}]}],
        "lib": [{"version": "2.0", "dependencies": []},
                {"version": "1.0", "dependencies": [{"name": "gone"}]}],
        "gone": [{"version": "1.0"}],
    }
    lock = {"app": "1.0", "lib": "1.0", "gone": "1.0"}
    r = client.post("/upgrade", json=upgrade_body(
        repository=repo, current_lock=lock))
    assert r.status_code == 200
    names = [c["package"] for c in r.json()["changes"]]
    assert names == sorted(names)
    by = {c["package"]: c for c in r.json()["changes"]}
    assert by["gone"] == {"package": "gone", "from": "1.0", "to": None}
    assert by["lib"] == {"package": "lib", "from": "1.0", "to": "2.0"}
    assert "gone" not in r.json()["locked"]


def test_upgrade_addition_change_uses_null_from():
    r = client.post("/upgrade", json=upgrade_body(
        current_lock={"app": "1.0", "lib": "1.0"}))
    by = {c["package"]: c for c in r.json()["changes"]}
    assert by["util"] == {"package": "util", "from": None, "to": "1.0"}


def test_upgrade_target_not_in_lock_422():
    r = client.post("/upgrade", json=upgrade_body(target="absent"))
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "invalid_snapshot"
    assert "absent" in body["message"] and "current_lock" in body["message"]


def test_upgrade_no_higher_version_409():
    r = client.post("/upgrade", json=upgrade_body(target="app"))
    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "conflict"
    assert body["conflicts"][0]["subject"] == "app"


def test_upgrade_highest_feasible_target_version():
    """3.0 因 util==9 不可行；必须回退选可行的最高版本 2.0。"""
    repo = {
        "app": [{"version": "1.0",
                 "dependencies": [{"name": "lib"}]}],
        "lib": [
            {"version": "3.0",
             "dependencies": [{"name": "util", "specifier": "==9"}]},
            {"version": "2.0",
             "dependencies": [{"name": "util"}]},
            {"version": "1.0",
             "dependencies": [{"name": "util"}]},
        ],
        "util": [{"version": "1.0"}],
    }
    r = client.post("/upgrade", json=upgrade_body(repository=repo))
    assert r.status_code == 200
    assert r.json()["target_version"] == "2.0"


def test_upgrade_higher_versions_all_infeasible_409():
    repo = {
        "app": [{"version": "1.0",
                 "dependencies": [{"name": "lib"}]}],
        "lib": [
            {"version": "2.0",
             "dependencies": [{"name": "util", "specifier": "==9"}]},
            {"version": "1.0",
             "dependencies": [{"name": "util"}]},
        ],
        "util": [{"version": "1.0"}],
    }
    r = client.post("/upgrade", json=upgrade_body(repository=repo))
    assert r.status_code == 409
    assert r.json()["status"] == "conflict"


def test_upgrade_minimizes_non_target_changes():
    """无约束 util 可漂移到 3.0，但保留当前 1.0 可行时必须保留。"""
    repo = {
        "app": [{"version": "1.0",
                 "dependencies": [{"name": "lib"}]}],
        "lib": [
            {"version": "2.0", "dependencies": [{"name": "util"}]},
            {"version": "1.0", "dependencies": [{"name": "util"}]},
        ],
        "util": [{"version": "3.0"}, {"version": "1.0"}],
    }
    r = client.post("/upgrade", json=upgrade_body(repository=repo))
    assert r.status_code == 200
    body = r.json()
    assert body["locked"]["util"] == "1.0"
    assert body["changes"] == [{"package": "lib", "from": "1.0", "to": "2.0"}]


def test_upgrade_forced_transitive_drift_picks_highest():
    """util 必须升级时，取最高可行版本（确定性降序）。"""
    repo = {
        "app": [{"version": "1.0",
                 "dependencies": [{"name": "lib"}]}],
        "lib": [
            {"version": "2.0",
             "dependencies": [{"name": "util", "specifier": ">=2"}]},
            {"version": "1.0",
             "dependencies": [{"name": "util"}]},
        ],
        "util": [{"version": "3.0"}, {"version": "2.0"}, {"version": "1.0"}],
    }
    r = client.post("/upgrade", json=upgrade_body(repository=repo))
    assert r.status_code == 200
    assert r.json()["locked"]["util"] == "3.0"


def test_upgrade_root_as_target():
    repo = {
        "app": [{"version": "2.0", "dependencies": [{"name": "x"}]},
                {"version": "1.0"}],
        "x": [{"version": "1.0"}],
    }
    r = client.post("/upgrade", json=upgrade_body(
        repository=repo,
        current_lock={"app": "1.0"},
        target="app",
    ))
    assert r.status_code == 200
    body = r.json()
    assert body["target_version"] == "2.0"
    assert body["root_version"] == "2.0"
    assert body["locked"] == {"app": "2.0", "x": "1.0"}


def test_upgrade_target_name_normalized():
    r = client.post("/upgrade", json=upgrade_body(target="LIB"))
    assert r.status_code == 200
    assert r.json()["target"] == "lib"


def test_upgrade_prerelease_excluded_by_default():
    repo = {
        "app": [{"version": "1.0", "dependencies": [{"name": "lib"}]}],
        "lib": [{"version": "2.0rc1"}, {"version": "1.9"}, {"version": "1.0"}],
    }
    r = client.post("/upgrade", json=upgrade_body(repository=repo))
    assert r.status_code == 200
    assert r.json()["target_version"] == "1.9"


def test_upgrade_prerelease_allowed():
    repo = {
        "app": [{"version": "1.0", "dependencies": [{"name": "lib"}]}],
        "lib": [{"version": "2.0rc1"}, {"version": "1.0"}],
    }
    r = client.post("/upgrade", json=upgrade_body(
        repository=repo, allow_prereleases=True))
    assert r.status_code == 200
    assert r.json()["target_version"] == "2.0rc1"


def test_upgrade_only_prerelease_higher_default_409():
    repo = {
        "app": [{"version": "1.0", "dependencies": [{"name": "lib"}]}],
        "lib": [{"version": "2.0rc1"}, {"version": "1.0"}],
    }
    r = client.post("/upgrade", json=upgrade_body(repository=repo))
    assert r.status_code == 409


def test_upgrade_reuses_marker_semantics():
    """同一快照：linux 不拉 win-only；win32 拉 win-only。"""
    repo = {
        "app": [{"version": "1.0", "dependencies": [{"name": "lib"}]}],
        "lib": [
            {"version": "2.0", "dependencies": [
                {"name": "win-only", "marker": "sys_platform == 'win32'"}]},
            {"version": "1.0"},
        ],
        "win-only": [{"version": "1.0"}],
    }
    r = client.post("/upgrade", json=upgrade_body(
        repository=repo, current_lock={"app": "1.0", "lib": "1.0"}))
    assert r.status_code == 200
    assert r.json()["locked"] == {"app": "1.0", "lib": "2.0"}

    win_env = {"python_version": "3.11", "sys_platform": "win32",
               "platform_machine": "AMD64"}
    r2 = client.post("/upgrade", json=upgrade_body(
        repository=repo,
        current_lock={"app": "1.0", "lib": "1.0"},
        target_environment=win_env))
    assert r2.status_code == 200
    assert r2.json()["locked"] == {"app": "1.0", "lib": "2.0",
                                   "win-only": "1.0"}


def test_upgrade_bad_lock_version_422():
    r = client.post("/upgrade", json=upgrade_body(
        current_lock={"app": "1.0", "lib": "oops"}))
    assert r.status_code == 422
    assert "PEP 440" in r.json()["message"]


def test_upgrade_duplicate_canonical_lock_key_422():
    r = client.post("/upgrade", json=upgrade_body(
        current_lock={"app": "1.0", "lib": "1.0", "LIB": "2.0"}))
    assert r.status_code == 422
    assert "lib" in r.json()["message"]


def test_upgrade_empty_lock_422():
    r = client.post("/upgrade", json=upgrade_body(current_lock={}))
    assert r.status_code == 422


def test_upgrade_unknown_root_422():
    r = client.post("/upgrade", json=upgrade_body(root="missing"))
    assert r.status_code == 422


def test_upgrade_invalid_snapshot_422():
    repo = {"app": [{"version": "nope", "dependencies": []}]}
    r = client.post("/upgrade", json=upgrade_body(
        repository=repo, current_lock={"app": "nope"}))
    assert r.status_code == 422


def test_upgrade_rejects_specifier_field_422():
    r = client.post("/upgrade", json=upgrade_body(specifier=">=1"))
    assert r.status_code == 422


def test_upgrade_deterministic_regardless_of_input_order():
    repo = {
        "app": [{"version": "1.0", "dependencies": [{"name": "lib"}]}],
        "lib": [
            {"version": "2.0", "dependencies": [{"name": "zzz"}]},
            {"version": "1.9", "dependencies": [{"name": "aaa"}]},
            {"version": "1.0"},
        ],
        "zzz": [{"version": "1.0"}],
        "aaa": [{"version": "1.0"}],
    }
    lock = {"app": "1.0", "lib": "1.0"}
    first = client.post("/upgrade", json=upgrade_body(
        repository=repo, current_lock=lock)).json()
    shuffled = {k: list(reversed(v)) for k, v in repo.items()}
    second = client.post("/upgrade", json=upgrade_body(
        repository=shuffled, current_lock=lock)).json()
    assert first == second
