# BLE-протокол v1

Сервис `7d2f0001-7b0b-4d46-9b18-5fb29f52a001` содержит write-характеристику команд `...0002` и notify-характеристику ответов `...0003`. UTF-8 JSON передаётся одним сообщением при достаточном MTU; клиент запрашивает MTU 512. Реализация ограничивает вход 1024 байт.

Команда содержит `v`, `commandId`, `composterId`, `sessionId`, `action` (`OPEN`/`CLOSE`), `issuedAt`, `expiresAt`, `nonce`, `signature`. Ответ содержит `v`, `commandId`, `deviceId`, `status`, `state`, `message`.

Ошибки: `INVALID_SIGNATURE`, `EXPIRED`, `WRONG_DEVICE`, `REPLAY_DETECTED`, `UNSUPPORTED_ACTION`, `LOCK_FAILURE`, `INVALID_FORMAT`. Подпись вычисляется над объектом без поля `signature`, ключи сортируются, пробелы отсутствуют.

