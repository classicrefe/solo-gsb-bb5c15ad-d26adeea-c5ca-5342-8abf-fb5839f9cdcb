# 本地依赖解析 API（Python · FastAPI · packaging）

一个完全本地化的软件依赖解析服务：仓库快照随请求提交，基于
[`packaging`](https://packaging.pypa.io/) 实现 PEP 440（版本/约束）、
PEP 503（包名规范化）与 **PEP 508（环境标记 marker）**，通过带回溯的
搜索生成**确定性锁定清单**；无解时返回从根需求到冲突包的**约束来源链**。

除全新解析（`POST /resolve`）外，还支持在现有锁定清单上升级单个包
（`POST /upgrade`）：选择能完成整体解析的**最高**目标版本，并最小化
其它包的变化。

- **无远程访问**：不访问 PyPI 或任何索引，不做磁盘缓存，全部数据在请求内存中处理。
- **目标环境随请求给定**：每条依赖可携带 PEP 508 marker，由请求必填的
  `target_environment`（`python_version` / `sys_platform` / `platform_machine`）
  决定是否激活；**服务端不读取宿主机的任何平台信息**。
- **仅激活依赖参与求解**：marker 为假的依赖不进入全局约束、回溯、冲突来源链与 `edges`。
- **全局唯一版本**：同一包（PEP 503 规范化后）在整棵依赖树中只能选定一个版本。
- **高版本优先**：候选版本严格按 PEP 440 从高到低尝试，失败即回溯到更低版本。
- **环安全**：依赖环（含自环、菱形依赖）不会导致重复展开；环上的新约束仍会校验。
- **确定性**：结果只取决于快照内容与目标环境，与包键顺序、候选列表顺序无关。

---

## 一、单命令启动（Docker Compose）

```bash
docker compose up --build
```

服务启动后监听：

| 项目 | 地址 |
| --- | --- |
| API 根地址 | `http://localhost:8000` |
| 解析接口 | `POST http://localhost:8000/resolve` |
| 单包升级接口 | `POST http://localhost:8000/upgrade` |
| 健康检查 | `GET  http://localhost:8000/healthz` |
| 交互文档（Swagger） | `http://localhost:8000/docs` |
| OpenAPI Schema | `http://localhost:8000/openapi.json` |

端口在 `docker-compose.yml` 的 `ports: ["8000:8000"]` 中调整。

### 不使用 Docker 的本地运行方式

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 二、请求

### `POST /resolve`

请求体字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `repository` | `object` | 是 | 仓库快照：`{包名: [候选版本...]}`；包名按 PEP 503 规范化匹配（大小写/`_`/`.`/`-` 等价） |
| `repository.<pkg>[].version` | `string` | 是 | PEP 440 版本号，如 `1.2.3`、`2.0rc1` |
| `repository.<pkg>[].dependencies` | `array` | 否 | 该版本声明的依赖：`{"name", "specifier", "marker"}` |
| `…dependencies[].name` | `string` | 是 | 被依赖包名（PEP 503 规范化匹配） |
| `…dependencies[].specifier` | `string` | 否 | PEP 440 版本约束，如 `>=1.0,<2.0`；空串表示不约束 |
| `…dependencies[].marker` | `string` | 否 | PEP 508 环境标记表达式（**不含**前导分号），如 `"sys_platform == 'linux'"`；空串表示无条件激活 |
| `root` | `string` | 是 | 根包名，必须存在于 `repository` |
| `target_environment` | `object` | 是 | marker 求值所用的目标环境，**三项全部必填**，值完全取自请求 |
| `target_environment.python_version` | `string` | 是 | 目标 Python 版本（PEP 440），如 `3.11`、`3.12.4` |
| `target_environment.sys_platform` | `string` | 是 | 目标操作系统（对应 `sys.platform`），如 `linux`、`win32`、`darwin` |
| `target_environment.platform_machine` | `string` | 是 | 目标 CPU 架构（对应 `platform.machine()`），如 `x86_64`、`aarch64`、`AMD64` |
| `specifier` | `string` | 否 | 对根包的 PEP 440 约束，如 `>=1.0,<2.0`；空串表示取最高版本 |
| `allow_prereleases` | `boolean` | 否 | 是否允许预发布（`a/b/rc/dev`），默认 `false`（pip 跟随语义：约束显式钉住预发布时仍可用） |

### PEP 508 marker 规则

- 每条依赖可选一个 `marker` 表达式，支持 `and` / `or` / 括号与比较
  （`==`、`!=`、`>=`、`>`、`<`、`<=`、`~=`、`in`、`not in`），例如：

  ```json
  {"name": "pywin32", "specifier": ">=300", "marker": "sys_platform == 'win32'"}
  ```

  ```json
  {"name": "arm-accel", "marker": "platform_machine == 'aarch64' and python_version >= '3.10'"}
  ```

- marker **只允许引用三个变量**：`python_version`、`sys_platform`、
  `platform_machine`。引用任何其它名称（`os_name`、`platform_system`、
  `python_full_version`、`implementation_name`、`extra` 等）都属于越界，
  返回 **422**；marker 语法非法同样返回 **422**。错误信息会定位到
  **包、版本与依赖**，形如：

  ```text
  包 'app==1.2.3' 对 'some-dep' 的依赖 的 marker 'python_version >>> 3.9' 不是合法的 PEP 508 标记: ...
  包 'app==1.0' 对 'x' 的依赖 的 marker 'os_name == "posix"' 引用了不允许的变量 'os_name'；仅允许引用：platform_machine、python_version、sys_platform
  ```

- 环境值**完全取自请求**：服务进程不会调用 `sys` / `platform` 探测宿主机，
  因此在 Linux 容器里提交 `sys_platform: "win32"` 的请求，会严格按
  Windows 环境求解。
- **只有求值为真的依赖激活**：未激活依赖不会进入全局约束、回溯搜索、
  冲突来源链（409 的 `constraints.chain`）与输出的 `edges`；即使它引用了
  不存在的包或无解版本，也完全不影响结果。
- marker 在解析前对快照一次性求值，同一输入（含 `target_environment`）
  的输出逐字节一致。
- 注意：marker 无论激活与否都会做语法与变量范围校验——非法 marker
  即使在该环境下不会生效，也仍然返回 422。

### 成功请求示例（含回溯）

```bash
curl -s -X POST http://localhost:8000/resolve \
  -H 'Content-Type: application/json' \
  -d @examples/resolve.json
```

`web-framework 3.1.0` 要求 `shared-util==2.0.0`，而 `auth-lib` 要求
`<2.0`，求解器回溯改选 `web-framework 2.4.0`：

```json
{
  "status": "ok",
  "root": "app",
  "root_version": "1.0.0",
  "locked": {
    "app": "1.0.0",
    "auth-lib": "0.9.0",
    "shared-util": "1.5.0",
    "web-framework": "2.4.0"
  },
  "edges": [
    {"from": "root", "to": "app", "specifier": "*"},
    {"from": "app==1.0.0", "to": "auth-lib", "specifier": "*"},
    {"from": "app==1.0.0", "to": "web-framework", "specifier": ">=2.0"},
    {"from": "auth-lib==0.9.0", "to": "shared-util", "specifier": "<2.0"},
    {"from": "web-framework==2.4.0", "to": "shared-util", "specifier": ">=1.0,<2.0"}
  ]
}
```

- `locked`：按规范化包名排序的**锁定清单**（包 → 全局唯一版本）。
- `edges`：按声明方/被依赖方排序的**依赖边**；空约束统一显示为 `*`，
  第一条恒为 `root → 根包`。

### 跨环境示例（同一快照，不同 `target_environment`）

同一份仓库快照中的 `app 2.0.0` 声明了按平台 / 架构 / Python 版本激活的
marker 依赖（`linux-helper`、`win-helper`、`speedlib`、`modern-core`、
`legacy-core`）。只改 `target_environment`，锁定结果随之不同：

```bash
# 目标：Linux x86_64 / Python 3.11
curl -s -X POST http://localhost:8000/resolve \
  -H 'Content-Type: application/json' \
  -d @examples/markers-linux.json

# 目标：Windows AMD64 / Python 3.9（同一快照，仅环境不同）
curl -s -X POST http://localhost:8000/resolve \
  -H 'Content-Type: application/json' \
  -d @examples/markers-windows.json
```

Linux 环境（`linux-helper` 拉入 `native-bindings==2`，激活 `speedlib`
与 `modern-core`；`win-helper`、`legacy-core` 未激活，不出现在结果中）：

```json
{
  "status": "ok",
  "root": "app",
  "root_version": "2.0.0",
  "locked": {
    "app": "2.0.0",
    "linux-helper": "1.2.0",
    "modern-core": "4.0.0",
    "native-bindings": "2",
    "speedlib": "1.0.0"
  },
  "edges": [
    {"from": "root", "to": "app", "specifier": "*"},
    {"from": "app==2.0.0", "to": "linux-helper", "specifier": ">=1.0"},
    {"from": "app==2.0.0", "to": "modern-core", "specifier": "*"},
    {"from": "app==2.0.0", "to": "speedlib", "specifier": "*"},
    {"from": "linux-helper==1.2.0", "to": "native-bindings", "specifier": "==2"}
  ]
}
```

Windows 环境（`win-helper` 拉入 `native-bindings==1`，激活
`legacy-core`；`speedlib` 要求 `x86_64` 而未激活）：

```json
{
  "status": "ok",
  "root": "app",
  "root_version": "2.0.0",
  "locked": {
    "app": "2.0.0",
    "legacy-core": "0.9.0",
    "native-bindings": "1",
    "win-helper": "1.2.0"
  },
  "edges": [
    {"from": "root", "to": "app", "specifier": "*"},
    {"from": "app==2.0.0", "to": "legacy-core", "specifier": "*"},
    {"from": "app==2.0.0", "to": "win-helper", "specifier": ">=1.0"},
    {"from": "win-helper==1.2.0", "to": "native-bindings", "specifier": "==1"}
  ]
}
```

即使请求运行在 Linux 容器中，第二次请求仍严格按 `win32` / `AMD64` /
`3.9` 求解——环境值不会回退到宿主机。

### 无解请求示例（返回约束来源链）

```bash
curl -s -X POST http://localhost:8000/resolve \
  -H 'Content-Type: application/json' \
  -d @examples/conflict.json
```

`left` 与 `right` 对 `core` 给出互斥钉死约束，返回 `409`：

```json
{
  "status": "conflict",
  "message": "依赖约束无解，冲突包：core",
  "conflicts": [
    {
      "subject": "core",
      "kind": "incompatible",
      "message": "已选 core==1.0 与约束交集 ==1.0,==2.0 不兼容",
      "constraints": [
        {"chain": [
          {"package": null, "version": null, "requires": "*"},
          {"package": "app", "version": "1.0.0", "requires": "*"},
          {"package": "left", "version": "1.0", "requires": "==1.0"}
        ]},
        {"chain": [
          {"package": null, "version": null, "requires": "*"},
          {"package": "app", "version": "1.0.0", "requires": "*"},
          {"package": "right", "version": "1.0", "requires": "==2.0"}
        ]}
      ],
      "partial_selection": {"app": "1.0.0", "core": "1.0", "left": "1.0", "right": "1.0"}
    }
  ]
}
```

每条 `chain` 都是一跳约束来源：`package=null` 表示请求本身（根需求），
其后每一跳表示“`<package>==<version>` 声明了 `requires`”。同一冲突包上
所有互斥约束的来源链都会列出（回溯中尝试过的候选版本同样会出现）。

`kind` 取值：

- `no_candidates`：约束交集下没有任何候选版本（或该包不在快照中）；
- `incompatible`：包已选定版本后，环/菱形路径带来了不兼容约束。

### 其他状态码

| 状态码 | 含义 |
| --- | --- |
| `200` | 解析成功 |
| `409` | 约束无解（`status: "conflict"`）；或搜索超过内置尝试上限（`status: "limit"`） |
| `422` | 请求/快照非法：非 PEP 440 版本、非法约束串、**marker 语法非法或引用越界变量**、`target_environment` 缺失/字段非法、根包不在快照中、字段校验失败等 |

`GET /healthz` 返回 `{"status":"ok"}`。

---

## 三、`POST /upgrade`：在现有锁定清单上升级单个包

`/upgrade` **完全复用 `/resolve` 的仓库快照、根包、目标环境与 marker 语义**
（同一快照 + 同一 `target_environment` 下激活的依赖集合一致），在此之上额外
接收一份现有锁定清单 `current_lock` 与单个升级目标 `target`。

### 请求体字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `repository` | `object` | 是 | 仓库快照，语义同 `/resolve` |
| `root` | `string` | 是 | 根包名，必须存在于 `repository`（与 `/resolve` 相同） |
| `target_environment` | `object` | 是 | marker 求值环境，三项必填，语义同 `/resolve` |
| `allow_prereleases` | `boolean` | 否 | 是否允许预发布，默认 `false`（与 `/resolve` 同一套预发布过滤） |
| `current_lock` | `object` | 是 | 现有锁定清单：`{规范化包名: PEP 440 版本}`，**至少一项**；作为变化比较基线。值必须是合法 PEP 440 版本，规范化后重名（如 `a_b` 与 `a-b` 同时出现）返回 422 |
| `target` | `string` | 是 | 要升级的**单个**包（PEP 503 规范化匹配）；**必须存在于 `current_lock`，否则 422** |

> `/upgrade` 没有 `specifier` 字段：根包默认不约束（优先沿用当前根版本，
> 必要时也可作为非目标包发生变化）。请求体多传任何字段都会得到 422。

### 选择规则

1. **只考虑严格高于当前版本的目标候选**（PEP 440 比较，含 epoch），并经过与
   `/resolve` 相同的预发布过滤；没有任何更高候选即升级无解。
2. **目标版本取“能完成整体解析的最高版本”**：候选严格从高到低，逐个钉死
   （`==版本`）做一次完整回溯解析，第一个可行版本即选定版本；若全部不可行，
   返回 **409**（冲突负载与 `/resolve` 同构，可从来源链看出目标的下界约束）。
3. **目标版本确定后，在所有可行解中最小化非目标包的变化数**——相对
   `current_lock` 的：
   - **新增**：新锁定中出现、基线中没有的包；
   - **删除**：基线中有、新锁定中不再被任何依赖需要的包；
   - **版本变化**：同名包版本不同（目标包自身的升级也计入 `changes`，但不参与
     “最小化非目标变化”的优化目标）。
4. **变化数相同的解沿用 `/resolve` 的确定性次序**（MRV 展开、候选 PEP 440 降序
   下的第一个可行解），因此结果只取决于快照、环境与 `current_lock`。

### 成功响应

| 字段 | 说明 |
| --- | --- |
| `status` | 固定 `"ok"` |
| `root` / `root_version` | 根包名与最终选定版本 |
| `target` / `target_version` | 被升级包（规范化名）与升级后的版本 |
| `locked` | 升级后的**完整**锁定清单（规范化包名 → 版本） |
| `changes` | 相对 `current_lock` 的变化，**按包名排序**；每项为 `{"package", "from", "to"}`，新增时 `from` 为 `null`，删除时 `to` 为 `null` |

### 请求示例

```bash
curl -s -X POST http://localhost:8000/upgrade \
  -H 'Content-Type: application/json' \
  -d '{
    "repository": {
      "app": [{"version": "1.0.0", "dependencies": [{"name": "web-framework", "specifier": ">=2.0"}]}],
      "web-framework": [
        {"version": "3.1.0", "dependencies": [{"name": "shared-util", "specifier": "==2.0.0"}]},
        {"version": "2.4.0", "dependencies": [{"name": "shared-util", "specifier": ">=1.0,<2.0"}]}
      ],
      "shared-util": [{"version": "2.0.0"}, {"version": "1.5.0"}]
    },
    "root": "app",
    "target_environment": {"python_version": "3.11", "sys_platform": "linux", "platform_machine": "x86_64"},
    "current_lock": {"app": "1.0.0", "shared-util": "1.5.0", "web-framework": "2.4.0"},
    "target": "web-framework"
  }'
```

最高候选 `3.1.0` 要求 `shared-util==2.0.0`，是可行的整体解析，于是目标升到
`3.1.0`，同时 `shared-util` 随之变化（新增/删除/保留的判定与上述规则一致）：

```json
{
  "status": "ok",
  "root": "app",
  "root_version": "1.0.0",
  "target": "web-framework",
  "target_version": "3.1.0",
  "locked": {
    "app": "1.0.0",
    "shared-util": "2.0.0",
    "web-framework": "3.1.0"
  },
  "changes": [
    {"package": "shared-util", "from": "1.5.0", "to": "2.0.0"},
    {"package": "web-framework", "from": "2.4.0", "to": "3.1.0"}
  ]
}
```

新增包形如 `{"package": "new-lib", "from": null, "to": "1.0.0"}`；
不再被需要的包形如 `{"package": "old-lib", "from": "1.0.0", "to": null}`。

### `/upgrade` 状态码

| 状态码 | 含义 |
| --- | --- |
| `200` | 升级成功，返回完整 `locked` 与排序后的 `changes` |
| `409` | 不存在任何严格高于当前版本且能完成整体解析的目标版本（`status:"conflict"`）；或搜索超过内置尝试上限（`status:"limit"`） |
| `422` | `target` 不在 `current_lock`；`current_lock` 为空、含非法 PEP 440 版本或规范化后重名；快照/根包/环境/marker 非法（与 `/resolve` 相同的校验）；多传未知字段 |

---

## 四、解析规则说明

1. **包名规范化**：统一使用 PEP 503（`Some_Pkg`、`some.pkg`、`some-pkg` 视为同一个包）。
2. **环境标记（PEP 508）**：每条依赖的 `marker` 在请求提供的
   `target_environment` 上求值；只允许引用 `python_version` /
   `sys_platform` / `platform_machine`，求值环境不读取宿主机。仅激活的
   依赖进入后续全部求解流程（约束收集、回溯、来源链、`edges`）。
3. **约束交集**：对每个包收集所有激活的传递路径上的 PEP 440 约束，必须同时满足。
4. **选择次序**：在所有未决包中优先展开“可满足候选最少”的包（MRV），
   名称次序兜底；具体候选严格从高版本到低版本尝试。
5. **回溯**：候选版本展开后若与后续传递依赖冲突，放弃该版本，尝试下一个更低版本；
   每次尝试在独立的状态克隆上进行，失败不会污染其他分支。
6. **环处理**：已选定的包被再次要求时只校验约束、不重复展开，因此环不会无限递归；
   若环上带来矛盾约束，按 `incompatible` 报出。
7. **预发布**：默认排除 `a/b/rc/dev`；`allow_prereleases=true` 放开；
   或约束显式钉住预发布（如 `==2.0rc1`）时也会被接受。
8. **确定性**：候选按 PEP 440 降序、输出按键排序，重复解析同一快照与环境结果逐字节一致。

### 安全护栏

- **尝试上限**（默认 100,000 次候选展开）：防止恶意构造的快照造成指数级搜索，
  超出返回 `409 / status:"limit"`。
- **依赖链深度上限**（默认 5,000 层）：超出同样以 `limit` 报出，而不是进程栈溢出。

## 五、项目结构

```
app/
  main.py        # FastAPI 路由与错误映射（/resolve、/upgrade、/healthz）
  resolver.py    # 回溯求解器（候选排序 / MRV / 来源链重建）
  upgrade.py     # 单包升级：最高可行目标版本 + 最小变化枚举（分支定界）
  repository.py  # 快照规范化、PEP 440/503 解析、按环境过滤 marker
  markers.py     # PEP 508 marker 解析/白名单校验/环境求值（不读宿主机）
  schemas.py     # 请求/响应模型
examples/        # curl 示例请求体（含跨环境 marker 示例）
tests/           # 求解器与 HTTP 层测试（pytest + FastAPI TestClient）
Dockerfile
docker-compose.yml
requirements.txt
```

## 六、运行测试

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest -q
```
