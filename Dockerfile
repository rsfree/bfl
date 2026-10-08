# bfl2openai · ai-prod 部署镜像（服务器构建路径；无 GHCR 镜像）
# 运行方式：源码就位（非 pip install），.env 由卷挂到 /app/bfl2openai/.env
#   —— upstream/upscale 的 .env 加载器按「包目录」找文件，挂载点必须一致。
FROM python:3.13-slim

WORKDIR /app
COPY bfl2openai/requirements.txt ./bfl2openai/requirements.txt
RUN pip install --no-cache-dir -r bfl2openai/requirements.txt
COPY bfl2openai ./bfl2openai

EXPOSE 8000
CMD ["uvicorn", "bfl2openai.server:app", "--host", "0.0.0.0", "--port", "8000"]
