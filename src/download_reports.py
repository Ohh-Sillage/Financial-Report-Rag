"""
数据下载脚本：从巨潮资讯网批量下载上市公司年报

巨潮资讯网（cninfo.com.cn）是中国证监会指定的上市公司信息披露平台
所有年报均为公开信息，下载合法合规
"""

import os
import re
import time
import json
import random
import argparse
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

RAW_DIR = Path(__file__).parent.parent / "data" / "raw_pdf"
RAW_DIR.mkdir(parents=True, exist_ok=True)

# 股票代码、所属板块、公司简称
TARGET_STOCKS = [
    ("00285", "hk", "比亚迪电子"),   # 港交所主板
    ("002415", "sz", "海康威视"),    # 深交所主板
    ("688297", "sh", "中无人机"),    # 上交所科创板
    ("688169", "sh", "石头科技"),    # 上交所科创板
    ("01810", "hk", "小米集团-W"),   # 港交所主板
]

TARGET_YEARS = ["2023", "2024", "2025"]

CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_BASE_URL  = "https://static.cninfo.com.cn/"
TOPSEARCH_URL    = "https://www.cninfo.com.cn/new/information/topSearch/query"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.cninfo.com.cn/",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://www.cninfo.com.cn",
    "X-Requested-With": "XMLHttpRequest",
}

MAX_ATTEMPTS = 3             # 下载重试次数
MIN_PDF_SIZE = 100 * 1024    # 小于该字节数的文件视为异常（错误页/空文件）
PAGE_SIZE    = 30            # 公告查询每页条数
MAX_PAGES    = 4             # 港股翻页查找上限

# 年报标题关键词（A股"年度报告"、港股简体"年年报"/繁体"年報"）
REPORT_TITLE_KEYS = ("年度报告", "年度報告", "年报", "年報")
# 港股 searchkey 关键词（column=hke 下关键词可直接命中年报，标题风格差异大故逐个尝试）
HK_SEARCH_KEYS = ("年度报告", "年报", "年報", "年度報告")


def _politeness_sleep() -> None:
    """防反爬随机延时（每个查询/下载任务之间）"""
    time.sleep(random.uniform(1.5, 3.0))


def _clean_title(title: str) -> str:
    """去掉巨潮返回标题中的 <em> 高亮标签"""
    return re.sub(r"</?em>", "", title)


def _is_annual_report_title(title: str) -> bool:
    """标题是否为年报全报（排除摘要/英文）"""
    return (
        any(k in title for k in REPORT_TITLE_KEYS)
        and "摘要" not in title
        and "英文" not in title
    )


def _filter_annual(reports: list[dict], year: str | None = None,
                   require_year: bool = False) -> list[dict]:
    """
    从公告列表中筛选年报候选（标题关键词 + 排除摘要/英文 + PDF 附件）。
    require_year=True 时强制标题含目标年份（如"2024"）；否则优先含年份，无则保留全部
    （兼容汉字年份标题如"二零二三"）。
    """
    hits = [
        r for r in reports
        if _is_annual_report_title(r.get("announcementTitle", ""))
        and r.get("adjunctUrl", "").upper().endswith(".PDF")
    ]
    if year:
        with_year = [r for r in hits if year in r.get("announcementTitle", "")]
        if with_year or require_year:
            return with_year
    return hits


def _pick_best_report(candidates: list[dict]) -> dict:
    """从候选中挑选：优先修订版年报（数据以修订版为准），否则取第一条"""
    for r in candidates:
        if "修订" in r.get("announcementTitle", ""):
            return r
    return candidates[0]


def _split_name(company_name: str) -> str:
    """公司名拆字加空格（去掉 -W 等后缀，避免拆出孤立符号）"""
    return " ".join(list(company_name.split("-")[0]))


def _post_json(url: str, payload: dict, retries: int = 2) -> dict | None:
    """POST 表单请求并解析 JSON；失败重试后返回 None（与"无结果"区分）"""
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, data=payload, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"  请求失败（第{attempt+1}次）: {e}")
            if attempt < retries:
                time.sleep(1.5)
    return None


def _do_query(payload: dict) -> list[dict] | None:
    """执行公告查询，返回公告列表；接口/网络异常返回 None"""
    data = _post_json(CNINFO_QUERY_URL, payload)
    return None if data is None else (data.get("announcements") or [])


def _first_hit(keywords: tuple, base_payload: dict, year: str | None,
               require_year: bool) -> list[dict] | None:
    """
    依次用关键词查询公告，返回第一个含年报候选的结果。
    网络错误返回 None；全部关键词均无有效结果返回 []。
    """
    for kw in keywords:
        results = _do_query({**base_payload, "searchkey": kw})
        if results is None:
            return None
        hits = _filter_annual(results, year, require_year)
        if hits:
            return hits
    return []


HK_ORGID_CACHE: dict[str, str] = {}


def _get_hk_org_id(stock_code: str, company_name: str) -> str | None:
    """
    通过 topSearch 接口获取港股 orgId（巨潮港股 orgId 格式不统一：
    数字如 9900037222，或 gshk 前缀如 gshk0000285）。
    该接口仅支持 POST；结果缓存避免每个任务重复查询。
    """
    if stock_code in HK_ORGID_CACHE:
        return HK_ORGID_CACHE[stock_code]
    for kw in (company_name, stock_code):
        data = _post_json(TOPSEARCH_URL, {"keyWord": kw, "maxNum": 10})
        if not isinstance(data, list):
            continue
        for item in data:
            if str(item.get("code")) == stock_code and item.get("category") == "港股":
                org_id = item.get("orgId")
                if org_id:
                    HK_ORGID_CACHE[stock_code] = org_id
                    return org_id
    logger.warning(f"  获取港股 orgId 失败({stock_code})")
    return None


def _disclosure_window(year: str) -> str:
    """年报披露窗口：次年 1~6 月"""
    pub_year = str(int(year) + 1)
    return f"{pub_year}-01-01~{pub_year}-06-30"


def _build_payload(*, column: str, se_date: str, plate: str = "", stock: str = "",
                   category: str = "", searchkey: str = "", page_num: int = 1) -> dict:
    """构造公告查询表单"""
    return {
        "pageNum": page_num, "pageSize": PAGE_SIZE, "column": column, "tabName": "fulltext",
        "plate": plate, "stock": stock, "searchkey": searchkey, "secid": "",
        "category": category, "trade": "",
        "seDate": se_date, "sortName": "", "sortType": "", "isHLtitle": True,
    }


def _query_hk_annual(stock_code: str, company_name: str, year: str) -> list[dict] | None:
    """港股年报：column=hke + stock=代码,orgId 精确定位，关键词命中优先、翻页兜底"""
    org_id = _get_hk_org_id(stock_code, company_name)
    if org_id is None:
        return None
    base = _build_payload(column="hke", stock=f"{stock_code},{org_id}",
                          se_date=_disclosure_window(year))
    # column=hke 下 searchkey 生效，关键词可直接命中年报，无需翻页
    hits = _first_hit(HK_SEARCH_KEYS, base, year, require_year=False)
    if hits or hits is None:
        return hits
    # 兜底：关键词均未命中（标题不含任何年报字样），全量公告翻页查找
    for page in range(1, MAX_PAGES + 1):
        results = _do_query(_build_payload(column="hke", stock=f"{stock_code},{org_id}",
                                           se_date=_disclosure_window(year), page_num=page))
        if results is None:
            return None
        hits = _filter_annual(results, year)
        if hits:
            return hits
        if len(results) < PAGE_SIZE:
            break
    return []


def _query_a_annual(stock_code: str, plate: str, company_name: str, year: str) -> list[dict] | None:
    """A股年报：策略1 公司名+年份关键词；策略2 简称/拆字全文搜索 + 标题过滤"""
    base = _build_payload(column={"sh": "sse", "sz": "szse"}[plate], plate=plate,
                          category="category_ndbg_szsh", se_date=_disclosure_window(year))
    # 策略1：若命中的全是摘要/英文（如"石头科技2025年度报告"只命中摘要），继续策略2
    hits = _first_hit((f"{company_name}{year}年度报告",), base, year, require_year=False)
    if hits or hits is None:
        return hits
    # 策略2：简称全文搜索（标题用全称时靠分词命中，如"北京石头世纪科技股份有限公司..."），
    #         再退一步：拆字搜索（巨潮部分股票 secName 字间带空格，如"五 粮 液"）
    return _first_hit((company_name, _split_name(company_name)), base, year, require_year=True)


def query_annual_reports(stock_code: str, plate: str, company_name: str, year: str) -> list[dict] | None:
    """按板块分发年报查询：港股按代码精确定位，A股按关键词搜索"""
    if plate == "hk":
        return _query_hk_annual(stock_code, company_name, year)
    return _query_a_annual(stock_code, plate, company_name, year)


def _is_valid_pdf(path: Path) -> bool:
    """校验下载结果：大小达标且以 %PDF 魔数开头，防止错误页/空文件入库"""
    try:
        if path.stat().st_size < MIN_PDF_SIZE:
            logger.warning(f"  文件过小（{path.stat().st_size} B），疑似错误页: {path.name}")
            return False
        with open(path, "rb") as f:
            if f.read(4) != b"%PDF":
                logger.warning(f"  文件头不是 %PDF，疑似非 PDF 内容: {path.name}")
                return False
        return True
    except OSError as e:
        logger.warning(f"  文件校验异常: {e}")
        return False


def download_pdf(pdf_url: str, save_path: Path) -> bool:
    """
    下载 PDF：先写临时 .part 文件，校验通过后原子改名落盘。
    - 已存在正式文件则跳过（幂等，中断可续跑）
    - 每次重跑先清理残留 .part，避免截断文件被永久跳过
    """
    if save_path.exists():
        logger.info(f"已存在，跳过: {save_path.name}")
        return True

    tmp_path = save_path.with_suffix(save_path.suffix + ".part")
    tmp_path.unlink(missing_ok=True)

    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = requests.get(pdf_url, headers=HEADERS, timeout=120, stream=True)
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)

            if not _is_valid_pdf(tmp_path):
                raise ValueError("非有效 PDF（魔数不符或文件过小）")

            os.replace(tmp_path, save_path)   # 原子改名，避免残留截断文件
            size_kb = save_path.stat().st_size // 1024
            logger.info(f"下载成功: {save_path.name}  ({size_kb} KB)")
            return True
        except Exception as e:
            logger.warning(f"  第{attempt+1}次失败: {e}")
            tmp_path.unlink(missing_ok=True)
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)

    logger.error(f"下载失败: {pdf_url}")
    return False


def sanitize(name: str) -> str:
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip()


def process_one(task: tuple) -> dict | None:
    """处理单个「股票 × 年份」任务，返回 manifest 条目（失败返回 None）"""
    stock_code, plate, company_name, year = task
    logger.info(f"── {company_name}({stock_code}) {year}年报 ──")
    try:
        reports = query_annual_reports(stock_code, plate, company_name, year)
        if reports is None:
            logger.warning(f"  {company_name} {year}: 查询接口异常，跳过（可重跑补下）")
            return None
        if not reports:
            logger.warning(f"  {company_name} {year}: 未找到匹配年报，跳过")
            return None
    except Exception as e:
        logger.warning(f"  {company_name} {year}: 查询异常: {e}")
        return None
    finally:
        _politeness_sleep()

    report   = _pick_best_report(reports)   # 修订版优先
    title    = _clean_title(report["announcementTitle"])
    pdf_url  = CNINFO_BASE_URL + report["adjunctUrl"]
    filename = sanitize(f"{stock_code}_{year}_{company_name}_{title}.pdf")
    save_path = RAW_DIR / filename

    if not download_pdf(pdf_url, save_path):
        return None
    return {
        "stock_code":   stock_code,
        "plate":        plate,
        "company_name": company_name,
        "year":         year,
        "title":        title,
        "filename":     filename,
        "source_url":   pdf_url,
        "announce_id":  report.get("announcementId"),
    }


def _run_tasks(tasks: list, workers: int):
    """按 workers 并行（>1 线程池）或串行执行任务，逐个产出成功条目"""
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(process_one, t): t for t in tasks}
            for fut in as_completed(futures):
                try:
                    yield fut.result()
                except Exception as e:
                    logger.warning(f"任务异常: {e}")
    else:
        for t in tasks:
            try:
                yield process_one(t)
            except Exception as e:
                logger.warning(f"任务异常: {e}")


def main():
    parser = argparse.ArgumentParser(description="从巨潮资讯网批量下载上市公司年报 PDF")
    parser.add_argument("--workers", type=int, default=1,
                        help="并发下载线程数（默认 1 = 串行）")
    args = parser.parse_args()

    tasks = [(code, plate, name, year)
             for code, plate, name in TARGET_STOCKS
             for year in TARGET_YEARS]

    manifest = [item for item in _run_tasks(tasks, args.workers) if item]

    # 按 TARGET_STOCKS × TARGET_YEARS 固定顺序输出，便于人工核对
    order = {(t[0], t[1], t[3]): i for i, t in enumerate(tasks)}
    manifest.sort(key=lambda m: order[(m["stock_code"], m["plate"], m["year"])])

    manifest_path = RAW_DIR.parent / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    logger.info(f"\n完成！共下载 {len(manifest)}/{len(tasks)} 份年报")
    for item in manifest:
        logger.info(f"  {item['company_name']} {item['year']}: {item['filename']}")


if __name__ == "__main__":
    main()
