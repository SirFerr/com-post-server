# API flow

1. `POST /auth/login` возвращает JWT.
2. `GET /composters` возвращает точки карты.
3. `POST /composters/{id}/access` проверяет блокировку, доступность и расстояние, создаёт сессию и OPEN.
4. `POST /sessions/{id}/ack` фиксирует результат устройства и защищён от повторного подтверждения.
5. `POST /sessions/{id}/photo` загружает изображение, создаёт review и возвращает CLOSE.
6. `GET /moderation/reviews` и `POST /moderation/reviews/{id}` реализуют ручную модерацию.

