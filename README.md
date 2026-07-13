# Community Compost backend

Поддерживается Python 3.11–3.14. Pins NumPy 2.3.3 и psycopg 3.2.10 выбраны как первые версии соответствующих веток с готовыми Windows wheels для CPython 3.14, поэтому локальный C/C++ compiler не требуется.

FastAPI-сервер выпускного проекта: авторизация, карта/QR, одноразовые BLE-команды, фото, baseline-классификатор, модерация, полноценный административный API, телеметрия, аудит, баллы, нарушения и блокировка после трёх нарушений.

## Локальный запуск

```bash
docker compose up --build
docker compose exec api python -m app.seed
```

API: `http://localhost:8000`, веб-панель персонала: `/web`, Swagger: `/docs`, MinIO console: `http://localhost:9001`.

Веб-панель разделяет возможности по ролям: модерация фотографий, оборудование и телеметрия для инженера, пользователи/роли/блокировки и аудит для администратора. API онбординга компостера принимает существующий секрет прошивки либо генерирует новый и возвращает его один раз.

Тестовые аккаунты после seed: `admin@example.com / Admin123!`, `moderator@example.com / Moderator123!`, `engineer@example.com / Engineer123!`, `user@example.com / User123!`. Это только данные локальной разработки.

Без Docker: `python -m venv .venv`, установить `requirements.txt`, затем `python -m app.seed` и `uvicorn app.main:app --reload`. Тесты: `pytest -q`.

Переменные: `DATABASE_URL`, `JWT_SECRET`, `JWT_TTL_MINUTES`, `COMMAND_TTL_SECONDS`, `S3_ENDPOINT`, `S3_PUBLIC_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `ML_SERVICE_URL`.

ML-контейнер загружает фото из MinIO и применяет прозрачную эвристику цветовых областей (`LIKELY_VALID`, `LIKELY_INVALID`, `NEEDS_MANUAL_REVIEW`). Решение не заменяет модератора и подготовлено так, чтобы позже заменить реализацию `/analyze` обученной моделью без изменений Android и бизнес-процесса.

Архитектура и ограничения описаны в каталоге `docs`. Для демонстрации: запустить Compose, выполнить seed, войти пользователем, выбрать ближайший компостер, пройти OPEN, загрузить фото, выполнить CLOSE, затем отклонить проверку модератором.
