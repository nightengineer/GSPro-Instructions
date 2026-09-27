# Примечания к сборке

Основной сайт работает на VPS в составе `/opt/gspro`. Исходники: `/opt/gspro/app`; production-сборка: `/opt/gspro/deploy/Dockerfile`.

Управление: `cd /opt/gspro` и `bash deploy/dc ps`. Выпуск изменений выполняется через `deploy/deploy-release.sh`. Тестовые Dockerfile и Compose-профили не являются инструкцией запуска действующего сайта.
