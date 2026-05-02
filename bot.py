# bot.py — pip install "discord.py>=2.4" beautifulsoup4 aiohttp
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

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
CHECK_MIN = 10
CHANNEL_NAME = "공지알림"            # 자동 생성



CATEGORIES = {
    "학사공지":   "https://comai.cnu.ac.kr/computer/notice/bachelor.do",
    "일반공지":   "https://comai.cnu.ac.kr/computer/notice/notice.do",
    "취업정보":   "https://comai.cnu.ac.kr/computer/notice/job.do",
    "사업단공지": "https://comai.cnu.ac.kr/computer/notice/project.do",
}


# 디스코드 클라이언트 초기화
intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)

# ---------- 채널 자동 확보 ----------

async def ensure_channel(guild: discord.Guild):
    
    # 1) 이미 있으면 그대로
    cid = db.get_guild_channel(guild.id)
    if cid:
        ch = guild.get_channel(cid)
        if ch: 
            return ch

    for ch in guild.text_channels:

        # 2) 같은 이름이 있으면 재사용
        if ch.name == CHANNEL_NAME and ch.permissions_for(guild.me).send_messages:
            db.upsert_guild_channel(guild.id, ch.id)
            return ch

    # 3) 없으면 새로 생성
    try:
        ch = await guild.create_text_channel(
            CHANNEL_NAME, topic="충남대 컴공지능 공지 자동 알림",
            reason="공지 봇 초기 설정"
        )
        db.upsert_guild_channel(guild.id, ch.id)
        return ch
    except discord.Forbidden:
        fallback = guild.system_channel
        if not (fallback and fallback.permissions_for(guild.me).send_messages):
            fallback = next(
                (c for c in guild.text_channels
                 if c.permissions_for(guild.me).send_messages),
                None
            )
        if fallback:
            db.upsert_guild_channel(guild.id, fallback.id)
            # 4) 권한 없을 경우
            try:
                await fallback.send(
                    f"⚠️ `#{CHANNEL_NAME}` 채널을 만들 권한이 없어 이 채널로 알림을 보냅니다."
                )
            except discord.HTTPException: pass
        return fallback


# ---------- 크롤 ----------

# aiohttp로 비동기 get
async def fetch(session, url):
    async with session.get(url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'}) as r:
        return await r.text()


def parse(html, base):
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    for row in soup.select('table.board-table tbody tr'):
        a = row.select_one('td.b-td-left a')
        if not a:
            continue
        m = re.search(r'articleNo=(\d+)', a.get('href', ''))
        if not m:
            continue
        items.append({
            'id': m.group(1),
            'title': a.get_text(strip=True),
            'link': urljoin(base, a['href']),
            'sticky': 'b-top-box' in (row.get('class') or []),
        })
    return items

@tasks.loop(minutes=CHECK_MIN)
async def crawl():
    async with aiohttp.ClientSession() as session:
        broadcasts = []      # [(category, article_no, message), ...]
        for cat, url in CATEGORIES.items():
            try:
                html = await fetch(session, url)
            except Exception as e:
                print(f"crawl fail {cat}: {e}")
                continue
            items = parse(html, url)
            if not items:
                continue

            # 첫 실행: baseline만 저장하고 알림은 스킵
            if db.is_baseline_empty(cat):
                db.mark_seen(cat, items)
                continue

            new_ids = db.filter_new(cat, [n['id'] for n in items])
            new_items = [n for n in items if n['id'] in new_ids]

            for n in reversed(new_items):  # 오래된 순
                tag = "📌공지" if n['sticky'] else "🆕 새 글"
                msg = f"🔔 **[{cat}]** {tag}\n{n['title']}\n{n['link']}"
                broadcasts.append((cat, n['id'], msg))

            db.mark_seen(cat, new_items)
            db.trim_old(cat, keep=500)

        if not broadcasts:
            return

        for guild_id, channel_id in db.all_guild_channels():
            guild = client.get_guild(guild_id)
            if not guild: continue
            ch = guild.get_channel(channel_id) or await ensure_channel(guild)
            if not ch: continue
            for cat, art_no, msg in broadcasts:
                try:
                    await ch.send(msg)
                    db.log_notification(cat, art_no, guild_id, True)
                except discord.HTTPException as e:
                    print(f"send fail guild={guild_id}: {e}")
                    db.log_notification(cat, art_no, guild_id, False)

# ---------- 이벤트 ----------


@client.event
async def on_ready():
    db.init()                     # ← 첫 실행 시 테이블 자동 생성
    print(f"✅ Logged in as {client.user}  | servers: {len(client.guilds)}")
    # 이미 들어와 있는 서버들도 채널 확보
    for g in client.guilds:
        await ensure_channel(g)
    if not crawl.is_running():
        crawl.start()


@client.event
async def on_guild_join(guild: discord.Guild):
    ch = await ensure_channel(guild)
    if ch:
        await ch.send(
            "👋 안녕하세요! 충남대 컴공지능 학부 공지 자동 알림 봇입니다.\n"
            "이 채널로 새 공지가 올라올 때마다 자동으로 알림이 옵니다. (10분 주기)"
        )

client.run(TOKEN)
