# GSPro: действующий production

Сайт: https://gspro.store. Кабинет: https://gspro.store/account. Админка: https://gspro.store/admin.

Исходники находятся на VPS в `/opt/gspro/app`. Управление сервисами выполняется из `/opt/gspro` через `bash deploy/dc`. Инструкция перевода: `/opt/gspro/docs/TRANSLATIONS_RU_EN.md`.

Для выпуска используется `/opt/gspro/deploy/deploy-release.sh` с подготовленным каталогом исходников и новым тегом. Старые тестовые профили не используются для эксплуатации production.
