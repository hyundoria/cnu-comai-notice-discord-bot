# bot.py — pip install "discord.py>=2.4" beautifulsoup4 aiohttp playwright
# playwright install
import os
import re
import json
import traceback
from datetime import date, datetime, timedelta, timezone
import aiohttp
import db
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import discord
from discord.ext import tasks
from dotenv import load_dotenv

# [핵심 변경] 비동기 Playwright 임포트
from playwright.async_api import async_playwright

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
ID = os.environ["ID"]
PW = os.environ["PW"]
CHECK_MIN = 10
CHANNEL_NAME = "공지알림"
KST = timezone(timedelta(hours=9))  # 마감일 판단은 한국 시간 기준

CATEGORIES = {
    "학사공지":   "https://comai.cnu.ac.kr/computer/notice/bachelor.do",
    "일반공지":   "https://comai.cnu.ac.kr/computer/notice/notice.do",
    "취업정보":   "https://comai.cnu.ac.kr/computer/notice/job.do",
    "사업단공지": "https://comai.cnu.ac.kr/computer/notice/project.do",
}

# 사이버 캠퍼스 URL
cyber_campus_URL = "https://dcs-lcms.cnu.ac.kr/login"
todo_URL = "https://dcs-learning.cnu.ac.kr/std/todo"


# 디스코드 클라이언트 초기화
intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)  # 슬래시 커맨드용

# ---------- 채널 자동 확보 (기존과 동일) ----------
async def ensure_channel(guild: discord.Guild):
    cid = db.get_guild_channel(guild.id)
    if cid:
        ch = guild.get_channel(cid)
        if ch: return ch

    for ch in guild.text_channels:
        if ch.name == CHANNEL_NAME and ch.permissions_for(guild.me).send_messages:
            db.upsert_guild_channel(guild.id, ch.id)
            return ch

    try:
        ch = await guild.create_text_channel(
            CHANNEL_NAME, topic="충남대 컴공지능 및 사이버캠퍼스 공지 자동 알림",
            reason="공지 봇 초기 설정"
        )
        db.upsert_guild_channel(guild.id, ch.id)
        return ch
    except discord.Forbidden:
        fallback = guild.system_channel
        if not (fallback and fallback.permissions_for(guild.me).send_messages):
            fallback = next(
                (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None
            )
        if fallback:
            db.upsert_guild_channel(guild.id, fallback.id)
            try:
                await fallback.send(f"⚠️ `#{CHANNEL_NAME}` 채널 생성 권한이 부족합니다.")
            except discord.HTTPException: pass
        return fallback

# ---------- 크롤러 함수 모음 ----------

async def fetch(session, url):
    async with session.get(url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'}) as r:
        return await r.text()

def parse_general(html, base):
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    for row in soup.select('table.board-table tbody tr'):
        a = row.select_one('td.b-td-left a')
        if not a: continue
        m = re.search(r'articleNo=(\d+)', a.get('href', ''))
        if not m: continue
        items.append({
            'id': m.group(1),
            'title': a.get_text(strip=True),
            'link': urljoin(base, a['href']),
            'sticky': 'b-top-box' in (row.get('class') or []),
        })
    return items

# ---------- 마감일 추출 (제목 기반, best-effort) ----------
_DATE_RE = re.compile(r'(?:(\d{4})\s*[.\-/년]\s*)?(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?')
# 아래 힌트가 제목에 없으면 날짜가 있어도 마감일로 보지 않는다(게시일 등 오탐 방지).
_DEADLINE_HINTS = ('까지', '마감', '신청', '접수', '기한', '모집', '~')

def _today_kst() -> date:
    return datetime.now(KST).date()

def _find_dates(text: str, today: date):
    out = []
    for m in _DATE_RE.finditer(text):
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        year = int(y) if y else today.year
        try:
            dt = date(year, mo, d)
        except ValueError:
            continue
        # 연도 미기재 & 이미 30일 이상 지난 날짜면 내년으로 보정
        if not y and dt < today - timedelta(days=30):
            try:
                dt = date(year + 1, mo, d)
            except ValueError:
                continue
        out.append(dt)
    return out

def extract_deadline(title: str):
    """제목에서 마감일로 추정되는 날짜를 'YYYY-MM-DD'로 반환. 없으면 None."""
    if not title or not any(kw in title for kw in _DEADLINE_HINTS):
        return None
    today = _today_kst()
    # 범위(예: 7.15~7.30)면 '~' 뒤(종료일)를 우선 본다.
    search_text = title.rsplit('~', 1)[1] if '~' in title else title
    cands = _find_dates(search_text, today)
    if not cands:
        cands = _find_dates(title, today)
    if not cands:
        return None
    return cands[-1].isoformat()  # 가장 뒤의 날짜를 마감일로 본다

# 사이버 캠퍼스 Playwright 비동기 접근 및 파싱
async def fetch_and_parse_cyber_campus():
    items = []
    # async_playwright를 컨텍스트 매니저로 실행
    async with async_playwright() as p:
        # headless=True로 설정하여 백그라운드에서 실행 (서버 배포 시 필수)
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # 1. 로그인 페이지 이동
            await page.goto(cyber_campus_URL)
            
            # 💡 [TODO] 아래 선택자(selector)를 실제 사이트에 맞게 수정하세요.
            await page.fill('input[name="user_id"]', ID) 
            await page.fill('input[name="user_password"]', PW)
            await page.click('button[data-act="clickLogin"]')
            
            # 로그인 처리가 완료될 때까지 대기
            await page.wait_for_load_state('networkidle')

            # 2. Todo(공지) 페이지로 이동
            await page.goto(todo_URL)
            await page.wait_for_load_state('networkidle')

            # 3. HTML 파싱
            html = await page.content()
            soup = BeautifulSoup(html, 'html.parser')
            
            todo_rows = soup.select('.tabulator-row') 
            
            for row in todo_rows:
                # 고유 ID 추출 (db에서 중복 방지용)
                id_el = row.select_one('div[tabulator-field="rseq"]')
                course_el = row.select_one('div[tabulator-field="course_nm"]')
                title_el = row.select_one('div[tabulator-field="boarditem_title"]')
                
                if id_el and title_el:
                    item_id = id_el.get_text(strip=True)
                    course_name = course_el.get_text(strip=True) if course_el else "사이버캠퍼스"
                    title_text = title_el.get_text(strip=True)
                    
                    items.append({
                        'id': f"todo_{item_id}",
                        # 디스코드 알림 제목에 과목명을 함께 표시합니다.
                        'title': f"[{course_name}] {title_text}", 
                        'link': todo_URL,
                        'sticky': False
                    })
                    
        except Exception as e:
            print(f"사이버 캠퍼스 크롤링 실패: {e}")
        finally:
            await browser.close()
            
    return items


# ---------- 메인 루프 ----------

async def _crawl_once():
    broadcasts = []      # [(category, article_no, embed), ...]
    
    # 1. 기존 일반 웹사이트 크롤링 (aiohttp)
    async with aiohttp.ClientSession() as session:
        for cat, url in CATEGORIES.items():
            try:
                html = await fetch(session, url)
                items = parse_general(html, url)
            except Exception as e:
                print(f"crawl fail {cat}: {e}")
                continue
            
            if not items: continue
            
            if db.is_baseline_empty(cat):
                db.mark_seen(cat, items)
                continue

            new_ids = db.filter_new(cat, [n['id'] for n in items])
            new_items = [n for n in items if n['id'] in new_ids]

            today_str = _today_kst().isoformat()
            for n in reversed(new_items):
                tag = "📌공지" if n['sticky'] else "🆕 새 글"
                embed = discord.Embed(
                    title=n['title'], url=n['link'],
                    description=f"**[{cat}]** 게시판에 {tag}가 등록되었습니다.", color=0x0054A6
                )
                broadcasts.append((cat, n['id'], embed))

                # 제목에서 마감일을 뽑아 미래 날짜면 리마인더용으로 저장
                dl = extract_deadline(n['title'])
                if dl and dl >= today_str:
                    db.add_deadline(cat, n['id'], n['title'], n['link'], dl)

            db.mark_seen(cat, new_items)
            db.trim_old(cat, keep=500)

    # 2. 사이버 캠퍼스 크롤링 (Playwright)
    cyber_items = await fetch_and_parse_cyber_campus()
    cat_cyber = "사이버캠퍼스 Todo"
    
    if cyber_items:
        if db.is_baseline_empty(cat_cyber):
            db.mark_seen(cat_cyber, cyber_items)
        else:
            new_ids = db.filter_new(cat_cyber, [n['id'] for n in cyber_items])
            new_items = [n for n in cyber_items if n['id'] in new_ids]

            for n in reversed(new_items):
                embed = discord.Embed(
                    title=n['title'], url=n['link'],
                    description=f"**[{cat_cyber}]** 새로운 알림이 있습니다.", color=0xE74C3C # 사이버캠퍼스는 눈에 띄게 빨간색
                )
                broadcasts.append((cat_cyber, n['id'], embed))

            db.mark_seen(cat_cyber, new_items)
            db.trim_old(cat_cyber, keep=500)

    # 3. 디스코드로 메시지 전송
    if not broadcasts:
        return

    for guild_id, channel_id in db.all_guild_channels():
        guild = client.get_guild(guild_id)
        if not guild: continue
        ch = guild.get_channel(channel_id) or await ensure_channel(guild)
        if not ch: continue

        for cat, art_no, embed in broadcasts:
            try:
                await ch.send(embed=embed)
                db.log_notification(cat, art_no, guild_id, True)
            except discord.HTTPException as e:
                print(f"send fail guild={guild_id}: {e}")
                db.log_notification(cat, art_no, guild_id, False)


@tasks.loop(minutes=CHECK_MIN)
async def crawl():
    # 한 주기에서 예기치 못한 예외가 나도 루프가 죽지 않고 다음 주기로 계속 진행되도록 감싼다.
    try:
        await _crawl_once()
    except Exception as e:
        print(f"⚠️ crawl 주기 실패, 다음 주기에 계속합니다: {e!r}")
        traceback.print_exc()

@crawl.error
async def crawl_error(error: Exception):
    # 위 try/except를 벗어난 경로로 루프가 멈춘 경우를 대비한 안전망.
    print(f"⚠️ crawl 루프가 중단되어 재시작합니다: {error!r}")
    traceback.print_exc()
    if not crawl.is_running():
        crawl.restart()

# ---------- 마감 리마인더 루프 ----------

async def _broadcast(embed: discord.Embed):
    """등록된 모든 길드 알림 채널로 임베드 전송."""
    for guild_id, channel_id in db.all_guild_channels():
        guild = client.get_guild(guild_id)
        if not guild:
            continue
        ch = guild.get_channel(channel_id) or await ensure_channel(guild)
        if not ch:
            continue
        try:
            await ch.send(embed=embed)
        except discord.HTTPException as e:
            print(f"reminder send fail guild={guild_id}: {e}")

@tasks.loop(hours=6)
async def deadline_reminder():
    try:
        today = _today_kst()
        tomorrow = today + timedelta(days=1)
        due = db.due_deadlines(today.isoformat(), tomorrow.isoformat())
        for row in due:
            dl = date.fromisoformat(row["deadline_date"])
            when = "오늘" if dl == today else "내일"
            embed = discord.Embed(
                title=f"⏰ 마감 임박: {row['title']}",
                url=row["link"],
                description=f"**[{row['category']}]** 마감이 **{when}({row['deadline_date']})**입니다.",
                color=0xF39C12,
            )
            await _broadcast(embed)
            db.mark_deadline_reminded(row["category"], row["article_no"])
        # 마감 지난 지 오래된 항목 정리(7일 이전)
        db.trim_old_deadlines((today - timedelta(days=7)).isoformat())
    except Exception as e:
        print(f"⚠️ deadline_reminder 실패: {e!r}")
        traceback.print_exc()

@deadline_reminder.before_loop
async def _before_deadline_reminder():
    await client.wait_until_ready()

# ---------- 슬래시 커맨드 ----------

@tree.command(name="통계", description="공지 전송 통계와 감지 현황을 봅니다.")
async def stats_cmd(interaction: discord.Interaction):
    s = db.notification_stats(days=7)
    embed = discord.Embed(title="📊 공지 봇 통계", color=0x0054A6)

    if s["per_category"]:
        lines = [f"`{cat}` ✅ {ok}  ❌ {fail}" for cat, ok, fail in s["per_category"]]
        embed.add_field(name=f"최근 {s['days']}일 전송", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name=f"최근 {s['days']}일 전송", value="기록 없음", inline=False)

    embed.add_field(name="전체 누계", value=f"✅ {s['total_ok']}  ❌ {s['total_fail']}", inline=True)
    if s["last_at"]:
        embed.add_field(name="마지막 전송", value=s["last_at"][:19].replace("T", " ") + " UTC", inline=True)

    if s["seen_counts"]:
        lines = [f"`{cat}` {n}건" for cat, n in s["seen_counts"]]
        embed.add_field(name="감지·저장된 공지 수", value="\n".join(lines), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="마감", description="다가오는 마감 일정을 봅니다.")
async def deadline_cmd(interaction: discord.Interaction):
    today = _today_kst()
    rows = db.upcoming_deadlines(today.isoformat(), limit=10)
    if not rows:
        await interaction.response.send_message("다가오는 마감 일정이 없습니다.", ephemeral=True)
        return
    embed = discord.Embed(title="⏰ 다가오는 마감", color=0xF39C12)
    for row in rows:
        dl = date.fromisoformat(row["deadline_date"])
        days = (dl - today).days
        d_label = "D-DAY" if days == 0 else f"D-{days}"
        embed.add_field(
            name=f"{d_label} · {row['deadline_date']}",
            value=f"[{row['title']}]({row['link']})",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- 이벤트 ----------

@client.event
async def on_ready():
    db.init()
    print(f"✅ Logged in as {client.user}  | servers: {len(client.guilds)}")
    for g in client.guilds:
        await ensure_channel(g)
    try:
        synced = await tree.sync()
        print(f"🔧 슬래시 커맨드 {len(synced)}개 동기화 완료")
    except Exception as e:
        print(f"슬래시 커맨드 동기화 실패: {e!r}")
    if not crawl.is_running():
        crawl.start()
    if not deadline_reminder.is_running():
        deadline_reminder.start()

@client.event
async def on_guild_join(guild: discord.Guild):
    ch = await ensure_channel(guild)
    if ch:
        welcome_embed = discord.Embed(
            title="🤖 공지 알림 봇이 추가되었습니다!",
            description=f"앞으로 일반 공지와 사이버 캠퍼스 알림을 전달해 드릴게요.\n자동으로 {CHECK_MIN}분마다 업데이트됩니다.",
            color=0x2ECC71
        )
        await ch.send(embed=welcome_embed)

client.run(TOKEN)