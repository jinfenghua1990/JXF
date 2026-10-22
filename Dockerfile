
# ---- 门禁：前端 JS 语法检查（失败则构建终止，旧镜像继续服务） ----
FROM node:20-alpine AS jslint
COPY backend/app/static /check/static
COPY tools/check_inline_js.js /check/check.js
RUN node /check/check.js /check/static

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PYTHONPATH=/app/backend

WORKDIR /app

# 依赖层单独缓存，改代码不会触发重装
# 默认走清华源（国内 NAS 构建快）；CI 在境外，构建时用 --build-arg 覆盖为 pypi.org
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r /app/requirements.txt

COPY VERSION /app/VERSION
COPY backend /app/backend

# 静态文件以通过 JS 门禁校验的副本为准
COPY --from=jslint /check/static /app/backend/app/static
# 门禁：Python 语法检查
RUN python -m compileall -q /app/backend/app

RUN mkdir -p /app/data/uploads /app/data/backups

EXPOSE 8021

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8021/healthz',timeout=4).read()"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8021"]
