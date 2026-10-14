# bfl2openai 反代（回环 :39100；服务侧默认免鉴权，见 README「暴露面」）
proxy_pass http://127.0.0.1:39100;
proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_buffering off;           # 生成是长请求（1k 约 30s；4k 复合最长 2-3 分钟）
proxy_read_timeout 300s;
proxy_send_timeout 300s;
client_max_body_size 64m;      # multipart 图片上传（edits 门为主；覆盖 4096² PNG ≈27MB + 余量）
