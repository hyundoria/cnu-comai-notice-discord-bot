# cnu-comai-notice-discord-bot

충남대학교 컴퓨터인공지능학부의 주요 공지사항을 실시간으로 확인하여 디스코드 서버로 자동 전송해주는 봇입니다.

## 주요 기능
- **실시간 공지 크롤링**: 학사·일반·취업·사업단 공지를 10분마다 확인해 신규 공지가 있으면 디스코드로 알림을 보냅니다.
- **사이버캠퍼스 알림**: 사이버캠퍼스 Todo(과제·공지) 항목을 감지해 알립니다. *(로그인 셀렉터 설정 필요)*
- **마감 리마인더**: 공지 제목에서 마감일을 추출해 D-1/당일에 자동 리마인드합니다. `/마감`으로 다가오는 일정을 조회할 수 있습니다.
- **전송 통계**: `/통계`로 카테고리별 전송 성공/실패 및 감지 현황을 확인합니다.

## 기술 스택
- **Language**: Python
- **Library**: discord.py, BeautifulSoup4, aiohttp, Playwright, sqlite3
- **Infra**: Docker

## 실행 방법

### 1. 환경 변수 설정

프로젝트 최상위 디렉토리에 `.env` 파일을 생성합니다. **`=` 양옆에 공백이 없어야 합니다** (Docker `--env-file`은 공백을 허용하지 않습니다).

```
DISCORD_TOKEN=your_discord_bot_token_here
ID=cyber_campus_id
PW=cyber_campus_password
```

### 2. Docker로 실행하기

```bash
# 이미지 빌드
docker build -t cnu-notice-bot .

# 컨테이너 실행
docker run -d \
    --name cnu-notice-bot \
    --env-file .env \
    -v $(pwd)/data:/app/data \
    --restart always \
    cnu-notice-bot
```

> **참고**
> - `--env-file .env` 로 토큰/계정 정보를 주입합니다.
> - DB(SQLite)는 컨테이너 내부 `/app/data/bot.db` 에 저장되므로, 재배포 시에도 데이터를 유지하려면 반드시 `-v $(pwd)/data:/app/data` 로 `data/` 디렉토리를 볼륨 마운트하세요.

### 3. 로컬에서 실행

```bash
# 가상환경 설정
python3 -m venv venv
source venv/bin/activate

# 라이브러리 설치
pip install -r requirements.txt
playwright install chromium   # 사이버캠퍼스 크롤링용 브라우저 설치

# 실행
python3 bot.py
```

## 재배포 (서버에서 최신 코드로 갱신)

```bash
# 최신 코드 받기
git pull origin main
#   ↳ 로컬 수정으로 거부되면:  git fetch origin && git reset --hard origin/main

# 기존 컨테이너 정리 후 재빌드·재실행
docker rm -f cnu-notice-bot
docker build --no-cache -t cnu-notice-bot .
docker run -d \
    --name cnu-notice-bot \
    --env-file .env \
    -v $(pwd)/data:/app/data \
    --restart always \
    cnu-notice-bot

# 로그 확인
docker logs -f cnu-notice-bot
```
