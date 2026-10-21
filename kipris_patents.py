#!/usr/bin/env python3
"""kipris_patents.py — KIPRIS Plus(지식재산처·한국특허정보원) 특허 출원 추이·출원인 집중도.

concentrate.py 의 --patents 가 부르는 모듈이며 단독 실행도 된다.
  python3 -I kipris_patents.py "자율주행" --years 2015-2025
  python3 -I kipris_patents.py "자율주행" --field abstract --max-pages 4

API: https://plus.kipris.or.kr/kipo-api/kipi/patUtiModInfoSearchSevice/getAdvancedSearch ('Sevice'는 실제 경로)
키: env KIPRIS_API_KEY → ~/.config/sci-concentration/kipris.key → ~/.kipris/key (파라미터명 ServiceKey)
무료 한도: 월 1,000회(매월 1일 초기화), 초당 50회 미만. 이 모듈은 호출 수를 ~/.kipris/usage_YYYYMM 에 자체 집계한다(참고용).

호출 수 = 연도 수(연도별 totalCount, numOfRows=1) + 레코드 페이지 수(numOfRows=500, --max-pages 상한).
표준 라이브러리만. 그래프는 concentrate.py 쪽 matplotlib 설정을 재사용한다.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

BASE = "https://plus.kipris.or.kr/kipo-api/kipi/patUtiModInfoSearchSevice/getAdvancedSearch"
UA = "sci-concentration/0.2 (kipris module)"
FIELDS = {"title": "inventionTitle", "abstract": "astrtCont", "word": "word", "claim": "claimScope", "ipc": "ipcNumber"}
MONTHLY_QUOTA = 1000
PAGE = 500
# 명칭 기반 해외 출원인 추정 — 보수적 목록(없으면 '국내 또는 미상'). 결과에는 반드시 〔추정〕을 단다.
FOREIGN_HINTS = ("인코포레이티드", "인코퍼레이티드", "리미티드", "게엠베하", "코포레이션", "코퍼레이션", "컴퍼니", "아게",
                 "에스에이", "엘엘씨", "엘티디", "가부시키가이샤", "가부시기가이샤", "가부시끼가이샤", "엔브이", "비브이",
                 "피티이", "에스피에이", "오와이", "에이비", "에스에이에스", "피엘씨", "홀딩 비.브이", "카부시키")


# ----------------------------------------------------------------------------- 키·호출
def read_key() -> str:
    k = (os.environ.get("KIPRIS_API_KEY") or "").strip()
    if k:
        return k
    for p in (Path.home() / ".config" / "sci-concentration" / "kipris.key", Path.home() / ".kipris" / "key"):
        try:
            t = p.read_text(encoding="utf-8").strip().splitlines()
            if t and t[0].strip():
                return t[0].strip()
        except Exception:
            pass
    return ""


def bump_usage() -> int | None:
    try:
        d = Path.home() / ".kipris"; d.mkdir(parents=True, exist_ok=True)
        p = d / f"usage_{dt.datetime.now():%Y%m}"
        n = int(p.read_text().strip()) + 1 if p.exists() else 1
        p.write_text(str(n)); return n
    except Exception:
        return None


def _ssl_ctx():
    ctx = ssl.create_default_context()
    try:
        if ctx.cert_store_stats().get("x509_ca", 0) == 0:
            import certifi  # type: ignore
            ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


def call(params: dict, key: str, log: list) -> ET.Element:
    q = {k: v for k, v in params.items() if v not in (None, "")}
    q["ServiceKey"] = key
    url = BASE + "?" + urllib.parse.urlencode(q, safe="~")
    shown = BASE + "?" + urllib.parse.urlencode({k: v for k, v in q.items() if k != "ServiceKey"}, safe="~") + "&ServiceKey=***"
    t0 = time.time()
    raw = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60, context=_ssl_ctx()) as r:
                raw = r.read().decode("utf-8", "replace")
            break
        except urllib.error.URLError as e:
            if attempt == 2:
                try:
                    import subprocess
                    raw = subprocess.run(["curl", "-sS", "-m", "60", "-A", UA, url], capture_output=True, text=True, check=True).stdout
                except Exception as e2:
                    raise SystemExit(f"KIPRIS 접속 실패: {e} / curl: {e2}\n  URL: {shown}")
            else:
                time.sleep(1.0)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise SystemExit(f"KIPRIS 응답이 XML이 아님(키·파라미터 오류 가능): {raw[:200]}\n  URL: {shown}")
    ok, code, msg = root.findtext(".//successYN"), root.findtext(".//resultCode"), root.findtext(".//resultMsg")
    used = bump_usage()
    log.append({"url": shown, "total": root.findtext(".//totalCount"), "sec": round(time.time() - t0, 2), "code": code})
    if ok != "Y":
        hint = ""
        if code in ("101", "02", "2"):
            hint = "\n  → 이 API(특허·실용 공개·등록공보)를 KIPRIS Plus에서 '활용 신청'했는지, 키가 맞는지 확인하세요."
        raise SystemExit(f"KIPRIS 오류 successYN={ok} resultCode={code}: {msg}{hint}\n  URL: {shown}")
    if used and used >= MONTHLY_QUOTA * 0.8:
        print(f"※ 이번 달 자체 집계 호출 {used}회 — 무료 한도 {MONTHLY_QUOTA}회 임박", file=sys.stderr)
    return root


def items(root: ET.Element) -> list[dict]:
    return [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("item")]


# ----------------------------------------------------------------------------- 집계
def is_foreign(name: str) -> bool:
    return any(h in name for h in FOREIGN_HINTS)


def aggregate(records: list[dict], years: list[int]) -> dict:
    """레코드(출원 단위) → 출원인 상위·상태 분포·출원인 유형 추정. 공동출원('|')은 출원인마다 1건씩 센다."""
    app = Counter(); status = Counter(); ftype = Counter(); yr_in_sample = Counter()
    for r in records:
        names = [n.strip() for n in (r.get("applicantName") or "").split("|") if n.strip()]
        for n in names:
            app[n] += 1
        first = names[0] if names else ""
        ftype["해외〔추정〕" if (first and is_foreign(first)) else "국내 또는 미상"] += 1
        status[r.get("registerStatus") or "미상"] += 1
        y = (r.get("applicationDate") or "")[:4]
        if y.isdigit():
            yr_in_sample[int(y)] += 1
    return {"applicants": app.most_common(), "status": status.most_common(), "ftype": ftype.most_common(), "sample_years": yr_in_sample}


def write_csv_md(base: Path, header, rows, title, note=""):
    with open(str(base) + ".csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(header); w.writerows(rows)
    lines = [f"### {title}", "", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    if note:
        lines += ["", note]
    Path(str(base) + ".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "\n".join(lines)


def pct(a, b):
    return round(a / b * 100, 1) if b else 0.0


# ----------------------------------------------------------------------------- 그래프
def draw(out: Path, label: str, term: str, field: str, years, year_cnt, agg, n_rec, total, fetched):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm
        from matplotlib import rcParams
    except ImportError:
        return [], []
    names = {f.name for f in fm.fontManager.ttflist}
    for c in ("Pretendard", "Apple SD Gothic Neo", "NanumGothic", "AppleGothic"):
        if c in names:
            rcParams["font.family"] = c; break
    rcParams["axes.unicode_minus"] = False
    NAVY, NAVY2, RED, GREY, LIGHT = "#1F3A5F", "#4F6D8F", "#C0392B", "#9AA7B6", "#C9D3DF"
    src = f"자료: KIPRIS Plus 특허·실용 공개·등록공보 API(조회 {fetched}) · 검색 {FIELDS[field]}=\"{term}\" · 국내 특허 출원(실용신안 제외) · 출원연도 기준"
    blind_from = dt.date.today().year - 1  # 출원 후 18개월 공개 → 최근 약 2개 연도는 과소
    files = []
    import warnings
    with warnings.catch_warnings(record=True) as wl:
        warnings.simplefilter("always")
        fig, ax = plt.subplots(figsize=(8.3, 4.4), dpi=200)
        ys = [year_cnt.get(y, 0) for y in years]
        cols = [LIGHT if y >= blind_from else NAVY2 for y in years]
        ax.bar(years, ys, color=cols, width=0.7)
        for x, v in zip(years, ys):
            ax.text(x, v, f"{v:,}", ha="center", va="bottom", fontsize=7, color=NAVY)
        ax.set_xticks(years); ax.set_ylabel("출원 건수(건)")
        ax.set_title(f"{label} — 연도별 국내 특허 출원 건수 (연한 막대 = 출원공개 18개월 미반영 구간)", fontweight="bold", color=NAVY, loc="left", fontsize=10)
        for s in ("top", "right"): ax.spines[s].set_visible(False)
        fig.text(0.01, 0.005, src, fontsize=6.3, color=GREY)
        fig.tight_layout(rect=(0, 0.03, 1, 1)); p = out / "그림4_특허_연도추이.png"; fig.savefig(p); plt.close(fig); files.append(p.name)

        fig, ax = plt.subplots(figsize=(8.3, 4.8), dpi=200)
        top = agg["applicants"][:10][::-1]
        nm = [n if len(n) <= 22 else n[:21] + "…" for n, _ in top]; vals = [c for _, c in top]
        cols = [GREY if is_foreign(n) else NAVY2 for n, _ in top]
        ax.barh(nm, vals, color=cols)
        for i, v in enumerate(vals):
            ax.text(v, i, f" {v:,}", va="center", fontsize=8, color=NAVY)
        ax.set_xlabel(f"출원 건수(건) — 표본 {n_rec:,}건 / 전체 {total:,}건 (회색 = 명칭 기반 해외 추정)")
        ax.set_xlim(0, max(vals) * 1.2 if vals else 1)
        ax.set_title(f"{label} — 출원인 상위 10 (명칭 그대로, 미정제)", fontweight="bold", color=NAVY, loc="left")
        for s in ("top", "right"): ax.spines[s].set_visible(False)
        fig.text(0.01, 0.005, src, fontsize=6.3, color=GREY)
        fig.tight_layout(rect=(0, 0.03, 1, 1)); p = out / "그림5_특허_출원인상위10.png"; fig.savefig(p); plt.close(fig); files.append(p.name)
        warn = [str(w.message) for w in wl if "missing from font" in str(w.message) or "Glyph" in str(w.message)]
    return files, warn


# ----------------------------------------------------------------------------- 본체
def run(label: str, term: str, years: list[int], field: str, out: Path, max_pages: int, key: str | None = None,
        no_chart: bool = False) -> dict:
    key = key or read_key()
    if not key:
        raise SystemExit("KIPRIS Plus 키가 없습니다. env KIPRIS_API_KEY 또는 ~/.config/sci-concentration/kipris.key 에 넣으세요 "
                         "(발급: https://plus.kipris.or.kr 회원가입 → 특허·실용 공개·등록공보 API 활용 신청, 무료 월 1,000회).")
    fld = FIELDS[field]
    log: list = []
    fetched = dt.date.today().isoformat()
    t0 = time.time()
    y0, y1 = years[0], years[-1]
    # 1) 연도별 totalCount
    year_cnt = {}
    for y in years:
        r = call({fld: term, "applicationDate": f"{y}0101~{y}1231", "patent": "true", "utility": "false", "numOfRows": 1, "pageNo": 1}, key, log)
        year_cnt[y] = int(r.findtext(".//totalCount") or 0)
    total = sum(year_cnt.values())
    out.mkdir(parents=True, exist_ok=True); (out / "raw").mkdir(exist_ok=True)
    # 2) 레코드 페이징(출원일 내림차순) — 출원인·상태 집계용 표본
    records = []
    pages = min(max_pages, -(-total // PAGE)) if total else 0
    for pno in range(1, pages + 1):
        r = call({fld: term, "applicationDate": f"{y0}0101~{y1}1231", "patent": "true", "utility": "false",
                  "numOfRows": PAGE, "pageNo": pno, "sortSpec": "AD", "descSort": "true"}, key, log)
        recs = items(r)
        (out / "raw" / f"kipris_page{pno}.json").write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
        records += recs
        if len(recs) < PAGE:
            break
    agg = aggregate(records, years)
    n_rec = len(records)
    coverage = pct(n_rec, total)
    # ---- 표
    t4 = write_csv_md(out / "표4_특허_연도추이", ["출원연도", "출원 건수〔원문 totalCount〕", "비고"],
                      [[y, year_cnt[y], "출원공개 18개월 미반영(과소)" if y >= dt.date.today().year - 1 else ""] for y in years],
                      f"표4. {label} 연도별 국내 특허 출원 건수 (KIPRIS Plus, {fld}=\"{term}\")",
                      "출원일 기준. 출원 후 1년 6개월이 지나야 공개되므로(특허법 제64조) 최근 약 2개 연도는 구조적으로 과소 — 감소로 읽지 않는다.")
    t5 = write_csv_md(out / "표5_특허_출원인상위10", ["순위", "출원인(명칭 그대로)", "출원 건수", "표본 대비(%)〔계산〕", "유형〔추정〕"],
                      [[i, n, c, pct(c, n_rec), "해외" if is_foreign(n) else "국내·미상"] for i, (n, c) in enumerate(agg["applicants"][:10], 1)],
                      f"표5. {label} 출원인 상위 10 (표본 {n_rec:,}건 = 전체 {total:,}건의 {coverage}%, 출원일 내림차순 상위)",
                      "출원인명은 KIPRIS 표기 그대로(계열사·표기 변형 미정제). 공동출원은 출원인마다 1건씩 셈. '해외'는 명칭 접미(인코포레이티드·리미티드·게엠베하 등) 기반 추정이며 국적 자료가 아님.")
    t6 = write_csv_md(out / "표6_특허_상태분포", ["상태", "건수", "표본 대비(%)〔계산〕"],
                      [[s, c, pct(c, n_rec)] for s, c in agg["status"]],
                      f"표6. {label} 출원 상태 분포 (표본 {n_rec:,}건)", "등록 ≠ 유효(연차료 미납 소멸 포함 가능).")
    charts, warn = ([], []) if no_chart else draw(out, label, term, field, years, year_cnt, agg, n_rec, total, fetched)
    calls_md = "\n".join(f"{i}. `{l['url']}` — totalCount {l['total']}, {l['sec']}s" for i, l in enumerate(log, 1))
    (out / "호출기록_특허.md").write_text(f"# KIPRIS Plus 호출 기록 (조회일 {fetched})\n\n{calls_md}\n", encoding="utf-8")
    peak = max(year_cnt, key=year_cnt.get) if year_cnt else None
    top3 = ", ".join(f"{n} {c}건" for n, c in agg["applicants"][:3])
    ft = {k: v for k, v in agg["ftype"]}
    summary = f"""## 특허 (KIPRIS Plus — 국내 특허 출원)

- 데이터: KIPRIS Plus 특허·실용 공개·등록공보 API(지식재산처·한국특허정보원) · 조회일 {fetched} · 검색 `{fld}="{term}"` · 출원일 {y0}~{y1} · 특허만(실용신안 제외)
- 출원 건수 합계 **{total:,}건** 〔원문: 연도별 totalCount 합〕 · 최다 연도 {peak}년 {year_cnt.get(peak, 0):,}건 〔원문〕
- 출원인 상위 3: {top3} 〔계산, 표본 {n_rec:,}건〕 · 해외〔추정〕 비중 {pct(ft.get('해외〔추정〕', 0), n_rec)}% 〔명칭 기반 추정〕
- 호출 {len(log)}회(연도 {len(years)} + 페이지 {pages}) · 소요 {round(time.time() - t0, 1)}초 · 글자 깨짐 경고 {len(warn)}건

{t4}

{t5}

{t6}

### 그래프
{chr(10).join('- ' + c for c in charts) or '- (생략)'}

### 읽을 때 주의
- **출원공개 18개월 블라인드**: 최근 약 2개 연도(연한 막대)는 아직 공개되지 않은 출원이 빠져 있다. 감소 추세로 읽지 않는다.
- 국내 공보(KR 출원)만 센다 — 국제 비교·패밀리 단위 집계가 아니다. 국가별 집중도는 논문(OpenAlex)과 달리 특허에서는 제공하지 않는다(출원인 국적 자료 없음).
- 출원인명 미정제(삼성전자 vs 삼성전자주식회사, 계열사). 상위 10은 명칭 그대로다.
- 표본은 출원일 내림차순 상위 {n_rec:,}건({coverage}%) — 전체가 아니면 `--max-pages`를 올린다(페이지당 호출 1회·500건, 월 1,000회 한도).
- 건수는 혁신의 질이 아니다(방어출원·덤불). 등록 ≠ 유효.
"""
    (out / "요약_특허.md").write_text(summary, encoding="utf-8")
    return {"total": total, "year_cnt": year_cnt, "n_rec": n_rec, "coverage": coverage, "calls": len(log), "charts": charts,
            "font_warn": warn, "summary_md": summary, "sec": round(time.time() - t0, 1)}


# ----------------------------------------------------------------------------- 자체 검사(오프라인)
FIXTURE_XML = """<?xml version="1.0" encoding="UTF-8"?><response><header><successYN>Y</successYN><resultCode>00</resultCode><resultMsg>NORMAL SERVICE.</resultMsg></header>
<body><items><item><applicantName>현대자동차주식회사|기아 주식회사</applicantName><applicationDate>20201231</applicationDate><applicationNumber>1020200000001</applicationNumber><inventionTitle>자율주행 차량 제어</inventionTitle><registerStatus>공개</registerStatus></item>
<item><applicantName>퀄컴 인코포레이티드</applicantName><applicationDate>20200310</applicationDate><applicationNumber>1020200000002</applicationNumber><inventionTitle>자율주행 센서</inventionTitle><registerStatus>등록</registerStatus></item>
<item><applicantName>현대자동차주식회사</applicantName><applicationDate>20190101</applicationDate><applicationNumber>1020190000003</applicationNumber><inventionTitle>자율주행 경로</inventionTitle><registerStatus>등록</registerStatus></item></items>
<count><numOfRows>500</numOfRows><pageNo>1</pageNo><totalCount>3</totalCount></count></body></response>"""


def selftest() -> int:
    fails = []
    def chk(name, cond):
        print(("  ✓ " if cond else "  ✗ ") + name)
        if not cond: fails.append(name)
    root = ET.fromstring(FIXTURE_XML)
    recs = items(root)
    chk("XML items 파싱 3건", len(recs) == 3 and recs[0]["applicantName"].startswith("현대자동차"))
    chk("totalCount 읽기", root.findtext(".//totalCount") == "3")
    agg = aggregate(recs, [2019, 2020])
    chk("공동출원 분리 집계(현대 2·기아 1·퀄컴 1)", dict(agg["applicants"]) == {"현대자동차주식회사": 2, "기아 주식회사": 1, "퀄컴 인코포레이티드": 1})
    chk("해외 추정(인코포레이티드)", is_foreign("퀄컴 인코포레이티드") and not is_foreign("현대자동차주식회사"))
    chk("상태 분포", dict(agg["status"]) == {"등록": 2, "공개": 1})
    chk("표본 연도", agg["sample_years"][2020] == 2)
    chk("날짜 범위 파라미터 형식", urllib.parse.urlencode({"applicationDate": "20200101~20201231"}, safe="~") == "applicationDate=20200101~20201231")
    print("특허 모듈 자체 검사 " + ("통과" if not fails else f"실패 {len(fails)}건"))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description="KIPRIS Plus 특허 출원 추이·출원인 집중도")
    ap.add_argument("term", nargs="?", help="검색어(국문, 예: 자율주행)")
    ap.add_argument("--label", help="라벨(기본: 검색어)")
    ap.add_argument("--years", default="2015-2025")
    ap.add_argument("--field", default="title", choices=list(FIELDS), help="title=발명의 명칭(기본) · abstract=초록 · word=자유검색 · claim=청구범위 · ipc=IPC")
    ap.add_argument("--max-pages", type=int, default=10, help="출원인 집계용 레코드 페이지 상한(500건/페이지, 기본 10)")
    ap.add_argument("--out")
    ap.add_argument("--no-chart", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.term or not a.term.strip():
        ap.error("검색어가 필요합니다")
    try:
        y0, y1 = (int(x) for x in a.years.split("-"))
        if not 1000 <= y0 <= y1 <= 9999:
            raise ValueError
    except ValueError:
        ap.error("--years 는 시작연도-종료연도 형식이며 시작연도가 더 클 수 없습니다")
    if a.max_pages < 1:
        ap.error("--max-pages 는 1 이상이어야 합니다")
    label = (a.label or a.term).strip()
    if label in {".", "..", ""}:
        ap.error("라벨은 일반 폴더명이어야 합니다")
    out = Path(a.out) if a.out else Path.cwd() / "out" / re.sub(r"[\\/:*?\"<>|]+", "_", label)
    r = run(label, a.term, list(range(y0, y1 + 1)), a.field, out, a.max_pages, no_chart=a.no_chart)
    print(f"완료: {out}  (출원 {r['total']:,}건 · 표본 {r['n_rec']:,}건 {r['coverage']}% · 호출 {r['calls']}회 · {r['sec']}s · 글꼴 경고 {len(r['font_warn'])})")
    return 3 if r["font_warn"] else 0


if __name__ == "__main__":
    sys.exit(main())
