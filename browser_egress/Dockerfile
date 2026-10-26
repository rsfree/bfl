FROM python:3.13-slim-bookworm
WORKDIR /app
# 系统 chromium（~120MB）替代 playwright 自带浏览器（~1.5GB）
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-liberation ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi==0.115.* uvicorn==0.32.* pydantic==2.* playwright==1.49.*
COPY main.py /app/main.py
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
EXPOSE 8011
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8011", "--workers", "1"]
