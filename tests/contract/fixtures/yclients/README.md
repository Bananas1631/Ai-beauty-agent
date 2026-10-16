# Фикстуры YCLIENTS

Ответы API в формате документации YCLIENTS с вымышленными данными: салон «Студия Лаванда»,
мастера и телефоны придуманы. Живых запросов ещё не было — нет партнёрского токена.

Когда токен появится, заменить файлы настоящими ответами 1–2 публичных филиалов,
предварительно удалив персональные данные (телефоны, имена клиентов, e-mail),
и прогнать `make test`: упавший контрактный тест покажет, где документация разошлась с API.

| Файл | Метод |
|---|---|
| company.json | GET company/{id} |
| book_services.json | GET book_services/{id} |
| book_staff.json | GET book_staff/{id} |
| book_dates.json | GET book_dates/{id} |
| book_times.json | GET book_times/{id}/{staff_id}/{date} |
| book_check_ok.json | POST book_check/{id} — время свободно |
| book_check_busy.json | POST book_check/{id} — время занято (HTTP 422) |
| book_code_required.json | POST book_record/{id} — нужен код из SMS (HTTP 422) |
| book_record.json | POST book_record/{id} — запись создана (HTTP 201) |
| records.json | GET records/{id} — записи филиала (user-токен) |
| records_create.json | POST records/{id} — запись от имени салона (user-токен) |
| record.json | GET record/{id}/{record_id} — одна запись (user-токен) |
| record_deleted.json | GET record/{id}/{record_id} — запись отменена (`deleted: true`) |
| error_401.json | любой метод — неверный токен |
| error_404.json | запись или филиал не найдены |
| error_429.json | любой метод — превышен лимит |
