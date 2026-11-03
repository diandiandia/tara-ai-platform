#!/bin/sh

# If a custom command is passed (e.g. celery), run it directly
if [ $# -gt 0 ]; then
    exec "$@"
fi

# 已有数据卷升级代码后，补齐模型新增字段（幂等；新库由 init-db 建全表）
python manage.py migrate
python manage.py init-db
# 首次启动从仓库 fixtures 导入固定 CSR 目录（已有生效目录则跳过）
python manage.py import-csr-catalog --if-missing \
    || echo "[WARN] 固定 CSR 目录自动导入失败，可在系统配置中手工导入"
python manage.py create-admin --user admin --pwd Admin123
exec uvicorn app.main:app --host 0.0.0.0 --port 8000