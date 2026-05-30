# pySdp

## Snapdragon Profiler 是什么？

[Snapdragon Profiler](https://developer.qualcomm.com/software/snapdragon-profiler) 是高通官方的 GPU 性能分析工具，适用于搭载 Adreno GPU 的设备（手机、XR 头显、PC）。它可以进行帧级截帧，采集每个 Draw Call 的参数、GPU 硬件计数器（clocks、bandwidth、shader stall 等）、Shader 源码、纹理和 Mesh 资源。

输出格式为 `.sdp` 文件（ZIP 包含截帧元数据 + 二进制资源）。

## pySdp 是什么？

pySdp 是基于 Snapdragon Profiler 截帧数据的**自动化分析平台**。它解决三个核心问题：

- **官方工具只能查看，不能分析** — Snapdragon Profiler 展示原始数据，但不告诉你瓶颈在哪里、哪些 DC 有问题
- **手动分析效率极低** — 一帧可能有数百到数千个 Draw Call，人工逐个检查不现实
- **需要深厚的 GPU 架构知识** — 理解 Adreno 的 metrics 含义需要专业背景

pySdp 通过规则引擎 + LLM 自动完成：Draw Call 分类（UI / 场景 / 阴影 / 后处理等）、性能瓶颈归因（shader bound / bandwidth bound / geometry bound）、Top-N 问题 DC 识别、AI 优化建议报告生成。

整个流程：**截帧 → C# 提取 → Python 分析 → WebUI 可视化 + AI Chat 交互式查询**。

---

## 架构概览

```
Browser (localhost:8000)
  └── WebUI SPA (index.html + app.js)
        │
        ├── /api/sdpcli/*    ──proxy──►  SDPCLI Server (localhost:5000)   [仅前端使用]
        ├── /api/snapshot/*  ──►  设备/应用发现 + 截帧工作流
        ├── /api/jobs/*      ──►  C# 提取 + Python 分析触发
        ├── /api/files/*     ──►  本地文件服务（只读）
        ├── /api/data/*      ──►  DuckDB 数据查询（MCP 暴露）
        ├── /api/chat/*      ──►  AI 助手（LLM 对话 + GPU 数据上下文）
        ├── /api/events      ──►  SSE 实时推送（数据变更通知）
        └── /api/logs/*      ──►  日志流

pySdp/
  webui/        FastAPI 应用（后端 + 静态资源 + SSE 实时推送）
  analysis/     Python 分析服务（分类、统计、topdc 等）
  data/         DuckDB 数据层（ingest、query、models、questions）
  pysdp/        独立 Python 客户端包（用于脚本/CI）
```

---

## 安装

### Windows

```powershell
# 1. 克隆
git clone https://github.com/mysheng8/pysdp && cd pysdp

# 2. 配置 — 在项目根目录创建 .env 文件：
#
#    # --- 路径 ---
#    PYSDP_PROJECT_DIR=D:/your/project           # SDP 文件和分析输出目录
#
#    # --- LLM（DrawCall 分类 / IR3→GLSL 反编译 / 报告生成）---
#    # 高频批量调用（每帧数百次），用最便宜的轻量模型
#    PYSDP_LLM_API_ENDPOINT=https://...
#    PYSDP_LLM_API_KEY=sk-...
#    PYSDP_LLM_MODEL=vertex_ai/gemini-2.5-flash-lite
#
#    # --- VLM（截图场景描述）---
#    # 需要视觉理解能力，用支持图片输入的多模态模型
#    PYSDP_VLM_API_ENDPOINT=https://...
#    PYSDP_VLM_API_KEY=sk-...
#    PYSDP_VLM_MODEL=...
#
#    # --- Chat AI（WebUI 侧边栏助手）---
#    # 交互式对话，需要较强推理能力，用中高端模型
#    PYSDP_CHAT_API_ENDPOINT=https://...
#    PYSDP_CHAT_API_KEY=sk-...
#    PYSDP_CHAT_MODEL=vertex_ai/gemini-2.5-flash
#
#    详见 config.ini 了解所有可用配置项及默认值。

# 3. 安装：创建 .venv，安装 Python 依赖，下载 SDPCLI
.\install.ps1

# 4. 启动
.\webui.ps1
```

`webui.ps1` 自动完成：终止占用端口的进程 → 加载 `.env` → 启动 SDPCLI Server（带 `-projectdir`）→ 启动 WebUI → 打开浏览器。按 **ESC** 停止所有进程。

打开 **http://localhost:8000**。

> API 文档（Swagger）：**http://localhost:8000/api/docs**

### SDPCLI 依赖

pySdp 需要 SDPCLI 来完成截帧和离线分析（C# 提取步骤）。`install.ps1` 会自动下载；如果缺失，`webui.ps1` 将无法启动。

### Project Directory

`ProjectDir` 是 pySdp 的核心工作目录，所有数据都存放在这里：

```
ProjectDir/
├── sdp/              # SDPCLI 截帧输出的 .sdp 文件
└── analysis/         # Python analysis pipeline 的输出（每次截帧一个子目录）
    └── <run_name>/
        └── snapshot_N/
            ├── dc.json, metrics.json, label.json, shaders.json, ...
            ├── shaders/          # disasm + glsl 文件
            ├── textures/         # 贴图文件
            └── meshes/           # OBJ 网格文件
```

设置方式：

- **推荐**：在 `.env` 中配置 `PYSDP_PROJECT_DIR=D:/your/project`
- **命令行覆盖**：`.\webui.ps1 -ProjectDir D:\other\project`（优先级高于 .env）

`webui.ps1` 启动时会读取 ProjectDir 并自动传给 SDPCLI（`-projectdir` 参数），无需分别配置。

### 自定义端口

```powershell
.\webui.ps1 -Port 8080 -SdpcliPort 5001
```

### SDPCLI 路径

`install.ps1` 下载 SDPCLI 到 `%USERPROFILE%\.pysdp\sdpcli\SDPCLI.exe`。  
强制重新下载（如 `pyproject.toml` 中版本号更新后）：`.\install.ps1 -Force`  
使用自定义路径：在 `.env` 中设置 `PYSDP_SDPCLI_PATH=C:\path\to\SDPCLI.exe`。

---

## 使用指南

### 截帧 + 分析（WebUI 操作流程）

1. **首页点击 "+" 卡片** → 打开 New Capture 弹窗
2. **Step 0 — 选择 Project & Version**（用于数据归类，可后续更改）
3. **Step 1 — Connect** → 选择设备 → 点击 Connect（等待连接成功）
4. **Step 2 — Launch** → 选择 App 包名 → 选择 Vulkan/GLES → 点击 Launch
5. **Step 3 — Capture** → App 运行到目标场景时点击 Capture（等待截帧完成）
6. **点击 "Analyze →"** → 自动执行 C# 提取 + Python analysis pipeline

分析完成后首页卡片会出现缩略图，双击进入 Explorer 查看结果。

### 关于 .sdp 文件

必须使用 pySdp 自带的截帧流程（通过 SDPCLI）来截帧，才能进行后续 analysis。pySdp 截帧生成的 `.sdp` 文件与官方 Snapdragon Profiler 格式兼容，也可以用官方工具打开查看。

如果已有通过 pySdp 截帧的 `.sdp` 文件（如从同事处获得）：

1. 将 `.sdp` 文件放到 `ProjectDir/sdp/` 目录下
2. 启动 WebUI → 首页会自动扫描并显示该文件
3. 选中卡片 → 点击 Analyze → 等待分析完成
4. 双击卡片进入 Explorer

### Explorer 界面

- **左侧 — Draw Call 列表**
  - **Params tab**：DC 调用参数，展开可查看子调用（setpass）
  - **Metrics tab**：GPU 硬件计数器（clocks、带宽、shader 繁忙率等）
  - **Label tab**：AI 分类结果（category / subcategory / confidence）
  - Category 筛选下拉框按类别过滤
  - 点击列头排序

- **右侧 — DC 详情面板**
  - Metrics 热力图（红色 = 该指标远高于中位数）
  - Shader 源码查看（GLSL ↔ DISASM 切换，支持 Recompile）
  - 纹理列表 + 预览
  - Mesh 3D 预览（OBJ）
  - Render Target 信息

### AI Chat

点击右上角 Chat 按钮打开侧边栏，支持自然语言查询 GPU 数据：

- "这一帧最耗时的 5 个 DC 是什么？"
- "UI 类 DC 占了多少 clocks？"
- "fragment shader stall 最严重的是哪些 DC？"

可以 Pin 指定 snapshot 作为查询上下文。

---

## 配置

配置优先级（从高到低）：

1. **环境变量**（`PYSDP_*`）— 最高优先级
2. **`.env` 文件** — 本地开发密钥（已 git-ignore）
3. **`config.ini`** — 已提交的默认值（不含密钥）

详见 `.env.example`。

### 日志级别

在 `config.ini` 中设置 `PyLogLevel=debug|info|warning|error`，或使用环境变量 `PYSDP_LOG_LEVEL`。

---

## GLES Shader 反编译 (ir3-disasm)

GLES 截帧使用 Adreno IR3 反汇编。`ir3-disasm`（[Mesa freedreno](https://gitlab.freedesktop.org/mesa/mesa)）已内置于 SDPCLI 包中，无需手动配置。芯片 ID 由 SDPCLI 截帧时自动检测。

支持的 Adreno GPU：

| Chip ID | GPU | 设备示例 |
|---|---|---|
| 0x06030001 | Adreno 660 | Snapdragon 888 |
| 0x06030500 | Adreno 7c+ Gen 3 / 8c Gen 3 | Snapdragon 7c+ Gen 3, QCM6490 |
| 0x06060201 | Adreno 662 (FD644) | — |
| 0x06060300 | FD663 | — |
| 0x07002000 | FD702 | QRB2210 |
| 0x07030001 | Adreno 730 | Snapdragon 8 Gen 1 |
| 0x07030002 | Adreno 725 | Snapdragon 7s Gen 2 |
| 0x43030B00 | FD735 | — |
| 0x43030c00 | Adreno X1-45 | Snapdragon X Plus |
| 0x43050a00 | Adreno A32 | G3x Gen 2 |
| 0x43050a01 | Adreno 740 | Snapdragon 8 Gen 2 |
| 0x43050b00 | Adreno 740 v3 | Meta Quest 3 |
| 0x43050c01 | Adreno X1-85 | Snapdragon X Elite |
| 0x43051401 | Adreno 750 | Snapdragon 8 Gen 3 |
| 0x44010000 | Adreno 810 | Snapdragon 8 Elite |
| 0x44030a20 | Adreno 829 | — |
| 0x44050001 | Adreno 830 | Snapdragon 8 Elite (variant) |
| 0x44050A31 | Adreno 840 | — |
| 0x44070041 | Adreno X2-85 | Snapdragon X2 Elite |
