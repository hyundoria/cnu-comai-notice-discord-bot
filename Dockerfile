# 1. 마이크로소프트의 Playwright 공식 파이썬 이미지를 베이스로 사용
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# 2. 작업 디렉토리 설정
WORKDIR /app

# 3. 필요한 시스템 패키지 설치 (sqlite3 등)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 4. 종속성 파일 복사 및 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. 소스 코드 및 .env 파일 복사
COPY . .

# 6. 봇 실행 명령어 (파이썬 버퍼링 비활성화로 로그 확인 용이하게 설정)
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]