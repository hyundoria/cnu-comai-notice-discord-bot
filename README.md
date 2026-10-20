# cnu-comai-notice-discord-bot

충남대학교 컴퓨터인공지능학부의 주요 공지사항을 실시간으로 확인하여 디스코드 서버로 자동 전송해주는 봇입니다.

## 주요 기능
- **실시간 공지 크롤링**: 학사·일반·취업·사업단 공지를 10분마다 확인해 신규 공지가 있으면 디스코드로 알림을 보냅니다.
- **사이버캠퍼스 알림**: `.env` 계정의 사이버캠퍼스 Todo(과제·공지) 항목을 감지해 **봇 소유자에게 DM으로만** 알립니다. 개인 수강 정보라 서버 채널에는 올리지 않습니다. *(현재 로그인 실패로 동작하지 않음 — `/상태`에 ⚠️로 표시)*
- **마감 리마인더**: 공지 제목에서 마감일을 추출해 D-1/당일에 자동 리마인드합니다. `/마감`으로 다가오는 일정을 조회할 수 있습니다.
- **전송 통계**: `/통계`로 카테고리별 전송 성공/실패 및 감지 현황을 확인합니다.
- **학식 메뉴**: `/학식 2학`, `/학식 전체`처럼 식당을 골라 학생식당 메뉴를 조회합니다. 날짜·끼니를 안 고르면 지금 시각 기준 다음 끼니를 보여주고, `날짜`(오늘·내일·모레·요일), `끼니` 옵션으로 골라 볼 수 있습니다.
- **상태 점검**: `/상태`로 게시판별 마지막 크롤 성공 시각과 최근 오류를 봅니다. 3시간 넘게 성공이 없는 항목이 생기면 봇 소유자에게 DM으로 알립니다.

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
docker build -t bot .

# 컨테이너 실행
docker run -d \
    --name bot \
    --env-file .env \
    -v $(pwd)/data:/app/data \
    --restart always \
    --init \
    --pids-limit 256 \
    --memory 768m --memory-swap 1536m \
    --log-opt max-size=10m --log-opt max-file=3 \
    bot
```

> **참고**
> - `--env-file .env` 로 토큰/계정 정보를 주입합니다.
> - `--init` 은 빼면 안 됩니다. 없으면 Playwright가 띄운 Chromium 좀비 프로세스가 회수되지 않고 쌓여, 약 3주 뒤 PID가 고갈되면서 크롤이 조용히 멈춥니다. `--pids-limit` 은 그래도 새는 경우 호스트 대신 컨테이너만 막히게 하는 안전장치입니다.
> - `--log-opt` 는 컨테이너 로그를 10MB × 3개로 제한합니다(없으면 디스크를 무한정 채웁니다).
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
docker tag bot bot:prev          # 문제 시 되돌릴 수 있게 직전 이미지 보관
docker build -t bot .
docker rm -f bot
docker run -d \
    --name bot \
    --env-file .env \
    -v $(pwd)/data:/app/data \
    --restart always \
    --init \
    --pids-limit 256 \
    --memory 768m --memory-swap 1536m \
    --log-opt max-size=10m --log-opt max-file=3 \
    bot

# 로그 확인
docker logs -f bot
```
