FROM python:3.13-slim-bookworm
WORKDIR /app
# 系统 chromium（~120MB）替代 playwright 自带浏览器（~1.5GB）
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-liberation ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi==0.115.* uvicorn==0.32.* pydantic==2.* playwright==1.49.* pillow==11.*
COPY main.py /app/main.py
COPY socksbridge.py /app/socksbridge.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
EXPOSE 8011
CMD ["/app/entrypoint.sh"]
