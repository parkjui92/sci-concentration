#!/usr/bin/env python3
"""sci-concentration — 키워드 하나로 논문 추이·국가/기관 집중도를 표와 그래프로.

데이터: OpenAlex 공개 API(키 없이 동작, https://api.openalex.org). group_by 집계 API만 써서
키워드 1건당 호출 4회(--quick) 또는 4 + 연도 수(기본: 연도별 국가 순위)를 쓴다.
특허는 키 없는 공개 경로가 없어 이 도구에 들어 있지 않다(README 참조).

사용:
  python3 -I concentrate.py "AI semiconductor" --years 2015-2025
  python3 -I concentrate.py "AI 반도체 / AI semiconductor"           # '/' 앞은 라벨(국문), 뒤는 검색어
  python3 -I concentrate.py "휴머노이드 로봇 / humanoid robot" "첨단 바이오 / advanced biotechnology"   # 여러 키워드
  python3 -I concentrate.py "AI 반도체" --query '"AI semiconductor" OR "AI chip" OR "AI accelerator"'
  python3 -I concentrate.py --selftest                                 # 오프라인 자체 검사
출력은 현재 폴더의 out/<라벨>/ (--out 으로 변경).

표준 라이브러리 + matplotlib(그래프)만 쓴다. matplotlib이 없으면 표·요약만 만들고 그래프는 건너뛴다.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from pathlib import Path

API = "https://api.openalex.org/works"
TOOL_DIR = Path(__file__).resolve().parent
__version__ = "0.2.0"
UA = "sci-concentration/0.2 (https://github.com/parkjui92; local tool)"
KR = "KR"


# ----------------------------------------------------------------------------- 입력
def parse_keyword(raw: str, query_override: str | None, loose: bool):
    """'라벨 / 검색어' 또는 '검색어' → (라벨, 검색식). 국문만 주면 그대로 검색(경고)."""
    raw = raw.strip()
    label, query = raw, raw
    if "/" in raw:
        a, b = [s.strip() for s in raw.split("/", 1)]
        label, query = a, b
    if query_override:
        query = query_override.strip()
    else:
        has_bool = re.search(r"\b(OR|AND|NOT)\b", query) or '"' in query
        if not has_bool and not loose and " " in query:
            query = f'"{query}"'  # 두 낱말 이상이면 구 검색(stemmed phrase)
    if re.search(r"[가-힣]", query):
        print("※ 검색식에 한글이 있습니다. OpenAlex는 영문 제목·초록 기준이라 건수가 매우 적을 수 있습니다.", file=sys.stderr)
    return label, query


def read_key_file() -> str:
    """~/.config/sci-concentration/openalex.key 가 있으면 그 첫 줄을 키로 쓴다(권한 600 권장)."""
    p = Path.home() / ".config" / "sci-concentration" / "openalex.key"
    try:
        return p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except Exception:
        return ""


def safe_dirname(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s).strip()
    return s[:60] or "keyword"


# ----------------------------------------------------------------------------- API

def http_get_json(url: str, timeout: int = 40) -> dict:
    """urllib(기본 인증서) → certifi → curl 순으로 폴백. python.org 파이썬은 OS 인증서를 안 보는 경우가 있다."""
    import ssl, subprocess
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    ctxs = [None]
    try:
        import certifi  # type: ignore
        ctxs.append(ssl.create_default_context(cafile=certifi.where()))
    except Exception:
        pass
    last = None
    for ctx in ctxs:
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except Exception as e:  # SSL 등
            last = e
    try:
        out = subprocess.run(["curl", "-sS", "-m", str(timeout), "-A", UA, url], capture_output=True, text=True, check=True).stdout
        return json.loads(out)
    except Exception as e:
        raise urllib.error.URLError(f"{last} / curl: {e}")

class OpenAlex:
    def __init__(self, mailto: str | None, api_key: str | None, log: list):
        self.mailto, self.api_key, self.log = mailto, api_key, log

    def group(self, filt: str, group_by: str, extra: dict | None = None) -> dict:
        params = {"filter": filt, "group_by": group_by, "per_page": 200}
        if extra:
            params.update(extra)
        if self.mailto:
            params["mailto"] = self.mailto
        if self.api_key:
            params["api_key"] = self.api_key
        url = API + "?" + urllib.parse.urlencode(params, safe=':,"|()', quote_via=urllib.parse.quote)
        shown = re.sub(r"&api_key=[^&]+", "&api_key=***", url)
        t0 = time.time()
        for attempt in range(4):
            try:
                data = http_get_json(url)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                body = e.read().decode("utf-8", "replace")[:300]
                hint = ""
                if e.code == 429 and "budget" in body:
                    hint = ("\n  → 이 공인 IP의 키 없는 하루 예산이 소진됐습니다(자정 UTC=09:00 KST 초기화). "
                            "무료 키를 받아 OPENALEX_API_KEY 또는 ~/.config/sci-concentration/openalex.key 에 넣으세요: "
                            "https://openalex.org/settings/api")
                raise SystemExit(f"OpenAlex HTTP {e.code}: {body}{hint}\n  URL: {shown}")
            except urllib.error.URLError as e:
                raise SystemExit(f"OpenAlex 접속 실패: {e.reason}\n  URL: {shown}")
        self.log.append({"url": shown, "count": data.get("meta", {}).get("count"),
                         "groups": data.get("meta", {}).get("groups_count"),
                         "sec": round(time.time() - t0, 2),
                         "oql": (data.get("meta", {}).get("x_query") or {}).get("oql", "")})
        return data


# ----------------------------------------------------------------------------- 계산
def hhi(counts: list[int]) -> float:
    tot = sum(counts)
    if not tot:
        return 0.0
    return round(sum((c / tot * 100) ** 2 for c in counts), 1)


def pct(a: int, b: int) -> float:
    return round(a / b * 100, 1) if b else 0.0


# ----------------------------------------------------------------------------- 출력
def write_csv_md(path_base: Path, header: list[str], rows: list[list], title: str, note: str = ""):
    with open(str(path_base) + ".csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    lines = [f"### {title}", "", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for r in rows:
        lines.append("| " + " | ".join(str(x) for x in r) + " |")
    if note:
        lines += ["", note]
    (Path(str(path_base) + ".md")).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "\n".join(lines)


def setup_font():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.font_manager as fm
    from matplotlib import rcParams
    names = {f.name for f in fm.fontManager.ttflist}
    for cand in ("Pretendard", "Apple SD Gothic Neo", "NanumGothic", "AppleGothic"):
        if cand in names:
            rcParams["font.family"] = cand
            rcParams["axes.unicode_minus"] = False
            return cand
    return None


NAVY, NAVY2, LIGHT, RED, GREY = "#1F3A5F", "#4F6D8F", "#C9D3DF", "#C0392B", "#9AA7B6"


def draw_charts(out: Path, label: str, query: str, years: list[int], year_cnt: dict, kr_cnt: dict,
                countries: list, insts: list, total: int, fetched: str) -> tuple[list[str], list[str]]:
    """PNG 3장 생성. (파일 목록, 글꼴 경고 목록) 반환."""
    import matplotlib.pyplot as plt
    font = setup_font()
    caught: list[str] = []
    files: list[str] = []
    src = f"자료: OpenAlex API(조회 {fetched}) · 검색식 {query} · {years[0]}~{years[-1]} · 저자 소속 국가/기관은 중복 집계(한 논문이 여러 국가에 셀 수 있음)"

    with warnings.catch_warnings(record=True) as wl:
        warnings.simplefilter("always")

        # 1. 연도 추이 (전체 + 한국)
        fig, ax = plt.subplots(figsize=(8.3, 4.6), dpi=200)
        ys = [year_cnt.get(y, 0) for y in years]
        ax.bar(years, ys, color=NAVY2, width=0.7, label="전체")
        for x, v in zip(years, ys):
            ax.text(x, v, f"{v:,}", ha="center", va="bottom", fontsize=7, color=NAVY)
        ax2 = ax.twinx()
        ks = [pct(kr_cnt.get(y, 0), year_cnt.get(y, 0)) for y in years]
        ax2.plot(years, ks, color=RED, marker="o", lw=1.6, label="한국 참여 비율(%)")
        ax2.set_ylim(0, max(5, max(ks) * 1.4))
        ax2.set_ylabel("한국 참여 논문 비율(%)", color=RED)
        ax.set_ylabel("논문 수(건)")
        ax.set_xticks(years)
        ax.set_title(f"{label} — 연도별 논문 수와 한국 참여 비율", fontweight="bold", color=NAVY, loc="left")
        for s in ("top",):
            ax.spines[s].set_visible(False); ax2.spines[s].set_visible(False)
        h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, loc="upper left", frameon=False, fontsize=8)
        fig.text(0.01, 0.005, src, fontsize=6.5, color=GREY, ha="left")
        fig.tight_layout(rect=(0, 0.03, 1, 1))
        p = out / "그림1_연도추이.png"; fig.savefig(p); plt.close(fig); files.append(p.name)

        # 2. 국가 점유율 상위 10 (가로 막대)
        fig, ax = plt.subplots(figsize=(8.3, 4.8), dpi=200)
        top = countries[:10][::-1]
        names = [c["name"] for c in top]
        vals = [c["share_total"] for c in top]
        cols = [RED if c["code"] == KR else NAVY2 for c in top]
        ax.barh(names, vals, color=cols)
        for i, (v, c) in enumerate(zip(vals, top)):
            ax.text(v, i, f" {v}% ({c['count']:,})", va="center", fontsize=8, color=NAVY)
        ax.set_xlabel("논문 수 대비 비율(%) — 전체 " + f"{total:,}건")
        ax.set_xlim(0, max(vals) * 1.3 if vals else 1)
        ax.set_title(f"{label} — 국가별 참여 비율 상위 10 (HHI {hhi([c['count'] for c in countries])})",
                     fontweight="bold", color=NAVY, loc="left")
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        fig.text(0.01, 0.005, src, fontsize=6.5, color=GREY, ha="left")
        fig.tight_layout(rect=(0, 0.03, 1, 1))
        p = out / "그림2_국가점유율.png"; fig.savefig(p); plt.close(fig); files.append(p.name)

        # 3. 기관 상위 10
        fig, ax = plt.subplots(figsize=(8.3, 4.8), dpi=200)
        top = insts[:10][::-1]
        names = [f"{c['name']} ({c['country'] or '-'})" for c in top]
        vals = [c["count"] for c in top]
        cols = [RED if c["country"] == KR else NAVY2 for c in top]
        ax.barh(names, vals, color=cols)
        for i, v in enumerate(vals):
            ax.text(v, i, f" {v:,}", va="center", fontsize=8, color=NAVY)
        ax.set_xlabel("논문 수(건)")
        ax.set_xlim(0, max(vals) * 1.2 if vals else 1)
        ax.set_title(f"{label} — 기관별 논문 수 상위 10 (붉은색 = 한국 기관)", fontweight="bold", color=NAVY, loc="left")
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        fig.text(0.01, 0.005, src, fontsize=6.5, color=GREY, ha="left")
        fig.tight_layout(rect=(0, 0.03, 1, 1))
        p = out / "그림3_기관상위10.png"; fig.savefig(p); plt.close(fig); files.append(p.name)

        for w in wl:
            m = str(w.message)
            if "missing from font" in m or "Glyph" in m:
                caught.append(m)
    return files, caught, font


# ----------------------------------------------------------------------------- 본체
def selftest() -> int:
    """네트워크 없이 계산·파싱·표 출력만 검사한다."""
    import tempfile
    fails = []
    def chk(name, cond):
        print(("  ✓ " if cond else "  ✗ ") + name)
        if not cond: fails.append(name)
    chk("hhi 균등 4국 = 2500", hhi([1, 1, 1, 1]) == 2500.0)
    chk("hhi 독점 = 10000", hhi([7]) == 10000.0)
    chk("hhi 빈 목록 = 0", hhi([]) == 0.0)
    chk("pct 반올림", pct(1, 3) == 33.3 and pct(0, 0) == 0.0)
    chk("라벨/검색어 분리 + 구 검색", parse_keyword("AI 반도체 / AI semiconductor", None, False) == ("AI 반도체", '"AI semiconductor"'))
    chk("한 낱말은 따옴표 없음", parse_keyword("robotics", None, False) == ("robotics", "robotics"))
    chk("--loose", parse_keyword("humanoid robot", None, True)[1] == "humanoid robot")
    chk("불리언 검색식 보존", parse_keyword("x", '"a b" OR "c"', False)[1] == '"a b" OR "c"')
    chk("폴더명 안전화", safe_dirname('a/b:c*?"<>|') == "a_b_c_")
    with tempfile.TemporaryDirectory() as td:
        md = write_csv_md(Path(td) / "t", ["a", "b"], [[1, 2], [3, 4]], "표", "주")
        chk("csv+md 생성", (Path(td) / "t.csv").exists() and "| 1 | 2 |" in md and (Path(td) / "t.md").read_text(encoding="utf-8").endswith("주\n"))
    try:
        import matplotlib  # noqa
        f = setup_font()
        chk("한글 글꼴 탐지(Pretendard/Apple SD Gothic Neo/NanumGothic)", f is not None)
    except ImportError:
        print("  - matplotlib 없음: 그래프 검사 건너뜀")
    print(f"자체 검사 {('통과' if not fails else '실패 ' + str(len(fails)) + '건')}")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description="OpenAlex 논문 추이·국가/기관 집중도")
    ap.add_argument("keyword", nargs="*", help='"라벨 / 검색어" 또는 "검색어" (여러 개 가능)')
    ap.add_argument("--selftest", action="store_true", help="오프라인 자체 검사")
    ap.add_argument("--years", default="2015-2025", help="예: 2015-2025")
    ap.add_argument("--query", help="검색식 직접 지정(불리언 가능: \"A\" OR \"B\")")
    ap.add_argument("--field", default="title_and_abstract", choices=["title_and_abstract", "title", "default"],
                    help="title_and_abstract(기본)·title·default(제목+초록+본문 전체)")
    ap.add_argument("--loose", action="store_true", help="두 낱말 이상이어도 구 검색(따옴표)을 안 함")
    ap.add_argument("--quick", action="store_true", help="연도별 국가 순위(연도 수만큼 추가 호출)를 생략")
    ap.add_argument("--out", help="출력 폴더(기본: ./out/<라벨>; 키워드가 여럿이면 그 아래 라벨별)")
    ap.add_argument("--mailto", default=os.environ.get("OPENALEX_MAILTO", ""), help="polite pool용 이메일(선택)")
    ap.add_argument("--api-key", default=os.environ.get("OPENALEX_API_KEY", "") or read_key_file(),
                    help="OpenAlex API 키(선택). env OPENALEX_API_KEY 또는 ~/.config/sci-concentration/openalex.key")
    ap.add_argument("--no-chart", action="store_true")
    ap.add_argument("--patents", action="store_true", help="KIPRIS Plus 국내 특허(출원 추이·출원인 상위·상태)도 함께 — 키 필요")
    ap.add_argument("--patent-term", help="특허 검색어(국문, 기본: 라벨에 한글이 있으면 라벨)")
    ap.add_argument("--patent-field", default="title", choices=["title", "abstract", "word", "claim", "ipc"], help="특허 검색 필드(기본 title=발명의 명칭)")
    ap.add_argument("--max-pages", type=int, default=10, help="특허 출원인 집계용 레코드 페이지 상한(500건/페이지)")
    a = ap.parse_args()
    if a.selftest:
        rc = selftest()
        try:
            sys.path.insert(0, str(TOOL_DIR))  # python3 -I 는 스크립트 폴더를 sys.path에 넣지 않는다(3.11+ -P)
            import kipris_patents
            rc = max(rc, kipris_patents.selftest())
        except ImportError:
            print("  - kipris_patents.py 없음: 특허 검사 건너뜀")
        return rc
    if not a.keyword:
        ap.error("키워드를 하나 이상 주세요 (또는 --selftest)")
    if a.query and len(a.keyword) > 1:
        ap.error("--query 는 키워드가 하나일 때만 쓸 수 있습니다")
    rc = 0
    for kw in a.keyword:
        r = run_one(kw, a)
        rc = max(rc, r)
    return rc


def run_one(keyword: str, a) -> int:
    t_start = time.time()
    y0, y1 = [int(x) for x in a.years.split("-")]
    years = list(range(y0, y1 + 1))
    label, query = parse_keyword(keyword, a.query, a.loose)
    if a.out:
        out = Path(a.out) if len(a.keyword) == 1 else Path(a.out) / safe_dirname(label)
    else:
        out = Path.cwd() / "out" / safe_dirname(label)
    fetched = dt.date.today().isoformat()
    log: list = []
    oa = OpenAlex(a.mailto or None, a.api_key or None, log)

    field = {"title_and_abstract": "title_and_abstract.search", "title": "title.search", "default": "default.search"}[a.field]
    base = f"{field}:{query},publication_year:{y0}-{y1}"

    # 1) 연도 (첫 호출이 성공한 뒤에야 폴더를 만든다 — 실패 시 빈 폴더를 남기지 않음)
    d = oa.group(base, "publication_year")
    out.mkdir(parents=True, exist_ok=True); (out / "raw").mkdir(exist_ok=True)
    (out / "raw" / "by_year.json").write_text(json.dumps(d, ensure_ascii=False, indent=1))
    total = d["meta"]["count"]
    year_cnt = {int(g["key"]): g["count"] for g in d["group_by"]}
    # 2) 국가
    d = oa.group(base, "authorships.countries"); (out / "raw" / "by_country.json").write_text(json.dumps(d, ensure_ascii=False, indent=1))
    countries = [{"code": g["key"].rsplit("/", 1)[-1], "name": g["key_display_name"], "count": g["count"]} for g in d["group_by"]]
    csum = sum(c["count"] for c in countries)
    for i, c in enumerate(countries, 1):
        c["rank"] = i; c["share_total"] = pct(c["count"], total); c["share_sum"] = pct(c["count"], csum)
    # 3) 기관
    d = oa.group(base, "authorships.institutions.id"); (out / "raw" / "by_institution.json").write_text(json.dumps(d, ensure_ascii=False, indent=1))
    insts = [{"id": g["key"], "name": g["key_display_name"], "count": g["count"], "country": None} for g in d["group_by"]]
    # 기관 국가: 상위 10만 institutions API 로 보강 (1회, 필터 ids)
    if insts:
        ids = "|".join(x["id"].rsplit("/", 1)[-1] for x in insts[:10])
        try:
            params = {"filter": f"ids.openalex:{ids}", "per_page": 10, "select": "id,display_name,country_code"}
            if a.mailto: params["mailto"] = a.mailto
            if a.api_key: params["api_key"] = a.api_key
            url = "https://api.openalex.org/institutions?" + urllib.parse.urlencode(params, safe=':,|', quote_via=urllib.parse.quote)
            res = http_get_json(url)["results"]
            cc = {x["id"]: x.get("country_code") for x in res}
            for x in insts[:10]:
                x["country"] = cc.get(x["id"])
            log.append({"url": re.sub(r"&api_key=[^&]+", "&api_key=***", url), "count": len(res), "groups": None, "sec": 0, "oql": "institutions country_code"})
        except Exception as e:  # 보강 실패해도 본체는 진행
            print(f"※ 기관 국가 코드 보강 실패: {e}", file=sys.stderr)
    # 4) 한국 연도별
    d = oa.group(base + f",authorships.countries:{KR}", "publication_year"); (out / "raw" / "kr_by_year.json").write_text(json.dumps(d, ensure_ascii=False, indent=1))
    kr_total = d["meta"]["count"]
    kr_cnt = {int(g["key"]): g["count"] for g in d["group_by"]}
    # 5) 연도별 국가 순위 (선택)
    kr_rank = {}
    if not a.quick:
        for y in years:
            d = oa.group(f"{field}:{query},publication_year:{y}", "authorships.countries", {"per_page": 50})
            (out / "raw" / f"country_{y}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1))
            rk = {g["key"].rsplit("/", 1)[-1]: i for i, g in enumerate(d["group_by"], 1)}
            kr_rank[y] = rk.get(KR)

    # ---- 표
    t1 = write_csv_md(out / "표1_연도추이", ["연도", "논문 수", "한국 참여 논문 수", "한국 비율(%)〔계산〕"] + ([] if a.quick else ["한국 순위〔계산〕"]),
                      [[y, year_cnt.get(y, 0), kr_cnt.get(y, 0), pct(kr_cnt.get(y, 0), year_cnt.get(y, 0))] + ([] if a.quick else [kr_rank.get(y) or "-"]) for y in years],
                      f"표1. {label} 연도별 논문 수 · 한국 참여 비율", "한국 비율 = 한국 소속 저자가 1명 이상인 논문 수 ÷ 그 해 전체 논문 수.")
    t2 = write_csv_md(out / "표2_국가집중도", ["순위", "국가", "논문 수", "전체 대비(%)〔계산〕", "국가 합계 대비(%)〔계산〕"],
                      [[c["rank"], c["name"], c["count"], c["share_total"], c["share_sum"]] for c in countries[:10]],
                      f"표2. {label} 국가별 집중도 상위 10 (HHI〔계산〕 {hhi([c['count'] for c in countries])}, 집계 국가 {len(countries)}개)",
                      "전체 대비 = 국가 논문 수 ÷ 전체 논문 수(공저로 합이 100%를 넘음). 국가 합계 대비 = 국가 논문 수 ÷ 모든 국가 논문 수 합(HHI는 이 비율의 제곱합, 10,000 만점).")
    t3 = write_csv_md(out / "표3_기관상위10", ["순위", "기관", "국가", "논문 수", "전체 대비(%)〔계산〕"],
                      [[i, x["name"], x["country"] or "-", x["count"], pct(x["count"], total)] for i, x in enumerate(insts[:10], 1)],
                      f"표3. {label} 기관별 논문 수 상위 10 (집계 기관 {len(insts)}개 중)", "기관 국가는 OpenAlex institutions 의 country_code.")

    # ---- 그래프
    charts, font_warn, font = [], [], None
    if not a.no_chart:
        try:
            charts, font_warn, font = draw_charts(out, label, query, years, year_cnt, kr_cnt, countries, insts, total, fetched)
        except ImportError:
            print("※ matplotlib이 없어 그래프를 건너뜁니다 (pip install matplotlib).", file=sys.stderr)

    # ---- 호출 기록·요약
    kr_c = next((c for c in countries if c["code"] == KR), None)
    calls_md = "\n".join(f"{i}. `{l['url']}` — count {l['count']}, groups {l['groups']}, {l['sec']}s" for i, l in enumerate(log, 1))
    (out / "호출기록.md").write_text(f"# OpenAlex 호출 기록 (조회일 {fetched})\n\n{calls_md}\n\n검색식(OpenAlex 해석): `{log[0]['oql']}`\n", encoding="utf-8")
    peak_year = max(year_cnt, key=year_cnt.get) if year_cnt else None
    top3 = ", ".join(f"{c['name']} {c['share_total']}%" for c in countries[:3])
    elapsed = round(time.time() - t_start, 1)
    summary = f"""# {label} — 논문 추이·국가/기관 집중도 요약

- 데이터: OpenAlex 공개 API · 조회일 {fetched} · 검색식 `{query}` (필드 {a.field}) · 기간 {y0}~{y1}
- 전체 논문 수 **{total:,}건** 〔원문: 호출 1 meta.count〕 · 한국 소속 저자 참여 논문 **{kr_total:,}건** 〔원문: 호출 4〕 → 참여 비율 **{pct(kr_total, total)}%**〔계산〕
- 최다 연도 {peak_year}년 {year_cnt.get(peak_year, 0):,}건 〔원문〕 · 국가 상위 3: {top3} 〔계산〕 · 한국 순위 {kr_c['rank'] if kr_c else '-'}위 〔계산〕
- 국가 집중도 HHI {hhi([c['count'] for c in countries])} 〔계산〕 (1,500 미만 분산 · 2,500 초과 집중, 미국 DOJ 합병 기준 차용)
- 호출 {len(log)}회 · 소요 {elapsed}초 · 글꼴 {font or '미지정'} · 글자 깨짐 경고 {len(font_warn)}건

{t1}

{t2}

{t3}

## 그래프
{chr(10).join(f'- {c}' for c in charts) or '- (생략)'}

## 읽을 때 주의
- 〔원문〕은 API 응답 값 그대로, 〔계산〕은 이 도구가 나눈 값. 원 응답은 `raw/`에 있고 호출 URL·조회일은 `호출기록.md`.
- 검색식이 달라지면 건수가 크게 달라진다(구 검색 ↔ 낱말 검색, 제목·초록 ↔ 전문). 발표에 쓰기 전 검색식을 함께 적는다.
- "0건"은 "연구가 없다"가 아니라 그 검색식·수록 범위 밖일 수 있다. OpenAlex는 영문 서지 중심이며 국문 학술지 수록이 부분적이다.
- 국가·기관은 저자 소속 기준 중복 집계라 비율 합이 100%를 넘는다. 최근 연도는 수록 시차로 과소일 수 있다.
- 특허는 `--patents`(KIPRIS Plus 키 필요)로 국내 출원 추이·출원인 상위를 붙인다. 논문의 국가별 집중도에 해당하는 특허 국가별 집계는 제공하지 않는다(국내 공보 단위).

## 호출 기록
{calls_md}
"""
    pat_line = ""
    if a.patents:
        term = a.patent_term or (label if re.search(r"[가-힣]", label) else "")
        if not term:
            print("※ --patents: 국문 검색어가 없습니다. \"국문 라벨 / 영문 검색어\" 형태로 주거나 --patent-term 을 지정하세요. 특허는 건너뜁니다.", file=sys.stderr)
        else:
            sys.path.insert(0, str(TOOL_DIR))
            import kipris_patents
            r = kipris_patents.run(label, term, years, a.patent_field, out, a.max_pages, no_chart=a.no_chart)
            summary += "\n" + r["summary_md"]
            charts += r["charts"]; font_warn += r["font_warn"]
            pat_line = f" · 특허 {r['total']:,}건(표본 {r['coverage']}%, 호출 {r['calls']}회, {r['sec']}s)"
    (out / "요약.md").write_text(summary, encoding="utf-8")
    print(f"완료: {out}  (논문 {total:,}건 · 한국 {kr_total:,}건 {pct(kr_total,total)}% · 호출 {len(log)}회 · {elapsed}s · 글꼴 경고 {len(font_warn)}{pat_line})")
    for c in charts:
        print("  -", c)
    if font_warn:
        print("※ 글꼴 경고:", font_warn[:3], file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
