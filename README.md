# 上市公司年报 RAG 问答系统

> 个人研究项目：以"上市公司年度报告智能问答"为场景，覆盖 **数据获取 → PDF 解析 → 分块 → 向量索引 → 混合检索 → 重排 → 生成 → 评估** 的完整 RAG 流水线；同时提供"原生实现"与"LangChain 实现"双版本对照，接近企业级落地标准。

---

## 项目亮点

- **真实数据、真实噪声**：巨潮资讯网（证监会指定披露平台）5 家公司 × 3 年 = 15 份年报 PDF（约 85 MB），包含扫描件、嵌套表格、页眉页脚噪声
- **双版本对照**
  - 原生版（`src/`，约 700 行）——每个环节手动可控：混合检索 + RRF 融合 + CrossEncoder 重排
  - LangChain 版（`src_langchain/`，约 200 行）——LCEL 链路组装，聚焦框架抽象对比
- **混合检索**：FAISS 向量检索 + jieba/BM25 关键词检索 + RRF 融合 + bge-reranker-base 精排（可选），互补语义理解与精确数字两类查询
- **三种分块策略**：固定大小 / 语义 / 层级（Small-to-Big），支持消融实验对比
- **幻觉控制**：相关性阈值拒答 + 答案逐条引用溯源（可定位到页码/章节）+ 3 道"应拒绝回答"测试题
- **完整评估体系**：20 题评测集（5 类）+ RAGAS 四项指标 + 分块 × 检索消融矩阵（Hit Rate@4 / MRR）
- **多种使用方式**：CLI 交互问答 / 单次查询 / Python 模块调用 / FastAPI HTTP 服务 / 可视化 Web 页面

## 效果示例

```
问题：海康威视2024年营业收入是多少

回答：海康威视2024年营业总收入为人民币9,249,600.00万元[2]。

来源：
  [2] 002415 2024年报 · 第三节 > 管理层讨论与分析 · 第33页
```

## 数据集

| 公司 | 代码 | 市场 | 行业 | 年报年份 |
|------|------|------|------|---------|
| 海康威视 | 002415 | 深交所 | 智能物联 | 2023 / 2024 / 2025 |
| 比亚迪电子 | 00285 | 港交所 | 电子制造 | 2023 / 2024 / 2025 |
| 小米集团-W | 01810 | 港交所 | 消费电子 | 2023 / 2024 / 2025 |
| 石头科技 | 688169 | 上交所科创板 | 智能硬件 | 2023 / 2024 / 2025 |
| 中无人机 | 688297 | 上交所科创板 | 无人机 | 2023 / 2024 / 2025 |

> 数据来源：[巨潮资讯网](https://www.cninfo.com.cn)（公开合法）。PDF 原件、解析结果与向量索引等大文件不入库，运行 `python src/download_reports.py` 可自动重新下载；数据元信息见 `data/manifest.json`。

## 系统流水线

```
年报 PDF（巨潮资讯网 15 份 / 约 85 MB）
  │
  ▼  src/download_reports.py ── 数据获取（巨潮 API 搜索 + 下载 + manifest）
  ▼  src/parse_pdf.py ───────── PDF 解析（文字 + 表格 + OCR + 章节结构）
  ▼  src/chunk_documents.py ─── 文档分块（fixed / semantic / hierarchical）
  ▼  src/build_index.py ─────── 向量化 + FAISS 索引（DashScope API / 本地 BGE）
  ▼  src/rag_pipeline.py ────── 向量 + BM25 → RRF 融合 → Rerank → qwen-plus 生成
  ▼  evaluation/ ────────────── RAGAS 四项指标 + 消融实验
```

## 快速开始

### 0. 环境准备

```bash
pip install -r requirements.txt

# Windows
set DASHSCOPE_API_KEY=sk-xxxxxxxx
# Linux / macOS
export DASHSCOPE_API_KEY=sk-xxxxxxxx
```

DashScope API Key 申请：https://dashscope.console.aliyun.com/ （生成与评估均使用 DashScope，无需 OpenAI Key）

### 1. 原生版：完整流水线

```bash
python src/download_reports.py    # ① 下载 15 份年报 PDF → data/raw_pdf/
python src/parse_pdf.py           # ② PDF 解析 → data/parsed/
python src/chunk_documents.py     # ③ 分块（默认 semantic）→ data/chunks/
python src/build_index.py         # ④ 向量化 + 建索引 → vectorstore/

# 问答（交互式 / 单次 / 过滤 / 查询改写）
python src/rag_pipeline.py
python src/rag_pipeline.py --query "海康威视2024年营业收入是多少"
python src/rag_pipeline.py --query "营业收入" --stock 002415 --year 2024
python src/rag_pipeline.py --query "小米最近怎么样" --query-rewrite
```

### 2. LangChain 版

```bash
python src_langchain/download_model.py   # 下载本地 BGE 模型（约 90 MB）
python src_langchain/build_index_lc.py   # 本地推理建索引（无 API 费用）
python src_langchain/rag_chain_lc.py     # LCEL 链问答
```

### 3. HTTP 服务 + 可视化页面

```bash
cd src
uvicorn serve:app --host 0.0.0.0 --port 8000
```

- `http://localhost:8000` —— 可视化问答页面
- `http://localhost:8000/docs` —— Swagger 接口文档

### 4. 评估

```bash
python evaluation/evaluate.py --pipeline both   # RAGAS 四项指标（两版对比）
python evaluation/compare_strategies.py         # 分块策略 × 检索方式 消融实验
```

更多参数与模块调用方式见 [USAGE_GUIDE.md](USAGE_GUIDE.md)。

## 技术选型一览

| 环节 | 原生版（`src/`） | LangChain 版（`src_langchain/`） |
|------|------------------|----------------------------------|
| PDF 解析 | pdfplumber + PyMuPDF + pytesseract（OCR 可选） | PyMuPDFLoader |
| 分块 | fixed / semantic / hierarchical 三策略可切换 | RecursiveCharacterTextSplitter |
| Embedding | DashScope `text-embedding-v3`（1024 维，按量计费） | 本地 `BAAI/bge-small-zh-v1.5`（512 维，离线） |
| 向量库 | FAISS IndexFlatIP（手动管理） | LangChain FAISS 封装 |
| 关键词检索 | jieba + rank_bm25 | — |
| 融合 / 重排 | RRF 融合 + bge-reranker-base（可选） | — |
| LLM | qwen-plus（DashScope OpenAI 兼容接口） | qwen-plus |
| 查询改写 | qwen-turbo（`--query-rewrite` 可选） | — |
| 代码量 | 约 700 行 | 约 200 行 |
| 定位 | 生产级定制参考 | 框架快速原型 |

## 评测体系

- **20 道标准题（5 类）**：简单事实 ×5、精确数字 ×5、跨文档对比 ×4、跨年趋势 ×3、应拒绝回答 ×3（幻觉控制）
- **RAGAS 四项指标**：Faithfulness（忠实度）、Answer Relevancy（答案相关性）、Context Precision（上下文精确率）、Context Recall（上下文召回率）
- **消融实验**：分块策略（3）× 检索方式（vector_only / bm25_only / hybrid）= 9 种组合，指标 Hit Rate@4 与 MRR

评估结果存档于 `evaluation/results/`。

## 目录结构

```
Financial-Report-Rag/
├── src/                    # 原生版：下载 / 解析 / 分块 / 索引 / 问答 / FastAPI 服务
│   └── static/index.html   #   可视化 Web 页面
├── src_langchain/          # LangChain 版：模型下载 / 索引构建 / LCEL 链
├── evaluation/             # 评测题集（20 题）+ RAGAS 评估 + 消融实验 + 结果存档
├── data/                   # 数据目录（raw_pdf / parsed / chunks 不入库，manifest.json 入库）
├── vectorstore/            # FAISS 索引（不入库，可由脚本重建）
├── models/                 # 本地模型权重（不入库，自动下载）
├── requirements.txt
├── ARCHITECTURE.md         # 技术架构说明
├── USAGE_GUIDE.md          # 操作手册
└── PROJECT_LOG.md          # 开发日志
```

## 运行成本参考（DashScope）

| 步骤 | 耗时 | 费用 |
|------|------|------|
| 下载 + 解析 + 分块 | 约 10 min | 免费（本地） |
| 建向量索引（原生版） | 5~10 min | 约 0.35 元 |
| 建向量索引（LangChain 版） | 4~5 min | 免费（本地 BGE） |
| 单次问答 / HTTP 请求 | 2~5 s | 约 0.002 元/次 |
| RAGAS 评估（20 题） | 30~60 min | 约 5~10 元 |

## 文档

| 文档 | 内容 |
|------|------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 整体方案、技术选型决策与设计原理 |
| [USAGE_GUIDE.md](USAGE_GUIDE.md) | 完整操作手册：环境 → 流水线 → 模块调用 → HTTP 服务 → 评估 → FAQ |
| [PROJECT_LOG.md](PROJECT_LOG.md) | 开发日志：数据获取记录、踩坑与修复、运行验证 |

## 已知限制

- `parse_pdf.py` 中 `table_bboxes` 未用于过滤文字提取区域，表格内容在 `table` 与 `text` 块中重复；影响 chunk 数量，不影响答案质量
- 消融实验需手动切换 `chunk_documents.py` 中的 `STRATEGY` 分别构建三套索引，暂无批量脚本
- Rerank 与 LangChain 版 BGE 模型首次运行需下载（约 278 MB / 90 MB）
