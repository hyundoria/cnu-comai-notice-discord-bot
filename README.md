# cnu-comai-notice-discord-bot

충남대학교 컴퓨터인공지능학부의 주요 공지사항을 실시간으로 확인하여 디스코드 서버로 자동 전송해주는 봇입니다.

## 주요 기능
- **실시간 공지 크롤링**: 학사,일반,취업,사업단 공지를 10분마다 확인해 신규 공지가 있을경우 디스코트 알림을 보냅니다.

## 기술 스택
- **Language**: Python 3.8
- **Library**: discord.py, BeautifulSoup4, aiohttp, sqlte3
- **Infra**: Docker

## 실행 방법

1. 프로젝트 최상위 디렉토리에 .env 파일을 생성하고 디스코드 봇 토큰을 입력합니다.
   
   DISCORD_TOKEN=your_discord_bot_token_here

2. Docker로 실행하기

   # 이미지 빌드
   docker build -t cnu-notice-bot .

   # 컨테이너 실행
   docker run -d \
       --name <컨테이너 이름> \
       -v $(pwd)/bot.db:/app/bot.db \
       --restart always \
       cnu-notice-bot

3. 로컬에서 실행

   # 가상환경 설정
   python3 -m venv venv
   source venv/bin/activate

   # 라이브러리 설치
   pip install -r requirements.txt

   # 실행
   python3 bot.py
