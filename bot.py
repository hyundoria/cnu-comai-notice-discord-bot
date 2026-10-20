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
# 이 시간 동안 한 번도 성공하지 못한 크롤 소스가 있으면 봇 소유자에게 DM으로 알린다.
# 학교 사이트 야간 점검에 울리지 않도록 크롤 주기보다 넉넉하게 둔다.
STALE_ALERT_MIN = 180
CYBER_CAT = "사이버캠퍼스 Todo"
CRAWL_LOOP = "크롤 주기"   # 주기 전체가 끝까지 돌았는지(행 감지용)
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
# 사이트 표 머리글(정식 이름) -> 디스코드에 보일 줄임말
CAFETERIAS = {"제1학생회관": "1학", "제2학생회관": "2학", "제3학생회관": "3학",
              "제4학생회관": "4학", "생활과학대학": "생과대"}


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
            CHANNEL_NAME, topic="충남대 컴퓨터인공지능학부 공지 자동 알림",
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

class CyberAuthError(RuntimeError):
    """학교 서버가 아이디·비밀번호가 틀렸다고 답함. 계속 재시도하면 포털 계정이 잠길 수 있다."""

# 설정되면 재시작(.env 수정 후) 전까지 사이버캠퍼스 로그인을 다시 시도하지 않는다.
_cyber_auth_error = None

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
            # 페이지 JS가 소속 대학(CNU)과 통합로그인(포털 비밀번호) 체크를 채운 뒤에 눌러야 한다.
            await page.wait_for_function(
                "() => document.loginForm && document.loginForm.univ_no.value === 'CNU'"
                " && document.querySelector('#cnu_auth')?.checked",
                timeout=15000,
            )
            await page.fill('input[name="user_id"]', ID)
            await page.fill('input[name="user_password"]', PW)

            # 로그인은 AJAX라 클릭 직후엔 페이지가 그대로다(networkidle 대기는 즉시 끝나 버린다).
            # 로그인 응답을 직접 받아 학교 서버가 준 결과를 확인한다.
            async with page.expect_response(lambda r: '/user/apiCnuAuthLogin' in r.url, timeout=30000) as info:
                await page.click('button[data-act="clickLogin"]')
            res = await (await info.value).json()
            header = res.get('header') or {}
            if str(header.get('code')) != '200':
                msg = f"로그인 거부({header.get('code')}): {header.get('msg')}"
                raise (CyberAuthError if str(header.get('code')) == '2003' else RuntimeError)(msg)
            ret = str((res.get('body') or {}).get('ret_code'))
            blocked = {"0": "개인정보 약관 동의 필요 — 브라우저로 한 번 로그인해 동의하세요",
                       "-1": "미승인 사용자", "-2": "초기 비밀번호 변경 필요"}
            if ret in blocked:
                raise RuntimeError(f"로그인 후 진행 불가: {blocked[ret]}")
            await page.wait_for_url(lambda u: '/login' not in u, timeout=20000)

            # 2. Todo(공지) 페이지로 이동
            await page.goto(todo_URL, wait_until='networkidle')
            # 세션이 안 잡혔으면 예외 없이 로그인 페이지로 돌려보내져 '할 일 없음'처럼 보인다.
            if 'login' in page.url:
                raise RuntimeError(f"로그인 실패로 보임 (도착 URL: {page.url})")
            # 표는 AJAX로 채워진다. 행이나 '항목 없음' 표시 중 하나가 뜰 때까지 기다린다.
            await page.wait_for_selector('.tabulator-row, .tabulator-placeholder', timeout=20000)

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
            raise   # 호출 쪽에서 실패로 기록한다
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
    global _cyber_cycle, _cyber_auth_error
    cycle = _cyber_cycle
    _cyber_cycle += 1
    broadcasts = []      # [(category, article_no, embed), ...] — 모든 서버 알림 채널로
    owner_dms = []       # 같은 형식 — 개인 계정 정보(사이버캠퍼스)라 봇 소유자 DM으로만
    
    # 1. 기존 일반 웹사이트 크롤링 (aiohttp)
    async with aiohttp.ClientSession() as session:
        for cat, url in CATEGORIES.items():
            try:
                html = await fetch(session, url)
                items = parse_general(html, url)
            except Exception as e:
                print(f"crawl fail {cat}: {e}")
                db.record_crawl(cat, False, repr(e))
                continue

            # 게시판이 비어 있을 리는 없으니, 빈 목록은 페이지 구조가 바뀐 것으로 본다.
            if not items:
                db.record_crawl(cat, False, "게시글 목록을 찾지 못함 (페이지 구조 변경?)")
                continue
            db.record_crawl(cat, True)
            
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
    if cycle % cyber_every == 0 and _cyber_auth_error:
        db.record_crawl(CYBER_CAT, False, f"{_cyber_auth_error} (잠금 방지로 재시도 중단 — .env 수정 후 재시작)")
    elif cycle % cyber_every == 0:
        try:
            cyber_items = await asyncio.wait_for(
                fetch_and_parse_cyber_campus(), timeout=CYBER_TIMEOUT_SEC
            )
            db.record_crawl(CYBER_CAT, True)
        except CyberAuthError as e:
            _cyber_auth_error = str(e)
            db.record_crawl(CYBER_CAT, False, f"{e} (잠금 방지로 재시도 중단 — .env 수정 후 재시작)")
            await _notify_owner(
                f"🔒 사이버캠퍼스 로그인 실패: {e}\n포털 계정이 잠기지 않도록 재시작 전까지 로그인을 다시 시도하지 않습니다.\n"
                "서버의 `.env`에서 `ID`/`PW`를 고친 뒤 컨테이너를 다시 띄워 주세요."
            )
        except asyncio.TimeoutError:
            print(f"⚠️ 사이버캠퍼스 크롤이 {CYBER_TIMEOUT_SEC}초를 넘겨 중단했습니다.")
            db.record_crawl(CYBER_CAT, False, f"{CYBER_TIMEOUT_SEC}초 시간 초과")
        except Exception as e:
            db.record_crawl(CYBER_CAT, False, repr(e))
    cat_cyber = CYBER_CAT
    
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
                # 공개 봇이라 누가 초대하든 그 서버 채널에 내 과목·과제가 올라가면 안 된다.
                owner_dms.append((cat_cyber, n['id'], embed))

            db.mark_seen(cat_cyber, new_items)
            db.trim_old(cat_cyber, keep=500)

    # 3. 디스코드로 메시지 전송
    for cat, art_no, embed in owner_dms:
        db.log_notification(cat, art_no, 0, await _notify_owner(embed=embed))   # guild_id 0 = 소유자 DM

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
        db.record_crawl(CRAWL_LOOP, True)
    except asyncio.TimeoutError:
        print(f"⚠️ crawl 주기가 {CRAWL_TIMEOUT_SEC}초를 넘겨 중단했습니다. 다음 주기에 계속합니다.")
        db.record_crawl(CRAWL_LOOP, False, f"{CRAWL_TIMEOUT_SEC}초 시간 초과")
    except Exception as e:
        print(f"⚠️ crawl 주기 실패, 다음 주기에 계속합니다: {e!r}")
        traceback.print_exc()
        db.record_crawl(CRAWL_LOOP, False, repr(e))

@crawl.error
async def crawl_error(error: Exception):
    # 위 try/except를 벗어난 경로로 루프가 멈춘 경우를 대비한 안전망.
    print(f"⚠️ crawl 루프가 중단되어 재시작합니다: {error!r}")
    traceback.print_exc()
    if not crawl.is_running():
        crawl.restart()

# ---------- 이상 감지 루프 ----------

# 디스코드엔 초록불인데 크롤만 조용히 멈추는 경우(예: PID 고갈로 인한 행)를 잡기 위한 것.
# 크롤 루프와 별개 태스크라서 크롤이 멈춰도 이 루프는 돈다.
_started_at = datetime.now(timezone.utc)
_alerted = set()   # 이미 알린 소스(복구되면 빠진다)

def _monitored_sources():
    return [CRAWL_LOOP, *CATEGORIES, CYBER_CAT]

def _stale_sources(statuses):
    """STALE_ALERT_MIN 동안 성공이 없는 소스 목록. 재시작 직후 오탐을 막으려 기동 시각도 기준에 넣는다."""
    now = datetime.now(timezone.utc)
    out = []
    for src in _monitored_sources():
        last_ok = (statuses.get(src) or {}).get("last_ok")
        since = max(_started_at, datetime.fromisoformat(last_ok)) if last_ok else _started_at
        if now - since > timedelta(minutes=STALE_ALERT_MIN):
            out.append(src)
    return out

async def _notify_owner(text: str = None, embed: discord.Embed = None) -> bool:
    try:
        app = await client.application_info()
        owner = app.team.owner if app.team else app.owner
        await owner.send(text, embed=embed)
        return True
    except Exception as e:
        print(f"소유자 DM 실패: {e!r}")
        return False

@tasks.loop(minutes=15)
async def health_check():
    try:
        statuses = db.crawl_statuses()
        stale = set(_stale_sources(statuses))
        for src in sorted(stale - _alerted):
            err = (statuses.get(src) or {}).get("last_error") or "기록된 오류 없음"
            await _notify_owner(
                f"⚠️ **{src}** 크롤이 {STALE_ALERT_MIN // 60}시간 넘게 한 번도 성공하지 못했습니다.\n"
                f"마지막 오류: `{err}`\n`/상태`로 전체 현황을 볼 수 있습니다."
            )
        for src in sorted(_alerted - stale):
            await _notify_owner(f"✅ **{src}** 크롤이 다시 정상입니다.")
        _alerted.clear()
        _alerted.update(stale)
    except Exception as e:
        print(f"⚠️ health_check 실패: {e!r}")
        traceback.print_exc()

@health_check.before_loop
async def _before_health_check():
    await client.wait_until_ready()

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

@tree.command(name="상태", description="공지 크롤링이 정상적으로 돌고 있는지 봅니다.")
async def status_cmd(interaction: discord.Interaction):
    statuses = db.crawl_statuses()
    stale = set(_stale_sources(statuses))
    embed = discord.Embed(
        title="🩺 봇 상태",
        description=(f"기동 {discord.utils.format_dt(_started_at, 'R')} · 서버 {len(client.guilds)}개\n"
                     f"{STALE_ALERT_MIN // 60}시간 넘게 성공이 없으면 ⚠️로 표시합니다."),
        color=0xE67E22 if stale else 0x2ECC71,
    )
    for src in _monitored_sources():
        st = statuses.get(src) or {}
        last_ok, last_fail = st.get("last_ok"), st.get("last_fail")
        lines = [f"마지막 성공 {discord.utils.format_dt(datetime.fromisoformat(last_ok), 'R')}"
                 if last_ok else "아직 성공 기록 없음"]
        # 마지막 성공 이후에 난 실패만 보여준다(이미 복구된 옛 오류는 생략).
        if last_fail and (not last_ok or last_fail > last_ok):
            lines.append(f"실패 {discord.utils.format_dt(datetime.fromisoformat(last_fail), 'R')}: "
                         f"`{(st.get('last_error') or '')[:150]}`")
        embed.add_field(name=f"{'⚠️' if src in stale else '✅'} {src}", value="\n".join(lines), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

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

# 날짜 선택지: 상대 날짜(+N일) 또는 요일(오늘 포함 가장 가까운 그 요일)
MENU_DAY_CHOICES = [("오늘", "+0"), ("내일", "+1"), ("모레", "+2")] + \
                   [(f"{w}요일", f"w{i}") for i, w in enumerate("월화수목금토")]
MEALS = ("조식", "중식", "석식")
# 끼니가 끝나는 시각(KST). 옵션 없이 부르면 아직 안 끝난 첫 끼니를 보여준다.
MEAL_ENDS = ((9, 30), (14, 0), (19, 0))

def _menu_day(choice: str, today: date) -> date:
    if choice.startswith("w"):
        return today + timedelta(days=(int(choice[1:]) - today.weekday()) % 7)
    return today + timedelta(days=int(choice))

def _upcoming_meal(now: datetime):
    """(날짜 오프셋, 끼니). 석식까지 끝났으면 내일 중식."""
    for meal, (h, m) in zip(MEALS, MEAL_ENDS):
        if (now.hour, now.minute) < (h, m):
            return 0, meal
    return 1, "중식"

@tree.command(name="학식", description="충남대 학생식당 메뉴를 봅니다. (예: /학식 2학, /학식 전체)")
@discord.app_commands.describe(식당="볼 식당 (전체 가능)", 날짜="조회할 날짜 (기본: 다음 끼니)", 끼니="특정 끼니만 보기")
@discord.app_commands.choices(
    # 식당을 첫 번째 '필수' 옵션으로 둬야 '/학식 2학'처럼 옵션 이름 없이 바로 입력된다.
    식당=[discord.app_commands.Choice(name="전체", value="전체")] +
         [discord.app_commands.Choice(name=short, value=n) for n, short in CAFETERIAS.items()],
    날짜=[discord.app_commands.Choice(name=n, value=v) for n, v in MENU_DAY_CHOICES],
    끼니=[discord.app_commands.Choice(name=m, value=m) for m in (*MEALS, "전체")],
)
async def menu_cmd(interaction: discord.Interaction,
                   식당: discord.app_commands.Choice[str],
                   날짜: discord.app_commands.Choice[str] = None,
                   끼니: discord.app_commands.Choice[str] = None):
    # 학교 서버가 느리면 3초 응답 제한을 넘기므로 먼저 defer한다.
    await interaction.response.defer(ephemeral=True)
    today = _today_kst()
    auto = 날짜 is None and 끼니 is None   # 날짜·끼니를 안 고르면 다음 끼니를 자동으로
    only = None if 식당.value == "전체" else 식당.value
    if auto:
        offset, meal = _upcoming_meal(datetime.now(KST))
        day = today + timedelta(days=offset)
    else:
        day = _menu_day(날짜.value, today) if 날짜 else today
        meal = 끼니.value if 끼니 and 끼니.value != "전체" else None
    try:
        menu = await fetch_menu(day)
    except Exception as e:
        print(f"학식 조회 실패: {e!r}")
        await interaction.followup.send("학식 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
        return

    def pick(info, meal):
        return [m for m in info['menus'] if not meal or m['meal'] == meal]

    targets = {n: info for n, info in menu.items() if not only or n == only}
    # 자동으로 고른 끼니를 운영하는 곳이 없으면(예: 석식) 그날 전체를 보여준다.
    if auto and not any(pick(info, meal) for info in targets.values()):
        meal = None

    weekday = "월화수목금토일"[day.weekday()]
    title = " ".join(filter(None, ("🍚", f"{day.isoformat()}({weekday})", meal, "학식")))
    embed = discord.Embed(title=title, url=MENU_URL, color=0x27AE60)
    for name, info in targets.items():
        menus = pick(info, meal)
        if menus:
            blocks = [
                f"**{m['meal']}·{m['target']}** {m['title']}\n" + " · ".join(m['items'])
                for m in menus
            ]
            value = "\n".join(blocks)
            if len(value) > 1024:   # 임베드 필드 값 상한
                value = value[:1023] + "…"
        elif info['link']:
            value = f"[메뉴운영내역 보기]({info['link']})"
        else:
            continue
        embed.add_field(name=CAFETERIAS.get(name, name), value=value, inline=False)

    # 제1학생회관 링크는 날짜와 무관하게 항상 있으므로, 실제 메뉴가 있었는지로 판단한다.
    if not any(pick(info, meal) for info in targets.values()):
        where = f"{식당.name}은(는) " if only else ""
        embed.description = f"{where}운영하지 않거나 식단이 아직 등록되지 않았습니다."
    if auto and meal:
        embed.set_footer(text="지금 시각 기준 다음 끼니만 표시 · 끼니:전체로 모두 보기")
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
    if not health_check.is_running():
        health_check.start()

@client.event
async def on_guild_join(guild: discord.Guild):
    ch = await ensure_channel(guild)
    if ch:
        welcome_embed = discord.Embed(
            title="🤖 공지 알림 봇이 추가되었습니다!",
            description=f"앞으로 학부 공지를 이 채널로 전달해 드릴게요.\n자동으로 {CHECK_MIN}분마다 업데이트됩니다.",
            color=0x2ECC71
        )
        await ch.send(embed=welcome_embed)

client.run(TOKEN)