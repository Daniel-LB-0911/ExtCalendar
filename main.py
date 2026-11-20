import os
import re
import time
from datetime import date
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    base_url: str 
    cal_user: str
    cal_pass: str

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()

# ====== 設定區（建議改用環境變數或 .env 管理帳密） ======
BASE_URL = settings.base_url
USERNAME = settings.cal_user
PASSWORD = settings.cal_pass

CALENDAR_URL = f"{BASE_URL}/calendar.php"
LOGIN_URL = f"{BASE_URL}/login.php"


# 簡易記憶體快取，避免頻繁打舊版 PHP 系統（預設快取 300 秒）
CACHE_TTL_SECONDS = 300
_cache: dict[str, tuple[float, list[dict]]] = {}

app = FastAPI(title="SRIS 出差日曆 API")


# ====== 資料模型 ======
class TripEvent(BaseModel):
    date: str                 # 例如 "2026-11-02"
    raw_title: str            # 原始文字 "Google 101/Mo、Joe"
    location: str             # 拆解後地點 "Google 101"
    people: list[str]         # 拆解後人員 ["Mo", "Joe"]
    event_url: Optional[str] = None
    border_color: Optional[str] = None  # 左側分類色碼（若有）


# ====== 1. 標題拆解邏輯（針對貴公司 地點/人員 格式） ======
def parse_title(raw_title: str) -> tuple[str, list[str]]:
    # 去除 ExtCalendar 前面的菱形符號或前後空白
    cleaned = raw_title.strip().lstrip("❖◇• ").strip()

    # 優先用 "/" 分割，若無 "/" 再試著處理像 "臺大醫院-Tiffany" 這種 "-"
    if "/" in cleaned:
        loc_part, people_part = cleaned.split("/", 1)
    elif "-" in cleaned:
        loc_part, people_part = cleaned.rsplit("-", 1)
    else:
        return cleaned, []

    location = loc_part.strip()
    # 依頓號、逗號、空白拆出人員，並清掉「支援」等補充字眼（可視需求調整）
    raw_people = re.split(r"[、,，]+", people_part.strip())
    people = [p.strip() for p in raw_people if p.strip()]
    return location, people


# ====== 2. 登入並抓取指定月份 HTML ======
async def fetch_calendar_html(year: int, month: int) -> str:
    target_date = f"{year:04d}-{month:02d}-01"

    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        # 步驟 A：先讀取登入頁（部分系統會需要順便取得初始 cookie 或隱藏欄位）
        await client.get(LOGIN_URL)

        # 步驟 B：送出登入表單（※ 請用 F12 確認貴公司的欄位名稱是否為 username / password）
        login_payload = {
            "username": USERNAME,
            "password": PASSWORD,
            "submit": "Login",  # 依 F12 實際送出欄位調整或刪除
        }
        login_resp = await client.post(LOGIN_URL, data=login_payload)
        if "Logout" not in login_resp.text and "calendar.php" not in str(login_resp.url):
            # 若登入頁與月曆不在同一頁，再請求一次月曆頁確認是否登入成功
            check_resp = await client.get(CALENDAR_URL)
            if "Logout" not in check_resp.text:
                raise HTTPException(status_code=401, detail="日曆系統登入失敗，請確認帳密或登入欄位名稱")

        # 步驟 C：抓取目標月份（ExtCalendar 2 通常支援 ?date=YYYY-MM-DD）
        resp = await client.get(CALENDAR_URL, params={"date": target_date})
        resp.raise_for_status()
        return resp.text


# ====== 3. 解析月曆 HTML ======
def parse_monthly_html(html: str, year: int, month: int) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    events: list[dict] = []

    # 策略：ExtCalendar 2 月曆中，每天的格子通常包含一個顯示日期數字的區塊
    # 以及一個連往新增活動（含該日期 YYYY-MM-DD）或顯示當天活動的連結
    # 這裡示範用「尋找當月每個日期格」的方式解析（建議依 F12 實際 HTML 結構微調 selector）

    # 找所有可能是日期儲存格的 <td>（ExtCalendar 2 常用 weekdayclr / todayclr / sundayemptyclr 等 class）
    day_cells = soup.find_all(
        "td",
        class_=re.compile(r"(weekdayclr|todayclr|sundayclr|saturdayclr|calday)", re.I),
    )

    # 若貴公司改過 class 名稱導致找不到，改用寬鬆備案：找內含日曆數字表格的 td
    if not day_cells:
        day_cells = [
            td for td in soup.find_all("td")
            if td.find("td", class_=re.compile(r"caldaydigits", re.I))
        ]

    for cell in day_cells:
        # 1) 取得這格是幾號
        digit_el = cell.find(class_=re.compile(r"caldaydigits", re.I))
        if not digit_el:
            continue

        day_match = re.search(r"\b(\d{1,2})\b", digit_el.get_text(strip=True))
        if not day_match:
            continue

        day_num = int(day_match.group(1))
        # 排除上/下個月殘留日期（例如月初出現 "October 2026" 或月底出現 "December 2026"）
        cell_text = cell.get_text(" ", strip=True)
        if re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}", cell_text):
            continue

        current_date = f"{year:04d}-{month:02d}-{day_num:02d}"

        # 2) 找出該格子內的所有活動連結
        # ExtCalendar 2 的活動連結通常帶有 mode=view 或 extid=，排除掉新增活動的 "+" 連結
        for a_tag in cell.find_all("a", href=True):
            href = a_tag["href"]
            title_text = a_tag.get_text(" ", strip=True)

            # 跳過日期數字本身與 "+" 新增按鈕
            if not title_text or title_text == str(day_num) or "addevent" in href.lower():
                continue

            # 抓取左側分類顏色（如果風格寫在包住 a_tag 的 td/div 的 style 或 bgcolor 上）
            border_color = None
            parent_box = a_tag.find_parent(["td", "div"])
            if parent_box:
                style = parent_box.get("style", "")
                color_match = re.search(r"(?:border-left-color|border-left|background-color):\s*([^;]+)", style, re.I)
                if color_match:
                    border_color = color_match.group(1).strip()

            location, people = parse_title(title_text)
            events.append({
                "date": current_date,
                "raw_title": title_text,
                "location": location,
                "people": people,
                "event_url": urljoin(BASE_URL + "/", href),
                "border_color": border_color,
            })

    return events


async def get_month_trips_cached(year: int, month: int, force_refresh: bool = False) -> list[dict]:
    cache_key = f"{year:04d}-{month:02d}"
    now = time.time()

    if not force_refresh and cache_key in _cache:
        cached_at, data = _cache[cache_key]
        if now - cached_at < CACHE_TTL_SECONDS:
            return data

    html = await fetch_calendar_html(year, month)
    trips = parse_monthly_html(html, year, month)
    _cache[cache_key] = (now, trips)
    return trips


# ====== 4. FastAPI 路由 ======
@app.get("/api/trips", response_model=dict)
async def get_trips(
    year: int = Query(default_factory=lambda: date.today().year, description="西元年"),
    month: int = Query(default_factory=lambda: date.today().month, ge=1, le=12, description="月份 (1-12)"),
    person: Optional[str] = Query(default=None, description="依人名篩選，例如 Kyle"),
    refresh: bool = Query(default=False, description="是否強制略過快取重新抓取"),
):
    """取得指定月份的大家出差資訊"""
    trips = await get_month_trips_cached(year, month, force_refresh=refresh)

    if person:
        keyword = person.lower()
        trips = [
            t for t in trips
            if any(keyword in p.lower() for p in t["people"]) or keyword in t["raw_title"].lower()
        ]

    return {
        "year": year,
        "month": month,
        "total_events": len(trips),
        "events": trips,
    }


@app.get("/api/trips/by-person", response_model=dict)
async def get_trips_by_person(
    year: int = Query(default_factory=lambda: date.today().year),
    month: int = Query(default_factory=lambda: date.today().month, ge=1, le=12),
    refresh: bool = Query(default=False),
):
    """依員工姓名彙整本月出差行程（方便直接查某人這月要去哪）"""
    trips = await get_month_trips_cached(year, month, force_refresh=refresh)

    summary: dict[str, list[dict]] = {}
    for t in trips:
        targets = t["people"] if t["people"] else ["未標註人員"]
        for p in targets:
            summary.setdefault(p, []).append({
                "date": t["date"],
                "location": t["location"],
                "raw_title": t["raw_title"],
            })

    return {
        "year": year,
        "month": month,
        "people_count": len(summary),
        "by_person": summary,
    }