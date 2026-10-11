# AI 小工具 · Agent 工作手册

> **这是唯一入口。** 先把这份读完，细节再看 `docs/`（索引在 §7）。
> 最后更新：2026-10-06

---

## 0. 这是什么

一个**本地网页工具**，用来批量注册 ChatGPT / Codex 账号，主要给「想自己搭号池」的人用。

- 后端 `app.py`（FastAPI），前端 `ui/index.html`（单文件），跑在 `http://127.0.0.1:8770`，
  **用系统浏览器当界面**（不是 Electron，也不是桌面窗口）
- 注册出来的账号带**密码 + TOTP 2FA**，可导出成一行一个的交付文本：
  `邮箱----密码----2FA密钥`（缺的字段写「无」）
- 邮箱和接码号码都从**本站号池**买，用户只需要填**一把「工具密钥」**
  （站内「自建号池」页创建）—— 不需要自己注册上游账号

**它不是什么**：不是服务端程序，没有数据库，没有用户系统。所有状态都在 `data/` 目录里。

---

## 1. 60 秒上手

```powershell
$t = "D:\xianyu-codex-commerce-suite\external\gpt-register-lite"

# ★ 先跑这个：一条命令核实「仓库状态」和这份文档说的是否一致
powershell -ExecutionPolicy Bypass -File 检查全部.ps1
#   -> 16 项（14 个回归 + 2 个审计）。全套不花钱、不联网取码。
#   -> 会用 8770 上已有的实例；没有就临时起一个，跑完关掉。

# 跑起来（开发模式；改 core/ 或 app.py 后必须重启，改 ui/index.html 只需刷新页面）
$env:GRL_NO_BROWSER='1'
Start-Process "$t\.venv\Scripts\python.exe" -ArgumentList 'app.py' -WorkingDirectory $t -WindowStyle Hidden
# 探活： GET http://127.0.0.1:8770/health
# 端口被占用会自动往后找（app.py 的 _pick_free_port）

# 单独跑某一个
python tools\test_preflight.py        # 开跑前自检的判定逻辑
python tools\test_account_api.py      # 接口级 2FA/密码链（72 项断言）
python tools\audit_accounts.py        # 账号数据体检

# 打包（三种方式，取舍见 §6）
powershell -ExecutionPolicy Bypass -File 打包绿色版.ps1 -WithBrowser
```

**环境变量**（都可用同名覆盖）：`GRL_HOST` / `GRL_PORT`（默认 8770）/ `GRL_NO_BROWSER=1`（不自动开浏览器）

---

## 2. 目录地图

### 源码（这些才是你要读/改的）

| 路径 | 说明 |
|---|---|
| `app.py` | FastAPI 全部路由（~35KB，一屏能扫完的规模） |
| `ui/index.html` | **整个前端就是一个文件**（72,797 字节 / 1612 行，HTML+CSS+JS 全内联，没有构建步骤） |
| `core/` | 26 个模块，见下表 |
| `requirements.txt` / `requirements.lock.txt` | 直接依赖（`>=`）/ **钉死的完整闭包**（交付用这个） |
| `build.spec` | PyInstaller 配置（onedir） |
| `检查全部.ps1` | **一条命令跑完全套检查**（14 个回归 + 2 个审计）—— 接手第一天先跑它 |
| `打包发布.ps1` `打包绿色版.ps1` `打包源码.ps1` | 三种交付方式的打包脚本 |
| `prepare.ps1` + `一键启动.bat` | **客户侧**首次运行的环境准备（源码版/绿色版共用） |
| `README.md` | 面向**使用者**的说明（不是给 agent 的） |

### `core/` 里值得先看的几个

| 模块 | 为什么重要 |
|---|---|
| `engine.py` | 任务编排、线程池、日志广播。想知道"点开始注册之后发生什么"从这里进 |
| `registration.py` | 注册主流程（41KB，步骤编号在注释里，如「步骤 6.5」） |
| `browser_flow.py` | 页面动作（**147KB，最大**）。`_set_element_value` / `_set_otp_value_via_js` 这些是绕过 React 受控输入的关键 |
| `oauth_flow.py` | 激活流程（116KB）。手机验证的换号循环 + 止损都在这里 |
| `security_setup.py` | 设密码 / 绑 2FA（TOTP 是自己实现的 RFC 6238，没引 pyotp） |
| `account_api.py` | **接口级**实现（浏览器内 fetch），比 DOM 自动化稳得多。2FA 那套走这里 |
| `sitepool.py` | 本站号池客户端（邮箱单 + 号码单共用一把密钥） |
| `preflight.py` | 开跑前自检：密钥 / 余额 / 代理 / 浏览器 |
| `credential_export.py` | 交付文本格式 + **可交付判定**（唯一出处） |
| `paths.py` | 路径解析 + 浏览器内核目录（`apply_runtime_env`） |

### 产物 / 数据（**别读、别改、别提交**）

| 路径 | 体积 | 说明 |
|---|---|---|
| `data/` | 10 MB | **真实账号库 + 真密钥**（`config.json` / `accounts.jsonl`） |
| `logs/` | 1 MB | 运行日志 |
| `.venv/` | 213 MB | 开发虚拟环境 |
| `browser/` | 597 MB | 浏览器内核缓存（做离线包时拷进来） |
| `dist/` `dist-green/` `dist-src/` `build/` `release/` `.cache/` | ~1.1 GB | 打包产物 |
| `attic/` | — | 2026-10-06 从 `core/`+`tools/` 挪出来的 19 个 `.bak-*` 杂物 |
| `artifacts/` | — | 实测留下的 jsonl |

全部已在 `.gitignore` 里。**注意 `dist/` 匹配不到 `dist-green/`，所以它们各列了一行。**

---

## 3. ★ 硬规则

1. **`core/oauth_flow.py` 和 `core/browser_flow.py` 现在是手工维护的唯一源。**
   两个生成器已退休（`tools/extract_*.py.retired`）。
   **读它们当参考可以，但绝对不要跑** —— 见 §4 第一条。

2. **`data/` 里有真密钥和真账号。** 任何交付、打包、提交之前，
   必须过 `tools/audit_secrets.py`（退出码 0 才算过）。打包脚本里已经做成硬门禁。

3. **改完 `core/**` 或 `app.py` 必须重启进程。** `ui/index.html` 是每次请求现读的，
   改 UI 只要刷新页面。**别把「改了」当成「生效了」** —— 这个坑踩过（见 §4）。

4. **`requirements.lock.txt` 钉的是「我们验证过的版本」，不是「最新版」。**
   `cloakbrowser` 被手工钉回 `0.5.11` 就是这个原因。要升级 = 一次有意识的、可回归的变更。

5. **`.bat` 内容必须纯 ASCII。** cmd.exe 用当前代码页解析 bat，中文会变乱码。
   中文提示一律交给 Python / PowerShell 输出。文件名可以是中文。

6. **别用 PowerShell 数 `browser_flow.py` 的行数** —— 它是纯 LF 结尾，
   `Get-Content | Measure-Object` 会少数 200 多行。用 read 工具或 Python 数。

---

## 4. ★★ 雷区（每一条都真的咬过人）

### 4.1 两个「重新提炼」生成器是地雷（已退休）

`core/oauth_flow.py` 和 `core/browser_flow.py` 的头部原本写着
「请勿手工编辑，改动请改 `tools/extract_*.py` 后重新生成」——**这句话已经作废**。
两个 core 文件后来被手工改得比生成器新得多，**跑一次会静默抹掉一大批修复**：

| 脚本（已改名 `.retired`） | 目标 | 实测跑一次的后果 |
|---|---|---|
| `extract_codex_oauth.py.retired` | `oauth_flow.py` | 2275 行 → **1373 行**（`-902`），丢 `_TOTP_REGISTRY`、`check_stop()`、通道收敛循环、`fraud_guard` 止损 |
| `extract_page_flow.py.retired` | `browser_flow.py` | 2861 行 → **2007 行**（`-854`），`_set_otp_value_via_js` 直接消失 |

### 4.2 Win11「智能应用控制」会拦未签名的 exe

实测：PyInstaller 打出来的 exe 在同一台机器上**连续 12 次启动全被拦**
（换路径、等 60 秒都拦），事件日志原文是
`did not meet the Enterprise signing level requirements`。

- **没有白名单、关掉还不可逆**
- 而官方 Python 的嵌入式包里**每个二进制都是 PSF 签名有效的**，
  所以**绿色版（不产出自己的 exe）能过**
- 详细证据与三种解法：`docs/20261006-绿色版方案.md`

### 4.3 浏览器内核不在包里，而且下载源有 GitHub

- 内核 ~597 MB（`chromium` 536MB + `geoip` 61MB），默认**不带**
- 主源 `cloakbrowser.dev`，**回退是 GitHub**（大陆大概率连不上）
- **`geoip` 库也来自 GitHub**（P3TERX 镜像），首次使用才下
- ★ **`copy` 会保留 mtime**，而 cloakbrowser 靠 mtime 判「geoip 库有没有超过 30 天」——
  所以打包脚本要在**产物里**再刷一次时间戳，否则客户第一次启动就去 GitHub 重下

### 4.4 嵌入式 Python 会捡走「用户级 site-packages」

`._pth` 里启用 `import site` 后，`%APPDATA%\Python\Python312\site-packages` 也会进
`sys.path`。后果：客户以前用 `pip install --user` 装过的包会被 pip 认为「已满足」
而**跳过安装**，运行时变成依赖客户那一份 —— 他一删就崩。
实测还让 pip 因假冲突报错退出、白跑一个镜像源。
**解法：`PYTHONNOUSERSITE=1`**（`一键启动.bat` 和 `prepare.ps1` 里都设了）。

### 4.5 「改了但没生效」

2026-10-06 实测：旧进程 10:19 启动，而 `core/browser_flow.py`（11:04）、
`core/oauth_flow.py`（11:13）都晚于启动时间 ⇒ **那批改动一行都没生效**。
证据：日志里是「换号前随机等待 **6.1 秒**」，而改动后的代码不可能产出这个值。

**改完 `core/` 顺手核一遍「进程启动时间 > 文件 mtime」。**

### 4.6 密钥审计器自己的两个坑（已修，但要知道）

1. **别把真实凭据写进审计器当比对表** —— 第一版就这么干了，审计器自己成了泄漏源。
   现在改成运行时从本地 `data/config.json` 读。
2. **`ZlibArchiveReader(exe)` 从偏移 0 找 PYZ 魔数必然失败**，
   会让整个 Python 代码层**扫不到却报「命中很少」**（假阴性）。
   正确做法：先把 PYZ 字节落到临时文件再交给 reader。
   现在脚本会在模块数为 0 时**主动报警「不可当作干净」**。

### 4.7 其他小坑

- 路径带空格时 `Start-Process -ArgumentList` 要**自己加引号**，否则 cmd 只取到第一个空格前
- `.py.bak-*` 这类文件的扩展名不在审计器白名单里 ⇒ 曾是**审计盲区**（已改成按文件名任意位置判断）
- 本机 `Get-Content` 读 UTF-8 日志会显示成乱码，那是**显示问题不是内容问题**

### 4.8 ★★ 别用 `with ThreadPoolExecutor(...)` 包住「带超时」的调用

**2026-10-09 实测踩到。用户症状：点了「开始检查」一直卡在「检查中…」。**

```python
# ✗ 错的：超时形同虚设，HTTP 请求会永久挂住
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    future = pool.submit(work)
    try:
        return future.result(timeout=90)
    except concurrent.futures.TimeoutError:
        return "超时"        # ← 想返回，但 __exit__ 会 shutdown(wait=True)
                             #   死等那个线程 → 请求永不返回
```

**根因**：`ThreadPoolExecutor.__exit__` 调的是 `shutdown(wait=True)`。
只要工作线程真的卡住（实测就是**首次启动浏览器被杀软/Defender 扫描新文件拖慢**），
`future.result(timeout=…)` 抛了 `TimeoutError` 也没用 —— 退出上下文时照样死等。

**正确写法**：自己建池，`finally` 里 `shutdown(wait=False)`。
宁可泄漏一个后台线程，也不能把请求挂死：

```python
pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
try:
    future = pool.submit(work)
    try:
        return future.result(timeout=90)
    except concurrent.futures.TimeoutError:
        return "超时"
finally:
    pool.shutdown(wait=False)
```

**同一处还补了两件事**，缺一个用户还是只能干等：

1. **后端打日志**：`[自检] 开始浏览器启动测试` / `结束：ok=… 耗时 …秒`。
   原来成功时不打日志，导致「卡在启动前」和「启动后卡住」在日志里长得一模一样，
   排查时完全看不出卡在哪。
2. **前端硬超时**：`ui/index.html` 的 `runPreflight` 用 `AbortController` 给了
   150 秒（深检）/ 30 秒（快检）上限，超时提示「再点一次」。
   否则页面永远停在「检查中…」，连重试按钮都是灰的。

**回归测试**：`tools/test_preflight.py` 第 [7] 节用「永不返回的假启动」钉死这个行为
（断言超时后必须在预算内返回）。

---

## 5. 当前状态：什么验证过、什么没有

### ✅ 已验证（有证据）

| 项 | 证据 |
|---|---|
| 14 个自检脚本 | 全 exit 0（`test_preflight` / `test_phone_otp_submit` / `test_account_api` / `test_totp_registry` / `test_totp_js` / `test_otp_typing` / `test_export_format` / `test_sitepool_provider` / `test_socks5_chain` / `verify_ui` / `selftest_core` / `check_ui_js` / `audit_accounts` / `check_sms_delivery`） |
| 密钥审计 | 源码树 + 解包后的 exe 全部字符串常量（1350 模块 / 48,356 条串）→ 真实凭据 0、夹带 0 |
| DLL 可移植性 | PE 导入表扫 70 个二进制 → 无缺失；`vcruntime140*.dll` 包内自带；**无 `msvcp140` 依赖** |
| 绿色版在 SAC 开启下可运行 | 同机 exe 被拦 12 次，绿色版 1 秒起来 |
| 中文+空格路径 | `D:\测试 中文 路径\…` → 1 秒起来，`data_dir` 正确解析 |
| 净化环境（PATH 只剩 system32、清空 PYTHON*） | 1 秒起来 |
| 2FA / 密码接口级链路 | `test_account_api.py` 72 项断言 + 真机跑通（`password` 16 位、`totp_secret` 32 位） |

### ❌ 还没验证（**别当成已成立**）

1. **当前代码上还没有一次完整的真实端到端注册**
   最后一次真跑用的是**旧代码**，而且当时号码源在挂（`activation=failed`）。
   花钱：邮箱 ¥0.04 + 号码（收到码才计费）。
2. **从没在一台干净 Windows 上测过**（本机是 Win11 家庭版，
   **没有 Windows 沙盒 / Hyper-V**，也没装任何虚拟机软件）。
   补测最省事的办法：**租一台按量付费的 Windows 云主机**，一小时几块钱。
3. **Win10 从没跑过**（全程只在 Win11 上）。
4. 完全没装 VC++ 运行库的机器（审计结论是包内自带，但没实测过）。
5. Mac 路径（`python.org` macOS 运行时 + `.command`）—— 思路同绿色版，**没验**。

---

## 6. 交付方式（三种，各有代价）

| 方式 | 体积 | 客户体验 | 代价 |
|---|---|---|---|
| `打包发布.ps1`（PyInstaller exe） | 171 MB | 双击 | **SAC 会拦未签名 exe**，要么买签名证书要么别用 |
| `打包绿色版.ps1`（推荐） | 200~800 MB | 双击 `启动.bat` | 包大；客户看到的是 bat（黑窗） |
| `打包源码.ps1` | **0.23 MB** | 双击，第一次等几分钟装环境 | 装环境可能失败；代码全给客户了 |

- `-WithBrowser` 给绿色版带上内核（+597 MB）→ 客户**零下载**
- 三个脚本都内置**密钥闸门**和**夹带检查**，不过就拒绝出包
- 绿色版和源码版**共用 `prepare.ps1`**（同一套环境准备逻辑，不会漂移）

---

## 7. 文档索引（按需要读，别全读）

| 文档 | 什么时候读 |
|---|---|
| `docs/20261006-维护记录.md` | **想知道"最近改了什么、为什么"** —— 最全的一份 |
| `docs/20261006-绿色版方案.md` | 要碰交付方式 / 遇到 SAC 问题时 |
| `docs/20261006-源码交付方案.md` | 要发源码版时 |
| `docs/20261006-Windows开箱即用-实施记录.md` | 想知道自检、端口、默认值这些设计取舍 |
| `docs/20261006-干净机器验证.md` | 想知道可移植性验证到什么程度 |
| `docs/20261006-发布就绪度.md` | 想评估"能不能发" |
| `docs/20261006-跨平台与开箱即用.md` | 要考虑 Mac 时 |
| `docs/20261006-交接-gsm888复用实测.md` | 历史交接（gsm888 那条线**已确认不接进本工具**，只作背景） |

---

## 8. 如果你是被请来 review 的

几个**我自己不确定、最想被挑战**的点：

1. **`core/browser_flow.py`（147KB）和 `core/oauth_flow.py`（116KB）是从另一个项目
   提炼 + 大量手工修补来的。** 里面有很多「实测发现 A 不行所以改成 B」的注释。
   它们**没有系统性的测试覆盖**，只有零星几个针对特定 bug 的测试。
   **如果你觉得某处逻辑可疑，很可能真的可疑。**
2. **`tools/` 有 88 个脚本**，大部分是**一次性探针**（`probe_*` / `check_*` / `shoot_*`），
   写死过本机路径、依赖当时的现场。**不要试图让它们全绿。**
   真正的回归集是 §5 列的那 14 个。
3. **「手机 OTP 提交后页面没动」这条路上有两次行为变更**（见维护记录 §8），
   现在的策略是「强制同步重试 2 次 → 抛错换号」。
   我担心的是：**如果原因是系统性的，会连换 12 个号**（`sms_max_retries=12`）。
   建议加连续止损，但**没做**（涉及花钱的行为变更，留给 owner 决定）。
4. **`data/accounts.jsonl` 里有一条脏密码**（某个 outlook 账号，14 位、含非字母数字），
   不可能由 `generate_password()` 产出，来源不明。
   现在被交付过滤挡住了，但**根因没查**。
   ⚠️ 这份文档**随包发给客户**，所以这里不写具体邮箱 / 密码 / 2FA 密钥。
5. **默认值 `harden_after_register=True` 是这轮才翻的**，
   老用户 `config.json` 里存过的 `false` 不会被改 —— 这是有意的，但值得确认是否符合预期。

---

## 9. 历史遗留

- `attic/` —— 19 个 `.bak-*`（2026-10-04 一次 toolkey 重构留下的备份）。
  移出 `core/` 和 `tools/` 是因为它们是**审计盲区**且会混进交付包。确认没用可以删。
- `tools/extract_*.py.retired` —— **故意保留**，作为"上游长什么样"的参考。
- `run.bat` —— 旧的启动脚本（要求客户自己装 Python）。已被
  `一键启动.bat` + `prepare.ps1` 取代，**保留但不再是发布路径**。
- 本工具目录在父仓库 `D:\xianyu-codex-commerce-suite` 里是 **`??` 未跟踪**（没有版本控制）。
  纳入版本控制**是安全的**（`.gitignore` 已覆盖 data/logs/browser/dist*/release/.cache），
  但**需要 owner 点头**。
