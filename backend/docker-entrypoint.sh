#!/bin/sh

# If a custom command is passed (e.g. celery), run it directly
if [ $# -gt 0 ]; then
    exec "$@"
fi

# 已有数据卷升级代码后，补齐模型新增字段（幂等；新库由 init-db 建全表）
python manage.py migrate
python manage.py init-db
python manage.py create-admin --user admin --pwd Admin123
exec uvicorn app.main:app --host 0.0.0.0 --port 8000