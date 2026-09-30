"""
文档分块脚本：对解析后的年报做分块处理

教学重点（三种分块策略的对比）：
  策略A  固定大小分块  —— 最简单，但会切断句子/表格
  策略B  语义分块      —— 按段落/章节边界切，保留语义完整性
  策略C  层级分块      —— 父子块：父块用于召回上下文，子块用于精确匹配

企业级 RAG 通常用 B 或 C，
让学生先跑通 A，再体会 B/C 在召回效果上的区别。

输出格式说明：
  每个 chunk 是一个 dict，包含：
    - chunk_id      唯一标识
    - content       文本内容（供 embedding）
    - metadata      元信息（供过滤/溯源）
      - stock_code  股票代码
      - year        年份
      - page_num    来源页码
      - section     章节路径（字符串）
      - block_type  text/table/title
      - is_ocr      是否 OCR 结果
      - strategy    分块策略名
"""

import json
import uuid
import argparse
import logging
from pathlib import Path
from typing import Iterator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PARSED_DIR = Path(__file__).parent.parent / "data" / "parsed"
CHUNKS_DIR = Path(__file__).parent.parent / "data" / "chunks"
CHUNKS_DIR.mkdir(parents=True, exist_ok=True)


# ── 策略 A：固定大小分块 ──────────────────────────────────────────────────────

def chunk_fixed(
    text: str,
    chunk_size: int = 500,
    overlap: int = 50,
) -> Iterator[str]:
    """
    按字符数切块，相邻块有重叠。
    缺点：无视句子/段落边界，表格会被切断。
    优点：实现最简单，块大小可预测。
    """
    start = 0
    while start < len(text):
        end = start + chunk_size
        yield text[start:end]
        start += chunk_size - overlap


# ── 公共工具 ─────────────────────────────────────────────────────────────────

def _meta_of(group: list[dict]) -> dict:
    """从块组推导元信息：首块页码/章节路径、组内块类型集合、是否含 OCR"""
    return {
        "page_num":    group[0]["page_num"],
        "section":     " > ".join(group[0]["section_path"]) if group[0]["section_path"] else "",
        "block_types": list({b["block_type"] for b in group}),
        "is_ocr":      any(b["is_ocr"] for b in group),
    }


def _group_blocks(blocks: list[dict], max_size: int) -> list[list[dict]]:
    """
    沿块边界把 blocks 累积成组（组内 content 总长不超过 max_size）：
      - title 块：先 flush 当前组，标题作为新组前缀（提供章节上下文）
      - table 块：单独成组（保证表格完整，不与其他文字混杂）
      - 文字块：累积，超 max_size 强制切组
    不会切断段落/表格，元信息可通过 _meta_of 从组首块恢复。
    """
    groups = []
    cur, cur_len = [], 0

    def flush():
        nonlocal cur, cur_len
        if cur:
            groups.append(cur)
        cur, cur_len = [], 0

    for block in blocks:
        btype = block["block_type"]
        blen  = len(block["content"])
        if btype == "title":
            flush()
            cur, cur_len = [block], blen
        elif btype == "table":
            flush()
            groups.append([block])
        else:
            if cur_len + blen > max_size and cur:
                flush()
            cur.append(block)
            cur_len += blen
    flush()
    return groups


# ── 策略 B：语义分块 ──────────────────────────────────────────────────────────

def chunk_semantic(
    blocks: list[dict],
    max_chunk_size: int = 800,
    min_chunk_size: int = 100,
) -> Iterator[dict]:
    """
    按解析结构分块：遇到标题强制切块，段落尽量合并到 max_chunk_size 以内。

    优点：保留语义完整性，章节边界清晰。
    缺点：块大小不均匀（财务报表单个表格可能很大）。
    """
    buffer_blocks = []
    buffer_len    = 0
    buffer_meta   = {}

    def flush(buf: list[dict]) -> dict | None:
        if not buf:
            return None
        content = "\n\n".join(b["content"] for b in buf)
        return {"content": content, "metadata": _meta_of(buf)}

    for block in blocks:
        btype = block["block_type"]
        blen  = len(block["content"])

        # 标题块：强制先 flush，标题单独作为一个小块（不用于 embedding，
        # 但保留在 chunk 里作为上下文前缀）
        if btype == "title":
            if buffer_blocks:
                result = flush(buffer_blocks)
                if result and len(result["content"]) >= min_chunk_size:
                    yield result
                buffer_blocks = []
                buffer_len    = 0

        # 表格块：单独成块（不与文字合并，防止 embedding 效果变差）
        if btype == "table":
            if buffer_blocks:
                result = flush(buffer_blocks)
                if result and len(result["content"]) >= min_chunk_size:
                    yield result
                buffer_blocks = []
                buffer_len    = 0
            yield {
                "content": block["content"],
                "metadata": {
                    "page_num": block["page_num"],
                    "section":  " > ".join(block["section_path"]),
                    "block_types": ["table"],
                    "is_ocr":   block["is_ocr"],
                }
            }
            continue

        # 文字块：累积，超过 max_chunk_size 就 flush
        if buffer_len + blen > max_chunk_size and buffer_blocks:
            result = flush(buffer_blocks)
            if result and len(result["content"]) >= min_chunk_size:
                yield result
            buffer_blocks = []
            buffer_len    = 0

        buffer_blocks.append(block)
        buffer_len += blen

    # 尾部剩余
    if buffer_blocks:
        result = flush(buffer_blocks)
        if result and len(result["content"]) >= min_chunk_size:
            yield result


# ── 策略 C：层级分块（父子块） ────────────────────────────────────────────────

def chunk_hierarchical(
    blocks: list[dict],
    parent_size: int = 2000,
    child_size:  int = 400,
) -> Iterator[dict]:
    """
    层级分块（父块 → 子块）：
      父块：沿章节边界累积的大段落（title 作前缀、table 单独成组），供 LLM 读上下文
      子块：父块内再沿块边界切出的小段落，用于向量检索（更精确）

    每个子块带 parent_id + parent_content（完整父块文本）：
    检索时命中子块 → 直接把父块内容喂给 LLM，即 "小to大检索"（Small-to-Big Retrieval）。
    元信息（页码/章节/块类型）沿块继承，不丢失解析阶段信息。
    """
    for parent_blocks in _group_blocks(blocks, parent_size):
        parent_id      = str(uuid.uuid4())[:8]
        parent_content = "\n\n".join(b["content"] for b in parent_blocks)
        for child_blocks in _group_blocks(parent_blocks, child_size):
            yield {
                "content":  "\n\n".join(b["content"] for b in child_blocks),
                "metadata": {
                    **_meta_of(child_blocks),
                    "parent_id":      parent_id,
                    "parent_content": parent_content,
                }
            }


# ── 主流程 ────────────────────────────────────────────────────────────────────

STRATEGY = "semantic"   # 改成 "fixed" 或 "hierarchical" 体验不同策略

def build_chunk_id(stock_code: str, year: str, idx: int) -> str:
    return f"{stock_code}_{year}_{idx:05d}"


def process_file(parsed_path: Path, strategy: str = STRATEGY):
    with open(parsed_path, encoding="utf-8") as f:
        data = json.load(f)

    meta   = data.get("meta", {})
    blocks = data.get("blocks", [])

    stock_code = meta.get("stock_code", "unknown")
    year       = meta.get("year", "unknown")

    logger.info(f"分块 {parsed_path.name}  策略={strategy}  blocks={len(blocks)}")

    # 根据策略生成 chunks
    raw_chunks = []

    if strategy == "fixed":
        full_text = "\n\n".join(b["content"] for b in blocks)
        for text_chunk in chunk_fixed(full_text):
            raw_chunks.append({
                "content":  text_chunk,
                "metadata": {"block_types": ["text"], "is_ocr": False, "section": "", "page_num": -1}
            })

    elif strategy == "semantic":
        for chunk in chunk_semantic(blocks):
            raw_chunks.append(chunk)

    elif strategy == "hierarchical":
        for chunk in chunk_hierarchical(blocks):
            raw_chunks.append(chunk)

    else:
        raise ValueError(f"未知策略: {strategy}")

    # 补充公共元信息
    result = []
    for idx, chunk in enumerate(raw_chunks):
        chunk_id = build_chunk_id(stock_code, year, idx)
        chunk["chunk_id"]              = chunk_id
        chunk["metadata"]["stock_code"] = stock_code
        chunk["metadata"]["year"]       = year
        chunk["metadata"]["strategy"]   = strategy
        chunk["metadata"]["source_file"] = parsed_path.name
        result.append(chunk)

    # 保存
    out_path = CHUNKS_DIR / f"{parsed_path.stem}_{strategy}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"  → {len(result)} 个 chunk，已保存 {out_path.name}")
    return result


def main():
    parser = argparse.ArgumentParser(description="年报分块脚本（fixed / semantic / hierarchical）")
    parser.add_argument("--strategy", choices=("fixed", "semantic", "hierarchical"),
                        default=STRATEGY, help=f"分块策略（默认 {STRATEGY}）")
    args = parser.parse_args()

    parsed_files = list(PARSED_DIR.glob("*.json"))
    if not parsed_files:
        logger.error("没有找到解析结果，请先运行 parse_pdf.py")
        return

    all_chunks = []
    for path in parsed_files:
        chunks = process_file(path, strategy=args.strategy)
        all_chunks.extend(chunks)

    # 合并所有公司的 chunk 到一个文件，方便统一建索引
    combined_path = CHUNKS_DIR / f"all_{args.strategy}.json"
    with open(combined_path, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, ensure_ascii=False, indent=2)

    logger.info(f"\n合并完成：共 {len(all_chunks)} 个 chunk → {combined_path}")

    # 简单统计
    avg_len = sum(len(c["content"]) for c in all_chunks) / max(len(all_chunks), 1)
    logger.info(f"平均 chunk 长度: {avg_len:.0f} 字符")

    table_count = sum(1 for c in all_chunks if "table" in c["metadata"].get("block_types", []))
    ocr_count   = sum(1 for c in all_chunks if c["metadata"].get("is_ocr"))
    logger.info(f"其中表格块: {table_count}  OCR块: {ocr_count}")


if __name__ == "__main__":
    main()
