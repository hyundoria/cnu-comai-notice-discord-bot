# bot.py — pip install "discord.py>=2.4" beautifulsoup4 aiohttp playwright
# playwright install
import os
import re
import json
import asyncio
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
# 사이버캠퍼스는 Chromium을 띄워야 해서 비싸다. 공지 게시판보다 성기게 확인한다.
CYBER_CHECK_MIN = 30
# 한 주기가 영원히 멈추는 것을 막기 위한 상한(초). CHECK_MIN 주기보다 반드시 짧게 둔다.
CYBER_TIMEOUT_SEC = 180   # 사이버캠퍼스 크롤 1회 상한
CRAWL_TIMEOUT_SEC = 420   # 크롤 주기 전체 상한
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

# 학식 (생협 모바일 식단 페이지: 날짜 하나에 식당들이 열로 나오는 표)
MENU_URL = "https://mobileadmin.cnu.ac.kr/food/index.jsp"
MENU_CACHE_SEC = 600   # 점심시간 동시 조회로 학교 서버를 두드리지 않도록 날짜별로 잠시 캐시
CAFETERIAS = ["제1학생회관", "제2학생회관", "제3학생회관", "제4학생회관", "생활과학대학"]


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

# ---------- 학식 메뉴 ----------
_menu_cache = {}   # 'YYYY.MM.DD' -> (fetched_at, parsed)

def parse_menu(html):
    """식단 표를 {식당: {'menus': [{'meal','target','title','items'}], 'link': url|None}}로."""
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.select_one('table.menu-tbl')
    if not table:
        return {}
    names = [th.get_text(strip=True) for th in table.select('thead th')][1:]  # 첫 칸은 '구분'
    out = {n: {'menus': [], 'link': None} for n in names}

    # 제1학생회관은 rowspan으로 모든 행을 덮는 '메뉴운영내역' 링크(외부 페이지)만 있다.
    m = re.search(r"const url = '([^']+)'", html)
    ext_link = m.group(1) if m else MENU_URL

    pending = [0] * len(names)   # 열마다 위 행의 rowspan이 아직 덮고 있는 행 수
    meal = ''
    for tr in table.select('tbody > tr'):
        tds = tr.find_all('td', recursive=False)
        if tds and 'building' in (tds[0].get('class') or []):
            meal = tds[0].get_text(strip=True)
            tds = tds[1:]
        if not tds:
            continue
        target, cells = tds[0].get_text(strip=True), iter(tds[1:])
        for i, name in enumerate(names):
            if pending[i]:
                pending[i] -= 1
                continue
            td = next(cells, None)
            if td is None:
                break
            pending[i] = int(td.get('rowspan', 1)) - 1
            if td.select_one('a[onclick*="openWindow"]'):
                out[name]['link'] = ext_link
                continue
            for li in td.select('li'):
                h3, p = li.select_one('h3'), li.select_one('p')
                items = [s for s in (p.get_text('\n', strip=True).split('\n') if p else []) if s]
                out[name]['menus'].append({
                    'meal': meal, 'target': target,
                    'title': h3.get_text(strip=True) if h3 else '',
                    'items': items,
                })
    return out

async def fetch_menu(day: date):
    key = day.strftime('%Y.%m.%d')
    now = asyncio.get_running_loop().time()
    hit = _menu_cache.get(key)
    if hit and now - hit[0] < MENU_CACHE_SEC:
        return hit[1]
    params = {'searchYmd': key, 'searchLang': 'OCL04.10', 'searchView': 'cafeteria'}
    async with aiohttp.ClientSession() as session:
        async with session.get(MENU_URL, params=params, timeout=aiohttp.ClientTimeout(total=15),
                               headers={'User-Agent': 'Mozilla/5.0'}) as r:
            r.raise_for_status()
            parsed = parse_menu(await r.text())
    # 날짜가 바뀌며 쌓이지 않게 만료된 항목은 버린다
    for k in [k for k, (t, _) in _menu_cache.items() if now - t >= MENU_CACHE_SEC]:
        del _menu_cache[k]
    _menu_cache[key] = (now, parsed)
    return parsed

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
            # context를 닫지 않으면 크롬 자식 프로세스가 남는다. 정리 자체가 멈출 수도 있어 상한을 둔다.
            for closer in (context.close, browser.close):
                try:
                    await asyncio.wait_for(closer(), timeout=30)
                except Exception as e:
                    print(f"playwright 정리 실패(무시): {e!r}")
            
    return items


# ---------- 메인 루프 ----------

# 시계 차이로 비교하면 주기 간격(600s)과 임계값(1800s)이 정확히 맞물려 지터에 흔들린다.
# 몇 번째 주기인지를 세는 편이 결정적이다.
_cyber_cycle = 0

async def _crawl_once():
    global _cyber_cycle
    cycle = _cyber_cycle
    _cyber_cycle += 1
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
    cyber_items = []
    cyber_every = max(1, round(CYBER_CHECK_MIN / CHECK_MIN))
    if cycle % cyber_every == 0:
        try:
            cyber_items = await asyncio.wait_for(
                fetch_and_parse_cyber_campus(), timeout=CYBER_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            print(f"⚠️ 사이버캠퍼스 크롤이 {CYBER_TIMEOUT_SEC}초를 넘겨 중단했습니다.")
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
    # 한 주기에서 예외가 나도, 또 응답 없이 멈춰도(행) 루프가 다음 주기로 계속 가도록 감싼다.
    try:
        await asyncio.wait_for(_crawl_once(), timeout=CRAWL_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        print(f"⚠️ crawl 주기가 {CRAWL_TIMEOUT_SEC}초를 넘겨 중단했습니다. 다음 주기에 계속합니다.")
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

@tree.command(name="학식", description="충남대 학생식당 메뉴를 봅니다.")
@discord.app_commands.describe(날짜="조회할 날짜 (기본: 오늘)", 식당="특정 식당만 보기 (기본: 전체)")
@discord.app_commands.choices(
    날짜=[discord.app_commands.Choice(name=n, value=i) for i, n in enumerate(("오늘", "내일", "모레"))],
    식당=[discord.app_commands.Choice(name=n, value=n) for n in CAFETERIAS],
)
async def menu_cmd(interaction: discord.Interaction,
                   날짜: discord.app_commands.Choice[int] = None,
                   식당: discord.app_commands.Choice[str] = None):
    # 학교 서버가 느리면 3초 응답 제한을 넘기므로 먼저 defer한다.
    await interaction.response.defer(ephemeral=True)
    day = _today_kst() + timedelta(days=날짜.value if 날짜 else 0)
    try:
        menu = await fetch_menu(day)
    except Exception as e:
        print(f"학식 조회 실패: {e!r}")
        await interaction.followup.send("학식 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
        return

    weekday = "월화수목금토일"[day.weekday()]
    embed = discord.Embed(title=f"🍚 {day.isoformat()}({weekday}) 학식", url=MENU_URL, color=0x27AE60)
    for name, info in menu.items():
        if 식당 and name != 식당.value:
            continue
        if info['menus']:
            blocks = [
                f"**{m['meal']}·{m['target']}** {m['title']}\n" + " · ".join(m['items'])
                for m in info['menus']
            ]
            value = "\n".join(blocks)
            if len(value) > 1024:   # 임베드 필드 값 상한
                value = value[:1023] + "…"
        elif info['link']:
            value = f"[메뉴운영내역 보기]({info['link']})"
        else:
            continue
        embed.add_field(name=name, value=value, inline=False)

    # 제1학생회관 링크는 날짜와 무관하게 항상 있으므로, 실제 메뉴가 있었는지로 판단한다.
    if not any(info['menus'] for name, info in menu.items() if not 식당 or name == 식당.value):
        where = f"{식당.value}은(는) " if 식당 else ""
        embed.description = f"{where}운영하지 않거나 식단이 아직 등록되지 않았습니다."
    await interaction.followup.send(embed=embed, ephemeral=True)

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