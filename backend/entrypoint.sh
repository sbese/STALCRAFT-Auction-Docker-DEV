#!/bin/sh
cd ./scaw
python manage.py makemigrations
python manage.py migrate

# Первоначальное заполнение базы данных, для работы демо-версии
# ITEM_COUNT=$(PGPASSWORD=$POSTGRES_PASSWORD psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USERNAME -d $POSTGRES_DATABASE_NAME -t -c "SELECT COUNT(*) FROM public.auction_item" | tr -d ' ')
# SALEHISTORY_COUNT=$(PGPASSWORD=$POSTGRES_PASSWORD psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USERNAME -d $POSTGRES_DATABASE_NAME -t -c "SELECT COUNT(*) FROM public.auction_salehistory" | tr -d ' ')

# if [ "$SALEHISTORY_COUNT" -eq "0" ] && [ "$ITEM_COUNT" -eq "0" ]; then
#     PGPASSWORD=$POSTGRES_PASSWORD psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USERNAME -d $POSTGRES_DATABASE_NAME -f ../auction_item.sql
#     PGPASSWORD=$POSTGRES_PASSWORD psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USERNAME -d $POSTGRES_DATABASE_NAME -f ../auction_salehistory.sql
# fi

# Создать суперпользователя, если он не существует
python manage.py shell <<'PY'
import os
from django.contrib.auth import get_user_model

User = get_user_model()
username = os.environ.get('DJANGO_SUPERUSER_USERNAME')
password = os.environ.get('DJANGO_SUPERUSER_PASSWORD')

if not User.objects.filter(username=username).exists():
    User.objects.create_superuser(username=username, password=password)
PY

# Сборщик истории стартует фоновым потоком внутри веб-процесса (auction/taskrunner.py).
# Переменная выставляется только здесь, чтобы manage.py-команды выше его не запускали.
export COLLECTOR_AUTOSTART=${COLLECTOR_AUTOSTART:-1}

# На Render (или при USE_GUNICORN=1) запускаем прод-сервер.
# ВАЖНО: ровно 1 worker - раннер фоновых задач живет внутри процесса,
# несколько воркеров запустят несколько сборщиков.
if [ -n "$RENDER" ] || [ "$USE_GUNICORN" = "1" ]; then
    python manage.py collectstatic --noinput
    exec gunicorn scaw.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 8 --timeout 120
else
    exec python manage.py runserver 0.0.0.0:8000
fi
