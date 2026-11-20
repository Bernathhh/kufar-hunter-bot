# 🏹 Kufar Hunter Telegram Bot

Быстрый и удобный бот для мониторинга объявлений на **Kufar.by** по заданным критериям (ключевые слова, цена, регион, районы, категория или готовая ссылка с фильтрами) с **мгновенными уведомлениями в Telegram**.

---

## 🛠 Управление фильтрами прямо из Telegram

| Команда | Описание |
|---|---|
| `/start`, `/help` | Запуск и главное меню |
| `/add <ссылка с kufar.by>` | **Самый удобный способ:** настройте любые фильтры на сайте в браузере, скопируйте ссылку и отправьте боту |
| `/add <запрос> [цена]` | Добавить по ключевым словам (напр. `/add ps5 slim 1200 2000`) |
| `/list` | Показать все ваши активные поиски |
| `/details <ID>` | Детальный просмотр параметров поиска (районы, категория, лимиты цен) |
| `/del <ID>` | Удалить поиск (или нажать на ссылку `/del_1`) |
| `/interval <сек>` | Изменить скорость проверки (напр. `/interval 30` — проверять каждые 30 секунд) |
| `/check` | Принудительно запустить проверку прямо сейчас |
| `/status` | Статистика работы (аптайм, число проверок, отправленных объявлений) |

---

## ☁️ Бесплатный хостинг с GitHub Student Developer Pack

В **GitHub Student Developer Pack** входит:

### 🥇 Вариант 1: DigitalOcean ($200 кредитов на 1 год) — Рекомендуется!
1. Перейдите на [education.github.com/pack](https://education.github.com/pack) и активируйте оффер **DigitalOcean** ($200 credit).
2. В панели DigitalOcean создайте **Droplet**:
   - **OS:** Ubuntu 24.04 LTS
   - **Plan:** Basic ($4/mo или $6/mo — списывается с бесплатных $200, хватит на весь год!)
   - **Datacenter:** Frankfurt или Amsterdam
3. Подключитесь по SSH к дроплету:
   ```bash
   ssh root@IP_ВАШЕГО_СЕРВЕРА
   ```
4. Установите git и склонируйте/скопируйте проект:
   ```bash
   git clone <ВАШ_РЕПОЗИТОРИЙ> /root/kufar_hunter_bot
   cd /root/kufar_hunter_bot
   ```
5. Запустите через systemd (чтобы бот работал 24/7 и сам перезапускался при перезагрузке):
   ```bash
   cp kufar_bot.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now kufar_bot
   ```
   Проверить статус:
   ```bash
   systemctl status kufar_bot
   ```

*(Либо запустите через Docker в одну команду: `docker compose up -d`)*

---

### 🥈 Вариант 2: Microsoft Azure for Students ($100 + бесплатный сервер на 12 мес)
1. Активируйте оффер **Microsoft Azure** в Student Pack (или на [azure.microsoft.com/free/students](https://azure.microsoft.com/free/students)).
2. Создайте виртуальную машину Linux (размер **B1s** — он входит в бесплатный уровень на 12 месяцев).
3. Запустите бота точно так же через `systemd` или `python3 bot.py`.

---

## 💻 Локальный запуск на Mac (прямо сейчас)

Если хотите запустить бота прямо сейчас на своем компьютере:
```bash
cd /Users/bernat/.gemini/antigravity/scratch/kufar_hunter_bot
python3 bot.py
```
После запуска напишите `/start` боту [@kufarhuntbot](https://t.me/kufarhuntbot) в Telegram!
