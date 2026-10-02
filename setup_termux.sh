#!/data/data/com.termux/files/usr/bin/bash
# Настройка студии Cubism D в Termux

set -e

echo "=== Обновление пакетов ==="
pkg update -y && pkg upgrade -y

echo "=== Установка зависимостей ==="
pkg install -y python nodejs-lts git openssh termux-api

echo "=== Python пакеты ==="
pkg install python-pip -y
pip install python-telegram-bot==21.0.1 python-dotenv==1.0.1

echo "=== Codex CLI ==="
npm install -g @mmmbuto/codex-cli-termux

echo "=== Разрешение на доступ к памяти ==="
termux-setup-storage

echo ""
echo "✅ Установка завершена."
echo ""
echo "Дальше:"
echo "  1. codex login — авторизация"
echo "  2. Отредактируй ~/studio/.env"
echo "  3. bash ~/studio/start.sh"
