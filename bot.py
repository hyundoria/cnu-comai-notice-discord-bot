# bot.py — pip install "discord.py>=2.4" beautifulsoup4 aiohttp playwright
# playwright install
import os
import re
import json
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

@tasks.loop(minutes=CHECK_MIN)
async def crawl():
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

            for n in reversed(new_items):
                tag = "📌공지" if n['sticky'] else "🆕 새 글"
                embed = discord.Embed(
                    title=n['title'], url=n['link'],
                    description=f"**[{cat}]** 게시판에 {tag}가 등록되었습니다.", color=0x0054A6
                )
                broadcasts.append((cat, n['id'], embed))

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

# ---------- 이벤트 ----------

@client.event
async def on_ready():
    db.init()
    print(f"✅ Logged in as {client.user}  | servers: {len(client.guilds)}")
    for g in client.guilds:
        await ensure_channel(g)
    if not crawl.is_running():
        crawl.start()

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