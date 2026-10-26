FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.115.* uvicorn==0.32.* pydantic==2.*
COPY main.py /app/main.py
EXPOSE 8011
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8011", "--workers", "1"]
