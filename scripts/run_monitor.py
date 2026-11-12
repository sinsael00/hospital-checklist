# -*- coding: utf-8 -*-
import os
import sys
import json
import urllib.request
import urllib.parse
import re
from datetime import datetime, timezone, timedelta
import calendar

KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
RECORDS_FILE = os.path.join(DATA_DIR, "records.json")

def slot_key(slot):
    """슬롯 표기(AM/PM/오전 09:00/깨진 인코딩 포함)를 AM/PM으로 정규화"""
    s = slot or ""
    if "09:00" in s or "AM" in s or "오전" in s:
        return "AM"
    return "PM"

def check_youtube_autocomplete(query, target):
    url = f"https://suggestqueries.google.com/complete/search?client=youtube&ds=yt&hl=ko&gl=KR&q={urllib.parse.quote(query)}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as res:
            raw = res.read().decode("utf-8", errors="ignore")
        match = re.search(r'window\.google\.ac\.h\((.*)\)', raw)
        if match:
            data = json.loads(match.group(1))
            suggestions = [item[0] for item in data[1]] if len(data) > 1 else []
        else:
            suggestions = []
    except Exception as e:
        print(f"Error fetching [{query}]: {e}")
        suggestions = []

    clean_target = target.replace(" ", "").lower()
    is_exposed = False
    rank = None
    matched_text = ""

    for i, s in enumerate(suggestions[:10], 1):
        clean_s = s.replace(" ", "").lower()
        if clean_target in clean_s:
            rank = i
            matched_text = s
            # 10위 밖(10위 이상)은 미노출 처리 (1~9위만 유효 노출)
            if rank < 10:
                is_exposed = True
            else:
                is_exposed = False
            break

    return {
        "is_exposed": is_exposed,
        "rank": rank,
        "matched_text": matched_text,
        "suggestions": suggestions[:10]
    }

def get_monthly_matrix(year, month, keywords, records, carry_over=True):
    month_records = [r for r in records if r["date"].startswith(f"{year}-{month:02d}")]
    _, num_days = calendar.monthrange(year, month)

    organized = {}
    for r in month_records:
        d = r["date"]
        kid = r["keyword_id"]
        slot = r["slot"]
        if d not in organized:
            organized[d] = {}
        if kid not in organized[d]:
            organized[d][kid] = {"AM": None, "PM": None, "LAST": None}

        if "오전" in slot or "09:00" in slot or "AM" in slot:
            organized[d][kid]["AM"] = r
        elif "오후" in slot or "18:00" in slot or "PM" in slot:
            organized[d][kid]["PM"] = r
        organized[d][kid]["LAST"] = r

    consecutive_counts = {kw["id"]: 0.0 for kw in keywords}
    if carry_over:
        prev_y = year - 1 if month == 1 else year
        prev_m = 12 if month == 1 else month - 1
        try:
            do_carry = True if (prev_y > 2026 or (prev_y == 2026 and prev_m > 9)) else False
            prev_matrix = get_monthly_matrix(prev_y, prev_m, keywords, records, carry_over=do_carry)
            if prev_matrix:
                last_res = prev_matrix[-1]["results"]
                for kw in keywords:
                    kid = kw["id"]
                    cnt = last_res.get(kid, {}).get("consecutive_count", 0.0)
                    if cnt and float(cnt) < 30.0:
                        consecutive_counts[kid] = float(cnt)
        except Exception:
            pass

    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"]
    days_data = []

    for day in range(1, num_days + 1):
        d_str = f"{year}-{month:02d}-{day:02d}"
        w_idx = datetime(year, month, day).weekday()
        w_kr = weekday_kr[w_idx]

        kw_results = {}
        for kw in keywords:
            kid = kw["id"]
            slot_info = organized.get(d_str, {}).get(kid, {"AM": None, "PM": None, "LAST": None})
            am_rec = slot_info["AM"]
            pm_rec = slot_info["PM"]
            last_rec = slot_info["LAST"]

            # 10위 밖(10위 이상)은 미노출 처리
            am_exp = bool(am_rec and am_rec.get("is_exposed") and (am_rec.get("rank") is None or am_rec.get("rank") < 10))
            pm_exp = bool(pm_rec and pm_rec.get("is_exposed") and (pm_rec.get("rank") is None or pm_rec.get("rank") < 10))

            if am_exp and pm_exp:
                credit = 1.0
            elif am_exp or pm_exp:
                credit = 0.5
            else:
                credit = 0.0

            is_exp = (credit > 0)
            best_rank = None
            for check in [am_rec, pm_rec, last_rec]:
                if check and check.get("is_exposed") and check.get("rank"):
                    r = check.get("rank")
                    if best_rank is None or r < best_rank:
                        best_rank = r

            prev = consecutive_counts[kid]
            if is_exp:
                if prev >= 30.0:
                    prev = 0.0
                consecutive_counts[kid] = round(prev + credit, 1)
            cnt = consecutive_counts[kid]

            kw_results[kid] = {
                "am": am_rec,
                "pm": pm_rec,
                "is_exposed": is_exp,
                "day_credit": credit,
                "best_rank": best_rank,
                "consecutive_count": cnt,
                "is_30_reached": (cnt == 30.0 or (cnt == 0.0 and prev >= 29.5))
            }

        days_data.append({
            "day": day,
            "date": d_str,
            "weekday": w_kr,
            "is_weekend": (w_kr in ["토", "일"]),
            "results": kw_results
        })

    return days_data

def render_html(year, month, hosp_slug, settings, records):
    now_kst = datetime.now(KST)
    today_str = now_kst.strftime("%Y-%m-%d")

    hospitals = settings.get("hospitals", {})
    h_info = hospitals.get(hosp_slug, {})
    h_name = h_info.get("name", hosp_slug)

    all_kws = settings.get("keywords", [])
    keywords = [k for k in all_kws if k.get("hospital") == hosp_slug and k.get("active", True)]

    matrix = get_monthly_matrix(year, month, keywords, records)
    last_row = matrix[-1] if matrix else None
    today_row = None
    for r in matrix:
        if r["date"] == today_str:
            today_row = r
            break

    target_summary_row = today_row if today_row else last_row

    completed_30_dict = {}
    completed_30_kws = []
    for kw in keywords:
        kid = kw["id"]
        for r in matrix:
            if r["results"].get(kid, {}).get("is_30_reached"):
                date_label = r["date"][5:]
                entry = f"{kw['display_name']} ({date_label})"
                if entry not in completed_30_kws:
                    completed_30_kws.append(entry)
                completed_30_dict[kid] = date_label

    # Summary row
    total_summary_tds = '<td><strong>30일 보장 현황</strong></td><td>-</td>'
    for kw in keywords:
        kid = kw["id"]
        cnt = target_summary_row["results"].get(kid, {}).get("consecutive_count", 0) if target_summary_row else 0
        rem = max(0, 30 - int(cnt))
        if kid in completed_30_dict:
            reach_date = completed_30_dict[kid]
            summary_text = f'🎉 <strong>30일 노출 보장 완주! ({reach_date} 달성)</strong><br><span style="font-size:12px;color:#059669;font-weight:700;">다음 사이클 누적: {cnt}일차</span>'
            summary_cls = 'cell-summary-goal'
        elif cnt == 30:
            summary_text = '🎉 <strong>30일 노출 보장 완료!</strong>'
            summary_cls = 'cell-summary-goal'
        else:
            summary_text = f'현재 <strong>{cnt}일</strong> / 30일 보장 (잔여 <strong>{rem}일</strong>)'
            summary_cls = 'cell-summary'
        total_summary_tds += f'<td colspan="4" class="{summary_cls}">{summary_text}</td>'

    # Cards
    is_past_month = (today_row is None and last_row is not None)
    detail_cards = ""
    for kw in keywords:
        kid = kw["id"]
        kr = target_summary_row["results"].get(kid, {}) if target_summary_row else {}
        am_rec = kr.get("am")
        pm_rec = kr.get("pm")

        am_badge = '<span class="slot-badge ok">✔ 노출 정상</span>' if (am_rec and am_rec.get("is_exposed")) else ('<span class="slot-badge fail">✘ 미노출</span>' if am_rec else '<span class="slot-badge wait">점검 대기</span>')
        pm_badge = '<span class="slot-badge ok">✔ 노출 정상</span>' if (pm_rec and pm_rec.get("is_exposed")) else ('<span class="slot-badge fail">✘ 미노출</span>' if pm_rec else '<span class="slot-badge wait">점검 대기</span>')

        cnt = kr.get("consecutive_count", 0)
        rem = max(0, 30 - int(cnt))
        if kid in completed_30_dict:
            reach_date = completed_30_dict[kid]
            cnt_badge = f'<span class="badge-contract-day goal" style="background:linear-gradient(135deg, #059669, #047857);color:#ffffff;">🎉 30일 완주! ({reach_date} 달성)</span>'
        elif cnt == 30:
            cnt_badge = '<span class="badge-contract-day goal">🎉 30일 보장 완료!</span>'
        else:
            cnt_badge = f'<span class="badge-contract-day">보장 {cnt}일차 (잔여 {rem}일)</span>'

        slot1_label = f"마지막 오전 ({target_summary_row['date'][5:]})" if is_past_month else "🌅 오전 09:00 점검"
        slot2_label = f"마지막 오후 ({target_summary_row['date'][5:]})" if is_past_month else "🌇 오후 18:00 점검"

        detail_cards += f'''
        <div class="today-card">
            <div class="today-card-top">
                <div>
                    <span class="today-kw-query">검색어: {kw["query"]}</span>
                    <h3 class="today-kw-title">{kw["display_name"]}</h3>
                </div>
                {cnt_badge}
            </div>
            <div class="today-card-slots">
                <div class="slot-item">
                    <span class="slot-label">{slot1_label}</span>
                    {am_badge}
                </div>
                <div class="slot-item">
                    <span class="slot-label">{slot2_label}</span>
                    {pm_badge}
                </div>
            </div>
        </div>
        '''

    # Table rows
    table_rows = ""
    for r in matrix:
        d_num = r["day"]
        d_str = r["date"]
        w_kr = r["weekday"]
        is_today = (d_str == today_str)

        row_cls = ["today-row"] if is_today else (["weekend-row"] if r["is_weekend"] else [])
        row_cls_str = f' class="{" ".join(row_cls)}"' if row_cls else ""
        w_style = ' style="color: #2563EB;"' if w_kr == "토" else (' style="color: #DC2626;"' if w_kr == "일" else "")

        td_cells = f'<td><strong>{month:02d}/{d_num:02d}</strong></td><td{w_style}><strong>{w_kr}</strong></td>'

        for kw in keywords:
            kid = kw["id"]
            kr = r["results"].get(kid, {})
            is_exp = kr.get("is_exposed", False)
            cnt = kr.get("consecutive_count", 0)
            is_30 = kr.get("is_30_reached", False)
            am_rec = kr.get("am")
            pm_rec = kr.get("pm")
            credit = kr.get("day_credit", 0.0)

            if d_str > today_str:
                td_cells += '<td class="cell-future">-</td><td class="cell-future">-</td><td class="cell-future">-</td><td class="cell-future">-</td>'
            else:
                am_html = '<td><span class="badge-ok">✔ 노출</span></td>' if (am_rec and am_rec.get("is_exposed")) else '<td><span class="badge-fail">x</span></td>'
                pm_html = '<td><span class="badge-ok">✔ 노출</span></td>' if (pm_rec and pm_rec.get("is_exposed")) else '<td><span class="badge-fail">x</span></td>'

                if credit == 1.0:
                    judge_html = '<td><span class="badge-judge-ok">★ 1일 노출</span></td>'
                elif credit == 0.5:
                    judge_html = '<td><span class="badge-judge-half">◑ 0.5일 노출</span></td>'
                elif is_exp:
                    judge_html = '<td><span class="badge-judge-ok">★ 노출</span></td>'
                else:
                    judge_html = '<td><span class="badge-judge-fail">미노출</span></td>'

                if is_exp:
                    if is_30 or cnt == 30:
                        day_html = '<td class="cell-goal-reached" title="30일 보장 만기 달성!"><strong>30일차 🎉</strong></td>'
                    else:
                        day_html = f'<td class="cell-yellow-badge"><strong>{cnt}일차</strong></td>'
                else:
                    day_html = '<td class="cell-unexposed">x</td>'

                td_cells += f'{am_html}{pm_html}{judge_html}{day_html}'

        table_rows += f'<tr{row_cls_str}>{td_cells}</tr>\n'

    th_keywords_row1 = "".join([f'<th colspan="4" class="kw-th">🔍 {kw["display_name"]} ({kw["query"]})</th>\n' for kw in keywords])
    th_keywords_row2 = "".join(['<th class="sub-th">오전 09:00</th><th class="sub-th">오후 18:00</th><th class="sub-th">당일 인정</th><th class="sub-th">보장 일차</th>\n' for kw in keywords])

    banner_html = ""
    if completed_30_kws:
        kws_str = ", ".join(completed_30_kws)
        banner_html = f'''
        <div class="goal-banner">
            <div class="goal-banner-icon">🎉</div>
            <div class="goal-banner-text">
                <div class="goal-banner-title">30일 노출 보장 만기 달성 안내!</div>
                <div class="goal-banner-desc">당월 30일 노출 보장이 완료된 키워드: <strong>{kws_str}</strong> (30일 채워지면 알림 및 새 주기 리사이클)</div>
            </div>
        </div>
        '''

    month_tab_bar = f'''<div class="month-tab-bar">
        <span class="month-tab-label">📅 점검 월 선택:</span>
        <a href="nov.html" class="month-tab {"active" if month == 11 else ""}">2026년 11월 (진행중)</a>
        <a href="oct.html" class="month-tab {"active" if month == 10 else ""}">2026년 10월 내역 (보장 달성)</a>
        <a href="sep.html" class="month-tab {"active" if month == 9 else ""}">2026년 9월 내역 (완료)</a>
    </div>'''

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{h_name} 유튜브 시크릿 자동완성 실시간 모니터링 관제센터</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Pretendard", "Apple SD Gothic Neo", sans-serif; background: #F8FAFC; color: #0F172A; padding: 24px; }}
        .header {{ background: #FFFFFF; border-radius: 16px; padding: 24px 32px; margin-bottom: 24px; border: 1px solid #E2E8F0; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); }}
        .header-top {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px; margin-bottom: 20px; }}
        .header h1 {{ font-size: 24px; font-weight: 800; color: #0F172A; }}
        .header-meta {{ font-size: 13px; color: #64748B; }}
        .kpi-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }}
        .kpi-card {{ background: #F1F5F9; border-radius: 12px; padding: 18px 20px; border: 1px solid #E2E8F0; }}
        .kpi-title {{ font-size: 13px; color: #64748B; font-weight: 600; margin-bottom: 6px; }}
        .kpi-value {{ font-size: 26px; font-weight: 800; color: #0F172A; }}
        .kpi-sub {{ font-size: 12px; color: #94A3B8; margin-top: 4px; }}
        .month-tab-bar {{ display: flex; align-items: center; gap: 10px; margin-bottom: 22px; flex-wrap: wrap; background: #FFFFFF; padding: 12px 20px; border-radius: 12px; border: 1px solid #E2E8F0; }}
        .month-tab-label {{ font-size: 13px; font-weight: 800; color: #334155; }}
        .month-tab {{ display: inline-flex; align-items: center; padding: 9px 18px; border-radius: 10px; text-decoration: none; font-size: 13px; font-weight: 700; color: #475569; background: #F1F5F9; transition: all 0.2s ease; border: 1px solid #CBD5E1; }}
        .month-tab:hover {{ background: #E2E8F0; color: #0F172A; }}
        .month-tab.active {{ background: linear-gradient(135deg, #2563EB, #1D4ED8); color: #FFFFFF; border-color: #1D4ED8; box-shadow: 0 4px 10px rgba(37,99,235,0.25); }}
        .goal-banner {{ background: linear-gradient(135deg, #ECFDF5, #D1FAE5); border: 2px solid #10B981; border-radius: 14px; padding: 16px 22px; margin-bottom: 22px; display: flex; align-items: center; gap: 16px; box-shadow: 0 4px 12px rgba(16,185,129,0.15); }}
        .goal-banner-icon {{ font-size: 28px; }}
        .goal-banner-title {{ font-size: 16px; font-weight: 800; color: #065F46; }}
        .goal-banner-desc {{ font-size: 13px; color: #047857; margin-top: 4px; }}
        .today-card-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .today-card {{ background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 14px; padding: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.03); }}
        .today-card-top {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px; gap: 10px; }}
        .today-kw-query {{ font-size: 11px; color: #64748B; font-weight: 600; text-transform: uppercase; }}
        .today-kw-title {{ font-size: 16px; font-weight: 800; color: #0F172A; margin-top: 2px; }}
        .badge-contract-day {{ background: #FEF3C7; color: #92400E; font-size: 12px; font-weight: 800; padding: 4px 10px; border-radius: 9999px; border: 1px solid #FCD34D; white-space: nowrap; }}
        .badge-contract-day.goal {{ background: linear-gradient(135deg, #10B981, #059669); color: white; border: none; }}
        .today-card-slots {{ display: flex; flex-direction: column; gap: 8px; }}
        .slot-item {{ display: flex; justify-content: space-between; align-items: center; background: #F8FAFC; padding: 10px 14px; border-radius: 8px; border: 1px solid #F1F5F9; font-size: 13px; }}
        .slot-badge.ok {{ background: #D1FAE5; color: #065F46; font-weight: 800; padding: 3px 10px; border-radius: 6px; }}
        .slot-badge.fail {{ background: #FEE2E2; color: #991B1B; font-weight: 700; padding: 3px 10px; border-radius: 6px; }}
        .slot-badge.wait {{ background: #F1F5F9; color: #94A3B8; font-weight: 600; padding: 3px 10px; border-radius: 6px; }}
        .table-wrap {{ background: #FFFFFF; border-radius: 16px; border: 1px solid #E2E8F0; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); overflow-x: auto; margin-bottom: 24px; }}
        table {{ width: 100%; border-collapse: collapse; text-align: center; font-size: 13px; min-width: 800px; }}
        th, td {{ padding: 10px 12px; border-bottom: 1px solid #E2E8F0; }}
        th {{ background: #F8FAFC; color: #475569; font-weight: 700; }}
        .kw-th {{ background: #0F172A; color: #FFFFFF; font-size: 14px; padding: 12px; }}
        .sub-th {{ background: #F1F5F9; font-size: 12px; }}
        tr.today-row {{ background: #EFF6FF; font-weight: 700; }}
        tr.weekend-row {{ background: #F8FAFC; }}
        .badge-ok {{ color: #059669; font-weight: 800; }}
        .badge-fail {{ color: #CBD5E1; font-weight: 700; }}
        .badge-judge-ok {{ background: #D1FAE5; color: #065F46; padding: 3px 8px; border-radius: 6px; font-weight: 800; font-size: 12px; }}
        .badge-judge-half {{ background: #FEF3C7; color: #92400E; padding: 3px 8px; border-radius: 6px; font-weight: 800; font-size: 12px; }}
        .badge-judge-fail {{ color: #94A3B8; font-size: 12px; }}
        .cell-yellow-badge strong {{ background: #FEF08A; color: #854D0E; padding: 3px 8px; border-radius: 6px; }}
        .cell-goal-reached strong {{ background: linear-gradient(135deg, #10B981, #059669); color: white; padding: 4px 10px; border-radius: 6px; font-weight: 800; box-shadow: 0 2px 6px rgba(16,185,129,0.3); }}
        .cell-summary-goal {{ background: #ECFDF5; color: #065F46; font-size: 14px; font-weight: 800; text-align: center; }}
        .cell-summary {{ background: #F8FAFC; color: #334155; font-size: 13px; text-align: center; }}
        .footer {{ text-align: center; font-size: 12px; color: #94A3B8; margin-top: 32px; padding-bottom: 24px; }}
    </style>
</head>
<body>
    <div class="header">
        <div class="header-top">
            <div>
                <h1>🎥 {h_name} 유튜브 자동완성 실시간 모니터링 관제센터</h1>
                <div class="header-meta">24시간 클라우드 무인 자동 점검 · 매일 09:00 / 18:00 정기 점검 갱신</div>
            </div>
            <div>
                <a href="../" style="text-decoration: none; padding: 8px 16px; background: #0F172A; color: white; border-radius: 8px; font-size: 13px; font-weight: 700;">← 전체 병원 포털</a>
            </div>
        </div>
        <div class="kpi-row">
            <div class="kpi-card">
                <div class="kpi-title">모니터링 대상 키워드</div>
                <div class="kpi-value">{len(keywords)}개</div>
                <div class="kpi-sub">완전 분리 단독 관제</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">보장 계약 기준</div>
                <div class="kpi-value">30일 단위</div>
                <div class="kpi-sub">오전/오후 0.5일 합산 보장제</div>
            </div>
        </div>
    </div>

    {month_tab_bar}
    {banner_html}

    <div class="today-card-grid">
        {detail_cards}
    </div>

    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th rowspan="2" style="width: 70px;">날짜</th>
                    <th rowspan="2" style="width: 50px;">요일</th>
                    {th_keywords_row1}
                </tr>
                <tr>
                    {th_keywords_row2}
                </tr>
            </thead>
            <tbody>
                {table_rows}
                <tr>
                    {total_summary_tds}
                </tr>
            </tbody>
        </table>
    </div>

    <div class="footer">
        ⚡ GitHub Actions Cloud Automation Engine · 365일 24시간 무중단 가동
    </div>
</body>
</html>
"""
    return html

def main():
    print("=" * 60)
    print("🚀 [Cloud Monitor & Static Site Builder Running]")
    print("=" * 60)

    # 1. Load settings & records
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        settings = json.load(f)
    with open(RECORDS_FILE, "r", encoding="utf-8") as f:
        records = json.load(f)

    now_kst = datetime.now(KST)
    date_str = now_kst.strftime("%Y-%m-%d")
    time_str = now_kst.strftime("%H:%M:%S")
    hour = now_kst.hour

    slot = "오전 09:00" if hour < 14 else "오후 18:00"
    # 레코드 id는 기존 최대값 기준으로 부여 (로컬/클라우드 병합 시 충돌 방지)
    next_id = max([r.get("id", 0) for r in records] + [0]) + 1

    # 2. Check keywords
    all_kws = settings.get("keywords", [])
    active_kws = [k for k in all_kws if k.get("active", True)]
    new_records_count = 0

    print(f"[*] Checking {len(active_kws)} active keywords for {date_str} {slot}...")
    for k in active_kws:
        res = check_youtube_autocomplete(k["query"], k["target"])
        rec = {
            "id": next_id,
            "timestamp": f"{date_str} {time_str}",
            "date": date_str,
            "time": time_str,
            "slot": slot,
            "keyword_id": k["id"],
            "query": k["query"],
            "target": k["target"],
            "display_name": k["display_name"],
            "is_exposed": 1 if res["is_exposed"] else 0,
            "rank": res["rank"],
            "matched_text": res["matched_text"],
            "suggestions_json": json.dumps(res["suggestions"], ensure_ascii=False),
            "contract_day": 0
        }
        # Update or append
        existing_idx = None
        for idx, r in enumerate(records):
            if r["date"] == date_str and slot_key(r["slot"]) == slot_key(slot) and r["keyword_id"] == k["id"]:
                existing_idx = idx
                break

        if existing_idx is not None:
            records[existing_idx] = rec
        else:
            records.append(rec)
            next_id += 1
            new_records_count += 1

        print(f"  - [{k['hospital']}] {k['display_name']} -> exposed={res['is_exposed']}, rank={res['rank']}")

    # Save records.json
    with open(RECORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"✔ Saved records.json ({len(records)} total records, +{new_records_count} new)")

    # 3. Render HTMLs for all hospitals
    for hosp in ["secret", "oho"]:
        h_dir = os.path.join(BASE_DIR, hosp)
        os.makedirs(h_dir, exist_ok=True)

        for m in [9, 10, 11]:
            html = render_html(2026, m, hosp, settings, records)
            fname = "sep.html" if m == 9 else ("oct.html" if m == 10 else "nov.html")
            with open(os.path.join(h_dir, fname), "w", encoding="utf-8") as f:
                f.write(html)

        # index.html
        default_file = "nov.html" if hosp == "secret" else "sep.html"
        with open(os.path.join(h_dir, "index.html"), "w", encoding="utf-8") as f:
            with open(os.path.join(h_dir, default_file), "r", encoding="utf-8") as rf:
                f.write(rf.read())

    print("✔ Rendered all HTML dashboards (9, 10, 11) successfully!")
    print("=" * 60)

if __name__ == "__main__":
    main()
