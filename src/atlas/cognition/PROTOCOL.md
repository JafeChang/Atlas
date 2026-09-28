# T-003 认知层：契约值、IPC 协议与失败语义

本文件是 `atlas.cognition` 的**实现说明**（不是第二份规格）。规格以 `SPEC.md` 为准：
§2.14（认知层裁决）、§2.2（PI 只输出 quote）、§2.3（四条不变量）、§3（任务规范）、
§4.7（硬门槛）、§5 登记 #4。

---

## 1. 隔离是怎么成立的（§4.7）

§4.7 的门槛成因是「agent **自带工具** + 处理不可信外部内容 ⇒ prompt injection 升级为
任意代码执行」。本方案的裁决（§2.14 决策一）是让前提不成立：

* `@earendil-works/pi-ai` 是**统一 LLM 调用库**；工具必须由调用方用 TypeBox 声明并自行处理。
* 边车 `run.mjs` 声明 **0 个工具**（`const tools = []`），并且**没有导入任何执行能力面**。
* 因此模型即便被注入文本说服，也**没有可调用的东西**。

**可观测的判据（不是"我们相信"）**

| 判据 | 观测点 |
|---|---|
| 零工具 | `inspect` 自述 `toolsRegistered == 0`；**真实请求体**里 `tools` 缺失或 `[]` |
| 能力面 | `inspect` 的 `static_imports` 恰好 5 项（见下），且源码正则不含任何执行/网络导入 |
| 无执行 | 注入测试：哨兵目录清单（路径→sha256）在调用前后**逐字节相同**；目标文件未被创建；注入里的 `rm -rf` 字面目标仍在 |
| 无子进程 | 边车进程期间父进程的子进程数不增长（有活对照：故意起一个 `sleep 0` 确认计数确实会变） |
| 最小环境 | `inspect` 的 `env.names` 只含白名单键；`leaked_secret_names` 恒为空 |

**边车的实际能力面**（`inspect.capabilities.static_imports`）：

```
./json-extract.mjs
@earendil-works/pi-ai
@earendil-works/pi-ai/api/openai-completions.lazy
node:crypto     # 只用于计算自身源码 sha256（可审计标识）
node:fs         # 只用于 readFileSync 读自身源码；唯一一处 fs 用法，有测试钉死
```

`process.getBuiltinModule("child_process")` 之类的调用是**只读探测**（报告"这个内建模块
是否存在"），不引入能力面；`inspect` 同时给出 `builtin_available` 作为**活对照**，
证明"没导入"不是"不存在"造成的假象。

> ⚠️ 一旦需要给模型工具，门槛**立即复活**，必须上真隔离（容器/微VM）。
> 当前裁决只是不为尚未存在的需求预付基础设施成本。

---

## 2. IPC 协议（版本化）

```
协议版本 : atlas.cognition.sidecar/1   （Python: PROTOCOL；Node: PROTOCOL）
帧前缀   : #atlas-cognition/1#        （Python: FRAME_PREFIX；Node: FRAME_PREFIX）
```

**父 → 子**：`stdin` 上一行 JSON job（一次性进程，读完即处理；无长连接状态）。

```jsonc
{
  "protocol": "atlas.cognition.sidecar/1",
  "jobId": "cog-…",
  "operation": "call" | "inspect" | "parse",
  "noise": false,                 // 仅测试钩子：在数据通道上先注入噪声行
  "call": {                       // operation == "call"
    "provider": "deepseek",
    "model": "deepseek-flash",
    "baseUrl": "https://api.deepseek.com",
    "apiKey": "…",                // 密钥只在这里出现，绝不进环境变量
    "timeoutMs": 60000,
    "maxOutputTokens": 4096,
    "temperature": 0.0,
    "reasoning": false,
    "noProxy": "127.0.0.1,localhost,::1",
    "proxyUrl": "",               // 需要时由父进程传入
    "systemPrompt": "…",
    "userContent": "…"            // 不可信外部内容只作为**数据**出现
  },
  "text": "…"                     // operation == "parse"
}
```

**子 → 父**：`stdout` 上的帧行；**只有以帧前缀开头的行是数据**，其余一律忽略
（这条规则同时是对"库往 stdout 打警告"的防御，有噪声注入测试钉死）。

```jsonc
{ "protocol": "atlas.cognition.sidecar/1", "jobId": "…",
  "sidecarSha256": "sha256:…",   // 边车源码摘要 → 记录为 sidecar_code_version
  "node": "v22.21.1", "toolsRegistered": 0,
  "result": { … } }              // 或 { "error": { "code": "…", "message": "…" } }
```

**退出码**：`0` = 全部 job 产出了结果帧（含已处理的降级）；`2` = 有 job 被判为协议/内部错误（响亮）；`1` = 崩溃。

**stderr** 只放诊断，永不承载协议。

### 为什么数据通道是 stdout 而不是 fd 3

**实测**：在本机 WSL（Ubuntu-24.04 / drvfs）下，父进程用 `os.pipe()` + `pass_fds` 传给
Node 的 fd 3 **不可写**——`writeSync(3, …)` 与 `createWriteStream(null, {fd: 3})` 都返回
`EINVAL: invalid argument, write`，而同一进程写 stdout/stderr 完全正常。
因此改用"前缀指纹 + stdout"方案：数据与噪声共用 stdout，**只有带指纹的行是数据**。
（该结论实测记录于本任务报告；`node:fs` 的 `writeSync` 在 WSL 管道上的这一行为已确认。）

---

## 3. 配置与凭据（provider/model 是配置，不是代码）

| 项 | 路由 A（**默认**） | 路由 B（回退） |
|---|---|---|
| `route_name` | `deepseek` | `openai-compatible` |
| provider | `deepseek` | `atlas-openai-compatible` |
| base_url | `https://api.deepseek.com` | `ATLAS_LLM_BASE_URL`（实测 `https://openrouter.ai/api/v1`） |
| model | `deepseek-flash` | `ATLAS_LLM_MODEL` |
| 凭据变量 | `DEEPSEEK_API_KEY` | `ATLAS_OPENAI_API_KEY` |

选择方式：参数 `route=` > 环境变量 `ATLAS_COGNITION_ROUTE` > 默认（`deepseek`）。
**切路由不需要改任何代码路径。**

* 凭据**只从环境变量 / `.env.local` 读**，绝不写进代码、提交、日志或测试夹具；
* **绝不**读 harness 的 `~/.dsh/.credentials.yaml`（那是 harness 的凭据，不是项目凭据）；
* `CognitionConfig.api_key` 标了 `repr=False`，配置对象被打进日志也不会泄露；
* 记录里只有 `credential_route`（`deepseek@api.deepseek.com#<sha256 前 16 位>`）——
  可辨识"走的是哪条路 / 哪把钥匙"，不可反推密钥；
* 密钥**不参与** `config_digest`，但参与幂等键之外的 `credential_route`。

### 代理

Node 的全局 `fetch` 只有在 **Node 启动前**设置 `NODE_USE_ENV_PROXY=1` 时才会挂上
`EnvHttpProxyAgent`。因此父进程在 `sidecar_env()` 里始终带上该开关，并在需要时注入
`HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY`。`NO_PROXY` 默认含回环地址，
保证本地 mock 服务器不会被送到代理。代理 URL 里可能含口令，因此同样 `repr=False`。

---

## 4. 失败语义（**降级** vs **响亮失败**）

### 降级为"未分类"（`status == "unclassified"`，必带原因码）

判据是「**PI 没拿到可用的模型输出**」。降级结果 `claims` 恒为空、`output` 恒为 `None`
（构造函数强制），保持 §2.3 "Proposed 可覆写"语义：下次模型可用时可重跑覆盖。

| 原因码 | 触发 |
|---|---|
| `unreachable_model` | 端点不可达 / 连接被拒 / DNS 失败 / 网络传输错误 |
| `timeout` | 模型侧超时；边车看门狗超时（已 kill 子进程） |
| `http_error` | 任意非 2xx（含 402 余额、429 限流、5xx） |
| `model_deprecated` | HTTP 404 且响应体明确说模型已下架（§2.14 决策三第 1 类） |
| `empty_completion` | 2xx 但没有任何可用文本 |
| `unparseable_output` | 有文本，但抽不出"恰好一个完整 JSON 值" |
| `sidecar_error` | 边车报错但没有更具体的原因（**原始码保留在 `detail`**） |

### 响亮失败（抛异常，**绝不**降级）

判据是「**这是必须修的 bug / 接线错误**」。

| 异常 | 触发 |
|---|---|
| `ModelEnvelopeError` | 解析出 JSON 但结构违反冻结契约（缺字段 / 多余字段 / 类型 / 越界 / schema_version 不符 / 顶层非对象） |
| `IsolationViolationError` | 边车报告注册了**非零**工具 ⇒ 隔离前提不成立 |
| `SidecarUnavailableError` | node 不可用 / 版本不足 / 依赖未安装 / 进程启动失败 |
| `ProtocolError` | 协议版本不符 / 无帧 / 多帧 / 帧不可解析 / 未知 status / 未实现操作 |
| `ConfigError` | 缺 API key / 配置非法 / 边车报 `config_error` |

**覆盖度自检**：`tests/test_cognition_degrade.py::test_every_degrade_reason_is_covered_or_documented`
断言 `DegradeReason` 的每个取值都有测试覆盖——新增原因码而没人测就会失败。

---

## 5. 输出契约（冻结）

模型必须在一次调用里返回**恰好一个** JSON 对象：

```json
{"schema_version": "cognition-output/1", "claims": [
  {"kind": "industry", "value": "ai", "quote": "…", "confidence": 0.9}
]}
```

* `ExtractedClaim` 继承 `atlas.contracts.ContractModel`（`frozen=True`、`extra="forbid"`）；
* **没有任何坐标字段**——`char_start` / `char_end` / `block_id` 在类型层就构造不出来
  （SPEC §2.2：PI 只输出 quote，坐标由 T-107 的确定性匹配产生）；
* 多余字段、缺失字段、越界 `confidence`、`schema_version` 不符 ⇒ `ModelEnvelopeError`。

### 解析策略（确定性）

1. 直接对整段文本做"恰好一个 JSON 值"扫描；
2. 失败则剥掉**恰好包住整段**的一层 markdown 围栏后再扫；
3. 再失败则剥掉**纯说明行**（被丢掉的行不得含任何结构字符 ` ``` ` `{` `}` `[` `]`）后再扫；
4. 全部失败 ⇒ `ModelOutputError` ⇒ 适配器降级为 `unparseable_output`。

**不用"正则抓第一个 `{`"**：两段输出 / 前后有说明文字时，贪心正则拼出非法 JSON、
非贪心正则**静默**返回第一段——两者都会给出"看似合法但错误"的结果。
本解析器的硬约束是：值之后只允许空白，且顶层必须是对象/数组；否则拒绝。
`tests/test_cognition_parse.py` 里有一条测试专门把这两种正则的错误行为写出来作对照，
并断言**两套实现**（Python 与边车）在同一语料上结论一致。

---

## 6. 版本与幂等

* 调用记录携带 `(code_version, config_version, model_version)`（SPEC §3），
  其中 `code_version` = prompt 版本（本包唯一"改一个字就改行为"的部分）；
* 记录还携带 `sidecar_code_version`（边车源码 sha256，可审计）；
* **幂等键** = `sha256(规范化 JSON{input_digest, config_digest, 三元组})`；
* 开启缓存时，同键第二次调用**不再请求模型**并返回同一记录（幂等的可操作定义）。

---

## 7. 成本与延迟实测（T-105 的预算输入）

数字来自 `tools/t003_real_call.py` 与 `tools/t003_timing.py`（2026-09-26，WSL Ubuntu-24.04）。

### 进程启动成本（**每次调用都要付**）

| 项 | 时间 |
|---|---|
| `node` 启动本身 | ~13 ms |
| `import '@earendil-works/pi-ai'` —— 在 `/mnt/c`（drvfs）上 | **~5900–6400 ms** |
| 同上，边车与 `node_modules` 放在**原生 Linux 文件系统** | **87 ms** |
| `import 'openai'`（barrel 的传递依赖之一） | ~1000 ms |

⇒ 成本几乎全部是 **drvfs 上逐文件读取 `node_modules`**，不是 pi-ai 本身慢。
缓解手段（**都不需要改协议**）：① 把 `node_modules` 放原生文件系统并用符号链接接进来；
② 若仍不够，再把边车改成常驻进程 + 多 job（协议本就是"stdin 一行一个 job"）。

### 模型侧（`deepseek-flash`，官方端点）

| 观测 | 值 |
|---|---|
| 端到端墙钟（含进程启动） | 6.7 s ～ 12.5 s |
| 边车内耗时（不含进程启动） | 1.9 s ～ 8.2 s |
| input tokens | 236 ～ 428 |
| output tokens | 129 ～ 1181 |
| **reasoning tokens** | **0 ～ 1024**（占 output 的 0%～87%） |
| cache_read tokens | 0 或 128（同一 prompt 前缀命中） |
| 成本 | **provider 不报告**：DeepSeek 的 `/models` 与响应体都不含定价，pi-ai 的 `cost.total` 为 0 ⇒ 记录为 `null`（"未报告"，不是 0） |

**对 T-105 的结论**：`deepseek-flash` 即使 `reasoning=false` 也会产生大量 reasoning token，
且数量在同一 prompt 上从 0 波动到 1024（8.2 s vs 1.9 s）。批量分类必须
① 把推理预算算进成本模型，② 对延迟做超时预算，③ 优先复用 prompt 前缀以吃到 cache_read。

