"""
Единственное место, где перечислены все фичи и их обработчики.
Добавление нового модуля — это одна новая запись здесь плюс новый файл в services/.
Сами сервисы друг про друга не знают.
"""
from services import seller_service, testing_service

MODULE_HANDLERS = {
    "testing": testing_service.handle,
    "seller": seller_service.handle,
}
