# GSPro: актуальные инструкции

Документы для текущего комплекта GSPro и полного S3-снимка. Новый сервер - Ubuntu 24.04 x86_64, сохранённые образы - для `gspro.store`.

## Начать с самого простого способа

1. Откройте [инструкцию интерактивного мастера](server-setup/Инструкция_настройки_GSPro.md), [PDF](server-setup/Инструкция_настройки_GSPro.pdf).
2. Скачайте [GSPro-Server-Setup.zip](GSPro-Server-Setup.zip) на новый VPS командами из инструкции и проверьте [SHA256](GSPro-Server-Setup.zip.sha256).
3. Получите собственный полный recovery-kit и снимок из S3, пройдите опрос и выполните восстановление с проверками.

## Восстановление и обслуживание

- [Сверка средств восстановления на VPS и обоих S3](docs/recovery/README.md).
- [Ручное независимое восстановление из S3](docs/recovery/Восстановление_из_S3.md), [PDF](docs/recovery/Восстановление_из_S3.pdf).
- [Публичные инструменты восстановления ZIP](GSPro-Recovery-Tools.zip), [SHA256](GSPro-Recovery-Tools.zip.sha256).
- [Все задачи эксплуатации](docs/OPERATIONS_GUIDE.md).
- [Запуск после сбоя](docs/START_AND_CHECK_RU.md), [диагностика окружения](docs/CHECK_ENV_RU.md).
- [Сайт, аккаунт, веб-админка и Mini App](docs/ADMIN_GUIDE.md), [PDF](docs/pdf/GSPro_Админка_Руководство.pdf).
- [Перевод RU/EN](docs/TRANSLATIONS_RU_EN.md), [архитектура](docs/architecture/README.md).
- [Каталог всех актуальных документов](DOCUMENTS.md).

Здесь публикуются актуальные инструкции и инструменты. Закрытые env, ключи восстановления, credentials, база и Docker-образы находятся в собственном S3 владельца. Источник кода - частный `nightengineer/GSPro`; этот репозиторий содержит документацию. Условия и границы проверки описаны в [PUBLICATION.md](PUBLICATION.md).
