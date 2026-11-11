#!/usr/bin/env bash
# Выполняется на сервере после выкладки кода (из GitHub Actions).
set -euo pipefail
cd /srv/modus/backend
ln -sfn /srv/modus/uploads uploads
[ -x .venv/bin/python ] || python3.12 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
set -a; . /srv/modus/backend.env; set +a
.venv/bin/python -m alembic upgrade head
sudo -n /usr/bin/systemctl restart modus-backend
# Ждём, пока сервис ответит; иначе деплой считается неудачным.
for i in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8021/api/health >/dev/null; then echo "Бэкенд запущен"; exit 0; fi
  sleep 1
done
echo "Бэкенд не ответил после перезапуска" >&2
sudo -n /usr/bin/journalctl -u modus-backend -n 40 --no-pager || true
exit 1
