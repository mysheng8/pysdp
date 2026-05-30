---
type: finding
topic: path-handling-inconsistency
status: investigated
related_paths:
  - webui/routes/files.py
  - webui/routes/proxy.py
  - config.py
  - config.ini
summary: >
  SdpDir/AnalysisDir 在 files.py 和 proxy.py 中存在多处不一致的路径解析逻辑，
  get_settings 路由的 is_absolute 判断是唯一"规范"实现，但 ingest_sdp 和 proxy
  的 capture/analysis 路由各自有不完整的复制版本。proxy.py 的 outputDir 注入逻辑
  与 ingest_sdp 的 copy 逻辑存在双重保障冗余，并可能在某些路径组合下产生矛盾行为。
last_updated: "2026-05-26"
---

# Finding: SdpDir / AnalysisDir 路径处理逻辑混乱

## Problem Statement

pysdp 有三处独立读取并拼接 `ProjectDir` + `SdpDir`/`AnalysisDir` 的逻辑，
但三处实现不完全一致，且存在历史遗留的重复防御机制。

## Evidence

### 证据 1：路径解析逻辑出现在三处，但行为不同

**files.py `get_settings` route（第 888–912 行）**

```python
sdp_dir = cfg.get("SdpDir", "sdp")
analysis_dir = cfg.get("AnalysisDir", "analysis")
if project:
    if not Path(sdp_dir).is_absolute():
        sdp_dir = str(Path(project) / sdp_dir)
    if not Path(analysis_dir).is_absolute():
        analysis_dir = str(Path(project) / analysis_dir)
```

这是最完整的版本：先取 raw 值，再 `is_absolute()` 判断，若为相对路径才拼接 `project`。
结果会被规范化为绝对路径后写入 `config.ini`（通过 `save_settings` route）。

**files.py `ingest_sdp` route（第 317–331 行）**

```python
project = cfg.get("ProjectDir", "")
sdp_sub = cfg.get("SdpDir", "sdp")
if project:
    sdp_dir = Path(sdp_sub) if Path(sdp_sub).is_absolute() else Path(project) / sdp_sub
```

逻辑等价，但没有 `analysis_dir` 分支（只关心 sdp 目录）。
问题：只在 `project` 非空时才执行 copy；若 `ProjectDir` 未设置，则跳过整个 copy 逻辑，
文件停留在 SDPCLI 写出的原始位置，后续 scan_dir 可能匹配不到。

**proxy.py `capture` route（第 106–116 行）**

```python
project = cfg.get("ProjectDir", "")
sdp_sub = cfg.get("SdpDir", "sdp")
if project:
    sdp_dir = str(_Path(project) / sdp_sub) if not _Path(sdp_sub).is_absolute() else sdp_sub
    body["outputDir"] = sdp_dir.replace("/", "\\")
```

**proxy.py `analysis` route（第 122–131 行）**

```python
project = cfg.get("ProjectDir", "")
analysis_sub = cfg.get("AnalysisDir", "analysis")
if project:
    analysis_dir = str(_Path(project) / analysis_sub) if not _Path(analysis_sub).is_absolute() else analysis_sub
    body["outputDir"] = analysis_dir.replace("/", "\\")
```

两处逻辑与 `get_settings` 等价，但存在以下差异：
- `capture` 路由：只在 `not body.get("outputDir")` 时才注入，即客户端若已提供则跳过。
- `analysis` 路由：同上，但没有打印日志。
- 两处都做 `replace("/", "\\")` 转 Windows 路径，而 `ingest_sdp` / `get_settings` 均用 `replace("\\", "/")` 转正斜杠。

### 证据 2：UI Settings 保存时写入绝对路径，造成"二次拼接"风险

`save_settings` route（第 915–963 行）直接把 `sdpDir` / `analysisDir` 字符串原样写回 `config.ini`。
`get_settings` 路由把绝对路径计算好后返回给前端，前端再原样 POST 回来。

因此 `config.ini` 里最终存的是：
```
SdpDir=D:/pysdp/project/sdp
AnalysisDir=D:/pysdp/project/analysis
```

此时 `Path(sdp_sub).is_absolute()` 为 True，所以不会再拼 `project`。
但如果 `config.ini` 里恰好保留的是注释掉的原始相对路径 `# SdpDir=sdp`，
而环境变量 `PYSDP_SDP_DIR=sdp`（相对）同时设置，
`get_settings()` 返回的就是未拼 project 的相对路径 `"sdp"`，
`ingest_sdp` 里的 `Path("sdp") / p.name` 会落在 CWD 下，而非 project 下。

### 证据 3：proxy.py outputDir 注入 vs ingest_sdp copy —— 双重防御且存在分歧

SDPCLI 把 `.sdp` 文件写到自己的工作目录。有两条补救路径：

**路径 A（proxy.py capture 注入 outputDir）**
- 在 capture 请求发出前，把 `outputDir` 注入为 pysdp 的 sdp 目录。
- SDPCLI 直接写到正确位置，无需事后 copy。
- 依赖于 SDPCLI Server 的 `outputDir` 参数被正确识别和遵守。

**路径 B（files.py ingest_sdp 做 copy）**
- capture 完成后，前端调用 `POST /api/files/sdp/ingest`，
  传入 SDPCLI 返回的原始路径。
- `ingest_sdp` 检测文件是否在 sdp_dir 之外，若是则 `shutil.copy2` 到正确位置。
- 依赖于前端传入的 path 是 SDPCLI 的原始路径（而非已经 outputDir 注入后的路径）。

**分歧点**：
- 若路径 A 成功，SDPCLI 已写到 `sdp_dir`，前端拿到的路径就是 `sdp_dir/xxx.sdp`。
  此时 `ingest_sdp` 里 `p.resolve() != dest.resolve()` 为 False，skip copy，正确。
- 若路径 A 失败（SDPCLI 不支持 outputDir 或参数名不匹配），SDPCLI 写到自己目录，
  前端拿到旧路径，`ingest_sdp` 做 copy，也能修复。
- **潜在问题**：如果 SDPCLI 的 `outputDir` 使用反斜杠（proxy.py 做了 `replace("/", "\\")`），
  但 ingest_sdp 比较的是 `p.resolve()` vs `dest.resolve()`，Windows 上路径大小写不敏感，
  两者 resolve 后相等，不会重复 copy，是安全的。
- **真正的问题**：若 `ProjectDir` 在某环境未配置（`project == ""`），
  proxy.py 的注入逻辑整个跳过（`if project:` guard），SDPCLI 写到自己目录；
  同时 `ingest_sdp` 的 copy 逻辑也跳过（同样的 `if project:` guard）。
  文件会停留在 SDPCLI 目录，`ingest_sdp` 仍然对其原始路径做 ingest，
  但 `scan_dir` 被设成 `p.parent`（即 SDPCLI 目录），
  导致 `sdp_files` 表里的 `scan_dir` 散乱，无法通过统一的 sdp_dir 过滤出来。

### 证据 4：`ingest_sdp` 里 `scan_dir` 设成 `parent_dir` 而非 sdp_dir

`_ingest_one`（第 448–473 行）：

```python
parent_dir = str(p.parent).replace("\\", "/")
# ...
database.conn().execute("INSERT OR REPLACE INTO sdp_files ... scan_dir ...", [
    ..., parent_dir, ...
])
```

`scan_dir` 存的是文件实际所在目录，不是配置的 sdp_dir。
这在 `list_sdp` 的过滤逻辑里造成不一致：
- 通过 `rescan_sdp` 入库的文件，`scan_dir` = 请求时传入的 `dir`（可能是 sdp_dir）。
- 通过 `ingest_sdp`（单文件入库）入库的文件，`scan_dir` = 文件实际父目录。
- 若 copy 成功，两者一致（都在 sdp_dir）。
- 若 copy 失败/跳过（`project` 未配置），`scan_dir` = SDPCLI 原始目录，与 sdp_dir 不匹配，
  `list_sdp?dir=<sdp_dir>` 过滤查不到该文件。

### 证据 5：config singleton 重复导入，缺少单一工厂函数

以下三处都有相同的导入 + 取值模板：

```python
from config import get_settings as _get_cfg
cfg = _get_cfg()
project = cfg.get("ProjectDir", "")
sdp_sub  = cfg.get("SdpDir", "sdp")
```

proxy.py 里 `capture` 和 `analysis` 路由各自重复一遍，
files.py 里 `ingest_sdp` 和 `get_settings` 又各自重复一遍。
没有任何公共的 `resolve_sdp_dir()` / `resolve_analysis_dir()` 辅助函数。

## Analysis

路径处理混乱的根本原因有两个：

1. **"规范实现"没有被封装**：`get_settings` 路由里的 `is_absolute()` + fallback 拼接逻辑
   是正确的，但它是一个 HTTP 路由处理函数，不是可复用的工具函数，
   导致其他需要同样逻辑的地方只能复制粘贴（且每次都有细微差异）。

2. **双重防御机制的演化顺序**：proxy.py 的 outputDir 注入是先加的，
   用于在源头解决 SDPCLI 存储位置问题；
   后来 ingest_sdp 的 copy 是后加的，用于在 SDPCLI 不支持 outputDir 时的补救。
   两处都有 `if project:` guard，导致在 `ProjectDir` 未配置时两处同时失效，
   且没有任何警告日志提示用户。

## Impact

1. **数据一致性风险**：`ProjectDir` 未配置时，SDP 文件分散在 SDPCLI 目录，
   `scan_dir` 字段混乱，`list_sdp` 无法统一查询。

2. **维护负担**：四处重复的路径解析代码，任一处行为变更都需要同步修改其他三处，
   极易产生漂移。

3. **路径分隔符不一致**：proxy.py 输出反斜杠（`replace("/", "\\")`），
   其余地方输出正斜杠（`replace("\\", "/")`）。
   在 Windows 上 Python 通常容忍两者，但 DB 存储的路径若混用分隔符，
   字符串相等比较（非 resolve）会失败（如 `list_sdp` 的 `scan_dir = ?` 过滤）。

4. **copy 逻辑缺少失败时的 fallback**：`ingest_sdp` 里 copy 失败只打 warning，
   继续用原始路径 ingest，但 `scan_dir` 会指向 SDPCLI 目录，
   此后 rescan sdp_dir 时该文件不会被清理也不会重新入库。

## Proposed Fix Direction

### 方向一：封装 `resolve_sdp_dir()` / `resolve_analysis_dir()` 工具函数

在 `config.py` 中添加：

```python
def resolve_dir(key: str, default: str) -> Path | None:
    """Resolve a configured directory key to an absolute Path.

    If the config value is already absolute, return it directly.
    Otherwise join with ProjectDir. Returns None if ProjectDir is not set
    and the value is still relative.
    """
    cfg = get_settings()
    project = cfg.get("ProjectDir", "")
    val = cfg.get(key, default)
    p = Path(val)
    if p.is_absolute():
        return p
    if project:
        return Path(project) / p
    return None
```

所有调用方改为：

```python
from config import resolve_dir
sdp_dir = resolve_dir("SdpDir", "sdp")
analysis_dir = resolve_dir("AnalysisDir", "analysis")
```

### 方向二：明确 proxy outputDir 注入的必要性

现在 ingest_sdp 已经做 copy，proxy.py 的 outputDir 注入变成了"尽力而为"的优化。
建议保留，但：
- 在 `if not project:` 时打一条 WARNING 而不是默默跳过。
- 统一路径分隔符为正斜杠（让 SDPCLI 自己处理平台差异，或在 proxy 里按 SDPCLI 需求转换）。

### 方向三：修复 `_ingest_one` 的 `scan_dir` 语义

`_ingest_one` 应当把 `scan_dir` 设为 **文件实际所在目录的规范化路径**，
且 copy 成功后要用 `dest.parent`，而不是原始 `p.parent`。
当前代码在 copy 成功后正确更新了 `p = dest`（第 329 行），
所以 `parent_dir` 在 copy 成功时已经是正确的——问题在于 copy 失败/跳过时，
`scan_dir` 存的是 SDPCLI 目录，与 `sdp_dir` 不一致。

建议：copy 失败时打 ERROR（而非 WARNING），或者用 `sdp_dir`（配置值）作为 `scan_dir`
而不是 `p.parent`，让查询逻辑更稳定。

## Related Context

- Related findings: (无现有 finding)
- Related plans: (无现有 plan)
- Related implementations: (无现有 implementation record)
