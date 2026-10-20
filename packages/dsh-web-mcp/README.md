# llm-dsh-web-mcp

让 DSH 原生的 `web_search` / `web_fetch` 工具由本地 **LLMMcp** MCP 服务提供，并在机器上还没有
LLMMcp 时自动安装。

DSH 的 `ctx.web` 是一个 provider 接缝（`@deepseek-ai/dsh-web`）：`@deepseek-ai/dsh-tool-web` 只负责
模型可见的工具，真正检索/抓取由注册进来的 provider 完成。本插件向 `ctx.web` 注册两个 provider
（`searchProvider` / `fetchProvider` 都叫 `llmmcp`），内部通过 **stdio MCP** 调 LLMMcp 的
`web_search` / `web_fetch`。

```
web_search 工具 ─┐                        ┌─ LLMMcpSearchProvider ─┐
                 ├─ ctx.web (dsh-web) ────┤                        ├─ MCP stdio ─ python -m llmmcp
web_fetch  工具 ─┘                        └─ LLMMcpFetchProvider ──┘
```

## 安装后自动就绪

插件本身零依赖，但服务需要在某个 Python 解释器里。首次加载时按下面顺序解析，全程后台进行、
不阻塞宿主启动：

1. `config.command` 指向的解释器（已存在可执行文件）——显式配置优先，永远不做安装。
2. `~/.llmmcp/config.json` 里 `command` 记录的安装结果（上次 bootstrap 留下的）。
3. `autoInstall` 开启时自动安装：
   - 找一个 Python 3.10+（依次试 `config.python`、`py`、`python`、`python3`）；
   - 在 `~/.llmmcp/venv` 建 venv；
   - `pip install` 已发布 wheel（默认取本仓库 GitHub Release 的 v0.1.2 wheel）；
   - 校验 `import llmmcp` 成功后，把解释器路径写回 `~/.llmmcp/config.json`。

安装失败不会影响宿主：插件照常注册 provider，`available()` 返回 false，日志给出 code 与 hint。
企业代理环境下只要能出网（`HTTPS_PROXY` 生效）即可，pip 会复用该代理。

## 目录

| 文件 | 作用 |
|---|---|
| `index.js` | cordis 插件（`name`/`inject`/`apply`）+ 最小 MCP stdio JSON-RPC 客户端 + 两个 provider |
| `runtime.mjs` | 运行时解析与自动安装（venv、pip、写回配置） |
| `scripts/verify.mjs` | 用假 ctx 驱动 provider，**真实拉起 MCP 子进程**（`--mock` 用内置假服务） |
| `scripts/verify-seam.mjs` | 挂载真实 `@deepseek-ai/dsh-web`，走 `ctx.web.search()` / `ctx.web.fetch()` |
| `scripts/verify-runtime.mjs` | 校验解析顺序；加 `--install` 会真的建 venv 并装 wheel |
| `scripts/verify-no-command.mjs` | 无命令时不安装、provider 仍注册但不可用 |
| `scripts/check-profile.mjs` | 离线校验 profile 补丁是否真的生效 |
| `scripts/fake-mcp-server.mjs` | `--mock` 用的假 stdio MCP 服务 |

## 装配

占位符：`%DSH_HOME%`（默认 `%USERPROFILE%\.dsh`）、`<repo>`（本仓库根）、`<llmmcp-python>`
（已装 llmmcp 的解释器；走自动安装时可以留空）。

**1) 让 profile 能按裸名解析插件**：

```powershell
New-Item -ItemType Junction `
  -Path   "$env:USERPROFILE\.dsh\profiles\node_modules\llm-dsh-web-mcp" `
  -Target "<repo>\packages\dsh-web-mcp"
```

**2) 在 `%DSH_HOME%\profiles\desktop\cordis.patch.yml` 末尾追加**：

```yaml
- insert:
    - id: web-mcp
      name: llm-dsh-web-mcp
      config:
        providerId: llmmcp
        autoInstall: true
        toolCallTimeoutMs: 90000
        searchTimeoutS: 55
        fetchTimeoutS: 40
        fetchFormat: markdown
        fetchMaxChars: 400000
        warmup: true

- id: web
  name: '@deepseek-ai/dsh-web'
  config:
    searchProvider: llmmcp
    fetchProvider: llmmcp
```

`command` / `args` 都可以省略：省略时走 `runtime.mjs` 的解析与自动安装，并把 `-m llmmcp`
作为默认参数。该补丁文件含本机路径，**不入版本库**（仓库 `.gitignore` 已忽略）。

## 配置项

| 字段 | 默认 | 说明 |
|---|---|---|
| `providerId` | `llmmcp` | 注册到 `ctx.web` 的 provider id |
| `command` | 空 | 解释器绝对路径；留空则自动解析/安装 |
| `args` | `['-m','llmmcp']`（自动安装时） | 启动参数 |
| `cwd` | 进程 cwd | 子进程工作目录 |
| `env` | `{}` | 追加环境变量；父环境里 `*KEY*/*TOKEN*/*SECRET*/*PASSWORD*/*AUTH*` 形状的变量不传给子进程 |
| `autoInstall` | `true` | 找不到可用命令时是否自动安装 |
| `python` | 空 | 建 venv 用的解释器；空则探测 `py`/`python`/`python3` |
| `llmmcpHome` | `~/.llmmcp` | 本机配置与托管 venv 的位置 |
| `venvDir` | `<llmmcpHome>/venv` | 托管 venv 路径 |
| `installSpec` | GitHub Release wheel | `pip install` 的目标，可换成本地 wheel 或路径 |
| `installTimeoutMs` | `900000` | venv 创建与 pip 安装的预算 |
| `searchTool` / `fetchTool` | `web_search` / `web_fetch` | MCP 工具名 |
| `toolCallTimeoutMs` | `60000` | 单次 `tools/call` 传输层预算 |
| `searchTimeoutS` | `min(55, 预算-5s)` | 传给 `web_search` 的 timeout（Google 首查要渲染浏览器） |
| `fetchTimeoutS` | `min(40, 预算-5s)` | 传给 `web_fetch` 的 timeout |
| `warmup` | `true` | 加载时即拉起子进程 |
| `fetchFormat` | `markdown` | 传给 `web_fetch` 的 format；结果按 `text` 交给 `ctx.web`，避免二次转换 |
| `fetchMaxChars` | `400000` | 传给 `web_fetch` 的 max_chars |
| `protocolVersion` | `2025-06-18` | MCP 握手协议版本 |
| `stderrTailChars` | `4000` | 子进程 stderr 保留长度（写日志前会抹掉 `user:password@`） |

插件不导出 schemastery `Config`：`@deepseek-ai/schemastery` 只能从 harness 依赖图解析到，
而插件是 `link:` 进来的零依赖包，模块顶层 import 它会导致整行插件加载失败。

## 验证

```powershell
cd <repo>\packages\dsh-web-mcp

node scripts/verify.mjs --mock --checks   # 不需要 Python
node scripts/verify.mjs                   # 真实 LLMMcp：检索条数、抓取状态与耗时
node scripts/verify-seam.mjs              # 挂真实 ctx.web 服务
node scripts/verify-runtime.mjs           # 解析顺序（不联网）
node scripts/verify-runtime.mjs --install # 真建 venv + 装 wheel（约 1 分钟）
node scripts/verify-no-command.mjs        # 无命令时不安装
node scripts/check-profile.mjs            # 只读：profile 补丁是否生效
```

## 回滚

删除 `cordis.patch.yml` 里 `web-mcp` 与 `id: web` 两个条目后重启，`ctx.web` 回到
`deepseek-official` / `http`；需要时再删除 junction 与 `~/.llmmcp`。插件本身不写任何持久状态，
唯一的落盘是自动安装后写入的 `~/.llmmcp/config.json`。

## 故障排查

| 现象 | 处理 |
|---|---|
| 首次 `web_search` 10~20 s | 正常：Google 对普通 HTTP 客户端是 JS 门禁，LLMMcp 用 headless 浏览器渲染 |
| `GOOGLE_RENDER_FAILED: headless browser timed out` | `python -m llmmcp diagnose` 看浏览器/代理；检查残留 chrome 进程；必要时调大 `searchTimeoutS` |
| `SEARCH_ALL_FAILED` | 查询太泛；LLMMcp 引擎链的既有行为，不是桥接问题 |
| `provider "llmmcp" is not registered` | 插件没加载：看 DSH 日志 `web-mcp` 行，确认 junction 可解析 |
| `registered but unavailable` | 没有可用解释器：看日志里的 bootstrap 结果，或显式配置 `command` |
| bootstrap 失败 | 日志给出 code 与 hint（`PYTHON_NOT_FOUND` / `VENV_FAILED` / `INSTALL_FAILED` / `INSTALL_BROKEN`）；代理环境需 `HTTPS_PROXY` |

**stdout 纪律**：插件从不写 stdout（属于宿主），子进程 stdout 只走 JSON-RPC；日志走 `ctx.logger`。

## 已知边界

- 非 2xx 由 LLMMcp 当结果返回（状态码 + 正文），桥接照常返回 `statusCode`。
- 单连接串行：一个子进程、一个 `tools/call` 队列。
- 子进程回收：`close()` 先关 stdin，250 ms 内未退出则 `taskkill /T /F`，并用 `process.once('exit')` 兜底。
