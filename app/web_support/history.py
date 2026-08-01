TITLES = {
    "REVIEW_APPROVED": "Загрузка одобрена", "REVIEW_REJECTED": "Загрузка отклонена",
    "USER_BLOCKED": "Пользователь заблокирован", "USER_UNBLOCKED": "Пользователь разблокирован",
    "USER_ROLE_CHANGED": "Роль пользователя изменена", "ALL_SESSIONS_REVOKED": "Все сессии завершены",
    "CONTAMINATION_REPORTED": "Выявлено загрязнение", "VIOLATION_RESOLVED": "Нарушение исправлено",
    "SCORE_ADJUSTED": "Баланс изменён", "INCIDENT_CREATED": "Инцидент создан",
    "INCIDENT_UPDATED": "Инцидент изменён", "COMPOSTER_CREATED": "Компостер создан",
    "COMPOSTER_UPDATED": "Настройки компостера изменены", "COMPOSTER_DELETED": "Компостер удалён",
    "MAINTENANCE_MODE_CHANGED": "Режим обслуживания изменён", "MAINTENANCE_RECORDED": "Работа по обслуживанию записана",
    "FULL_REPORTED": "Бак отмечен заполненным", "FULL_REPORT_CLEARED": "Бак отмечен освобождённым",
    "ENGINEERING_PHOTO_UPLOADED": "Инженерное фото загружено", "STORAGE_ANNOTATIONS_UPDATED": "Разметка изображения обновлена",
    "ML_DATASET_FROZEN": "Версия датасета зафиксирована", "ML_TRAINING_QUEUED": "Обучение модели запущено",
    "ML_MODEL_DEPLOYED": "Модель развёрнута", "FIRMWARE_UPLOADED": "Прошивка добавлена",
    "FIRMWARE_DELETED": "Прошивка удалена", "WEB_FLASH_COMPLETED": "Прошивка ESP завершена",
    "WEB_FLASH_FAILED": "Прошивка ESP завершилась ошибкой", "DEBUG_OPEN_REQUESTED": "Запрошено открытие замка в отладке",
    "DEBUG_CLOSE_REQUESTED": "Запрошено закрытие замка в отладке", "OPEN_REQUESTED": "Запрошено открытие замка",
    "CLOSE_REQUESTED": "Запрошено закрытие замка", "PROVISIONING_RESUMED": "Настройка компостера продолжена",
    "PROVISIONING_COMPLETED": "Настройка компостера завершена", "TELEMETRY_RECORDED": "Телеметрия записана",
    "PASSWORD_CHANGED": "Пароль изменён", "USER_REGISTERED": "Пользователь зарегистрирован",
    "USER_AUTO_BLOCKED": "Пользователь заблокирован автоматически", "VIOLATION_CANCELLED": "Нарушение отменено",
}


def history_presentation(action: str) -> tuple[str, str, str]:
    title = TITLES.get(action, action.replace("_", " "))
    if action.startswith("REVIEW_"):
        return title, "Проверка", "review"
    if action.startswith("USER_BLOCK") or action == "USER_UNBLOCKED":
        return title, "Доступ", "access"
    if "ROLE" in action:
        return title, "Роль", "role"
    if "CONTAMINATION" in action or "VIOLATION" in action:
        return title, "Нарушение", "violation"
    equipment_actions = {"OPEN_REQUESTED", "CLOSE_REQUESTED", "PROVISIONING_RESUMED", "PROVISIONING_COMPLETED", "TELEMETRY_RECORDED"}
    if any(value in action for value in ("INCIDENT", "MAINTENANCE", "COMPOSTER", "FULL_", "DEBUG_")) or action in equipment_actions:
        return title, "Оборудование", "equipment"
    if "ML_" in action:
        return title, "ML", "ml"
    if "FIRMWARE" in action or "FLASH" in action:
        return title, "Прошивка", "firmware"
    if "PHOTO" in action or "STORAGE_" in action:
        return title, "Файл", "storage"
    if "SESSION" in action:
        return title, "Сессии", "session"
    return title, "Система", "system"
