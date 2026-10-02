#!/data/data/com.termux/files/usr/bin/bash
# Запуск студии с блокировкой сна

cd "$(dirname "$0")"

# Не давать телефону уснуть
termux-wake-lock

# Запуск бота
python bot.py

# При выходе — снять блокировку
termux-wake-unlock