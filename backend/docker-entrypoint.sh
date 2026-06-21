#!/bin/sh

# If a custom command is passed (e.g. celery), run it directly
if [ $# -gt 0 ]; then
    exec "$@"
fi

python manage.py init-db
python manage.py create-admin --user admin --pwd Admin123
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
