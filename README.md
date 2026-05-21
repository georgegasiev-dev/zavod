# Новатор — Платёжный мониторинг: Backend

## Быстрый деплой на Railway (рекомендуется)

### Шаг 1 — Загрузи код на GitHub
1. Создай репозиторий на github.com (назови `novator-backend`)
2. Загрузи все файлы из этой папки

### Шаг 2 — Задеплой на Railway
1. Зайди на railway.app → "New Project" → "Deploy from GitHub"
2. Выбери репозиторий `novator-backend`
3. В разделе Variables добавь все переменные из config.env

### Шаг 3 — Email-воркер
1. В Railway → Add Service → New Service → Worker
2. Команда: `python email_watcher.py`
3. Те же переменные окружения

---

## API Endpoints

| Метод | URL | Описание | Авторизация |
|-------|-----|----------|-------------|
| GET | /api/data | Все данные | нет |
| GET | /api/data?month=Май | Данные за месяц | нет |
| GET | /api/status | Статус последней загрузки | нет |
| POST | /api/upload | Загрузить выписку | Basic Auth |
| GET | /api/health | Проверка | нет |

## Ручная загрузка выписки

```bash
curl -X POST https://your-app.railway.app/api/upload \
  -u admin:novator2026 \
  -F "file=@выписка.xlsx" \
  -F "month=Май"
```

## Настройка Gmail App Password

1. myaccount.google.com → Безопасность
2. Включи двухфакторную аутентификацию
3. Пароли приложений → создай для "Почта"
4. Скопируй 16-значный код → в EMAIL_PASS
