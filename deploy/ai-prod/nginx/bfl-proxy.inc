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
# 收请求体的两读间隔上限。默认 60s：手机端/弱网上传多张大图（每张 3~5MB）时，
# 只要出现一个 >60s 的空档 nginx 就断连返回 408，请求根本到不了应用层
# （2026-10-20 实测：本机上行 14KB/s 传 5MB → 408 且容器日志无记录）。
client_body_timeout 300s;

