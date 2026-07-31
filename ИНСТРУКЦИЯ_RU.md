# Crypto Autobot: инструкция от запуска до Binance

## 1. Как работает бот

Каждую минуту бот проверяет, появилась ли новая закрытая четырёхчасовая свеча по
BTCUSDT, ETHUSDT и SOLUSDT. Решение принимается только по закрытым свечам.

Long-сигнал:

- EMA20 выше EMA100;
- цена выше EMA100;
- свеча закрылась выше максимума предыдущих 30 четырёхчасовых свечей;
- объём свечи выше среднего;
- ADX подтверждает наличие достаточно сильного тренда;
- волатильность ATR находится в допустимом диапазоне.

Short работает зеркально. Размер позиции рассчитывается так, чтобы при Stop Loss
потеря составила не больше заданного процента баланса.

В Demo/Live бот:

1. Проверяет Binance, One-Way Mode, доступный USDT-баланс и лимит позиций.
2. Устанавливает isolated margin и заданное плечо.
3. Отправляет рыночный вход.
4. Сразу размещает на Binance биржевые Stop Loss и Take Profit.
5. Если защитные ордера не удалось поставить, отправляет аварийное закрытие.
6. Каждый цикл проверяет, что оба защитных ордера всё ещё существуют. Если один
   исчез, закрывает позицию аварийным рыночным ордером.
7. Синхронизирует позицию и PnL с веб-панелью.

Stop Loss и Take Profit находятся на Binance, поэтому остаются активными, даже если
бот или интернет временно отключились. По умолчанию используется фиксированная
защита; trailing можно включить отдельно после проверки новых настроек.

## 2. Первый запуск на Mac

Открой Terminal и выполни:

```bash
cd "/Users/maksympiatachenko/Documents/Трейдинг"
python3 crypto_autobot/bot.py \
  --config crypto_autobot/config.example.json \
  --once
```

Если по парам появилось `no signal` или сообщение `ATR filter`, всё работает:
бот получил свечи, но входа сейчас нет.

Запусти постоянный Paper-режим:

```bash
python3 crypto_autobot/bot.py \
  --config crypto_autobot/config.example.json
```

Не закрывай это окно Terminal. Открой в браузере:

```text
http://127.0.0.1:8090
```

Остановить бота: вернись в Terminal и нажми `Control + C`.

## 3. Создание Binance Demo

Используй [официальную Binance Futures Demo Trading](https://demo.binance.com/).
Demo и основной Binance используют разные API-ключи. Актуальное описание среды есть
в [Binance Futures Quick Start](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/quick-start).

1. Войди в Binance Demo.
2. Открой раздел API Management для Demo Futures.
3. Создай API Key и Secret.
4. В настройках Futures выбери One-Way Mode, а не Hedge Mode.
5. Получи тестовые USDT, если Demo-баланс пустой.

Никому не отправляй Secret, в том числе в этот чат. Не сохраняй ключи в Python,
JSON, скриншоты или заметки.

В новом Terminal установи ключи только для текущего окна:

```bash
export BINANCE_DEMO_API_KEY="ВСТАВЬ_DEMO_API_KEY"
export BINANCE_DEMO_API_SECRET="ВСТАВЬ_DEMO_SECRET"
```

Сначала проверь подключение. Эта команда не размещает ордера:

```bash
cd "/Users/maksympiatachenko/Documents/Трейдинг"
python3 crypto_autobot/bot.py \
  --config crypto_autobot/config.demo.example.json \
  --check
```

Правильный результат содержит:

```text
"status": "ok"
"environment": "demo"
"position_mode": "one-way"
```

## 4. Запуск Demo-торговли

После успешной проверки:

```bash
python3 crypto_autobot/bot.py \
  --config crypto_autobot/config.demo.example.json \
  --enable-orders
```

Теперь бот ждёт сетап. Пока его нет, в панели будут `no signal`, `ATR filter` или
другая конкретная причина. Когда сетап появится, бот отправит Demo-вход и два
защитных ордера.

Данные Demo сохраняются отдельно:

```text
crypto_autobot/data/state_demo.json
crypto_autobot/data/trades_demo.csv
```

### Быстрый Demo-профиль для проверки сделки

Этот профиль работает по закрытым 15-минутным свечам, проверяет рынок каждые
30 секунд и держит не больше одной позиции одновременно:

```bash
caffeinate -i python3 crypto_autobot/bot.py \
  --config crypto_autobot/config.demo.scalp.example.json \
  --enable-orders
```

В интерфейсе появится блок `Проверка рыночного ордера`. Выбери пару и нажми
`Test Long` или `Test Short`. После подтверждения бот отправит небольшой MARKET
ордер только на Binance Demo и сразу добавит Stop Loss и Take Profit. Кнопка
заблокирована в Paper и Live.

Перед проверкой добавь виртуальные USDT в кошелек Binance Futures Demo. Этот
профиль нужен для проверки исполнения и сбора статистики. Он не считается
готовой прибыльной стратегией: тест на 30 днях с комиссией и проскальзыванием
был немного отрицательным.

Ручные Binance-позиции помечаются в панели звёздочкой. Бот показывает их, но не
управляет ими.

### Переключение режима в веб-панели

В верхней части панели теперь есть кнопки `Paper`, `Demo` и `Live`.

- `Paper` доступен всегда.
- `Demo` станет активным после добавления `BINANCE_DEMO_API_KEY` и
  `BINANCE_DEMO_API_SECRET`.
- Demo-ордера разрешены, только если бот запущен с `--enable-orders`.
- Режим нельзя сменить, пока бот ведёт открытую позицию.
- Баланс, позиции и статистика каждого режима хранятся отдельно.

Чтобы запустить панель в Paper и затем переключаться в Demo:

```bash
export BINANCE_DEMO_API_KEY="ВСТАВЬ_DEMO_API_KEY"
export BINANCE_DEMO_API_SECRET="ВСТАВЬ_DEMO_SECRET"
python3 crypto_autobot/bot.py \
  --config crypto_autobot/config.example.json \
  --enable-orders
```

Для управления режимами на удалённом сервере задай отдельный пароль:

```bash
export DASHBOARD_CONTROL_TOKEN="ПРИДУМАЙ_ДЛИННЫЙ_ПАРОЛЬ"
```

Тогда в панели появится поле `Код управления`. Без этого пароля удалённое
переключение заблокировано.

`Live` дополнительно требует live-ключи, параметр `--allow-live-ui` при запуске и
точную фразу подтверждения в окне интерфейса. Это разрешает только выбор профиля:
для реальных ордеров всё ещё нужны `--enable-orders` и включённая стратегия в
live-конфиге.

## 5. Telegram-уведомления

В нужном конфиге поменяй:

```json
"telegram_enabled": true
```

Перед запуском установи новый Telegram-токен и chat ID:

```bash
export TELEGRAM_BOT_TOKEN="НОВЫЙ_ТОКЕН"
export TELEGRAM_CHAT_ID="ТВОЙ_CHAT_ID"
```

Токен, который когда-либо был опубликован в чате или на скриншоте, нужно отозвать
через BotFather и заменить.

## 6. Историческая проверка

Перед Demo или после изменения настроек запусти backtest:

```bash
cd "/Users/maksympiatachenko/Documents/Трейдинг"
python3 crypto_autobot/backtest.py \
  --config crypto_autobot/config.example.json \
  --days 365
```

В расчёт включены комиссия `5 bps` на каждую сторону и проскальзывание `2 bps`
на каждый ордер. Итоговый отчёт:

```text
crypto_autobot/data/backtest_report.json
```

Смотри не только на доходность, но и на число сделок, максимальную просадку и
результаты каждой пары. Один удачный период ничего не гарантирует; настройки должны
переживать разные рыночные режимы и после этого проверяться в Demo.

## 7. Бесплатный запуск через GitHub

Это вариант без банковской карты и без постоянно включённого Mac. GitHub Actions
будит бота в `:17` и `:47` каждого часа, бот делает один проход по закрытым 4h
свечам, а Stop Loss и Take Profit после входа остаются на Binance Demo.

Это не настоящий непрерывный сервер: запуск по расписанию иногда задерживается.
Поэтому GitHub-вариант оставлен только для Binance Demo и не поддерживает Live.

### Шаг 1. Установи GitHub Desktop

Установи [GitHub Desktop](https://desktop.github.com/) и войди в свой
GitHub-аккаунт. Homebrew и команды для авторизации не нужны.

### Шаг 2. Опубликуй готовую папку с Mac

В GitHub Desktop:

1. Выбрать `File` → `Add Local Repository`.
2. Указать папку `/Users/maksympiatachenko/Documents/Трейдинг/crypto_autobot`.
3. Сделать commit с названием `Prepare Binance Demo bot`.
4. Нажать `Publish repository`.
5. Указать имя `crypto-autobot-demo`.
6. Снять галочку `Keep this code private` и подтвердить публикацию.

Публичность нужна для бесплатного GitHub Pages. В репозитории будет виден код
стратегии, но локальные состояния, ключи, баланс и размеры позиций туда не попадут.

### Шаг 3. Добавь секреты

В GitHub открой репозиторий → `Settings` → `Secrets and variables` → `Actions` →
вкладка `Secrets`. Создай три `Repository secrets` с точными именами:

```text
BINANCE_DEMO_API_KEY
BINANCE_DEMO_API_SECRET
STATE_ENCRYPTION_PASSWORD
```

В первые два вставь Demo-ключи Binance. Значение для третьего получи в Terminal:

```bash
openssl rand -hex 32
```

Скопируй результат в `STATE_ENCRYPTION_PASSWORD`. Это отдельный пароль, которым
GitHub шифрует состояние бота перед сохранением. Не отправляй ни один из этих
секретов в чат и не добавляй их в файлы репозитория.

Telegram необязателен. Если уведомления включены в конфиге, дополнительно создай:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

### Шаг 4. Оставь ордера выключенными

В том же разделе открой вкладку `Variables` и создай `Repository variable`:

```text
Name: DEMO_ORDERS_ENABLED
Value: false
```

Так первый запуск только проверит Demo-подключение и сигналы.

### Шаг 5. Включи Pages и сделай первый запуск

1. Открой `Settings` → `Pages`.
2. В `Build and deployment` выбери `Source: GitHub Actions`.
3. Открой вкладку `Actions` репозитория.
4. Слева выбери `Crypto Autobot Demo`.
5. Нажми `Run workflow` → `Run workflow`.
6. Дождись двух зелёных задач `scan` и `deploy`.

Ссылка на панель появится в завершённой задаче `deploy` и в `Settings` → `Pages`.
Панель read-only: в ней нет переключения режимов, баланса и размера позиции.

### Шаг 6. Разреши Demo-ордера

Сначала убедись, что панель показывает `Binance Demo подключён`. Затем вернись в
`Settings` → `Secrets and variables` → `Actions` → `Variables`, открой
`DEMO_ORDERS_ENABLED`, поменяй значение на:

```text
true
```

Снова вручную запусти workflow. После этого панель должна показать
`Demo-ордера включены`. Все сделки остаются виртуальными на Binance Demo.

### Ограничения бесплатного варианта

- GitHub может задержать запланированный запуск; поэтому используется время не в
  начале часа.
- В публичном репозитории расписание автоматически отключается после 60 дней без
  активности. Тогда сделай любой безопасный commit и снова включи workflow в
  `Actions`.
- Зашифрованное состояние хранится в GitHub Cache. После долгого простоя оно может
  исчезнуть, поэтому перед повторным включением проверь открытые Demo-позиции.
- IP GitHub-сервера меняется между запусками, поэтому фиксированный IP whitelist
  для Demo-ключа здесь не подойдёт. Именно поэтому в GitHub используются только
  отдельные Binance Demo-ключи, а не ключи основного аккаунта.
- Этот workflow нельзя переводить на Live. Для реальных средств нужен постоянный
  сервер, мониторинг и отдельная длительная проверка Demo.

## 8. Подключение основного Binance

Не переходи сюда, пока Demo не показал понятную статистику и приемлемую просадку.
Прибыль в Demo не гарантирует прибыль на реальном рынке.

На Binance:

1. Открой USD-M Futures.
2. Создай отдельный API-ключ только для бота.
3. Разреши чтение и Futures Trading.
4. Не разрешай вывод средств.
5. Добавь публичный IP сервера в IP whitelist.
6. Включи One-Way Mode.

Для отдельного постоянного сервера задай live-переменные только в защищённом
хранилище окружения:

```text
BINANCE_LIVE_API_KEY=ТВОЙ_LIVE_API_KEY
BINANCE_LIVE_API_SECRET=ТВОЙ_LIVE_SECRET
```

На таком сервере сначала сделай копию live-конфига:

```bash
cp /home/ubuntu/crypto_autobot/config.live.example.json \
  /home/ubuntu/crypto_autobot/config.live.json
nano /home/ubuntu/crypto_autobot/config.live.json
```

В live-примере стратегия специально выключена:

```json
"enabled": false
```

Даже после включения `true` команда без подтверждения не сможет отправлять live-ордера.

Проверка ключей без ордеров:

```bash
set -a
source /etc/crypto-autobot.env
set +a
python3 /home/ubuntu/crypto_autobot/bot.py \
  --config /home/ubuntu/crypto_autobot/config.live.json \
  --check
```

Live-запуск требует одновременно разрешение ордеров и точную фразу:

```bash
python3 /home/ubuntu/crypto_autobot/bot.py \
  --config /home/ubuntu/crypto_autobot/config.live.json \
  --enable-orders \
  --confirm-live I_UNDERSTAND_REAL_MONEY
```

Перед live нужно изменить systemd-сервис на live-конфиг, live-команду и соответствующие
переменные. Сначала начни с минимального баланса и лимитов из live-примера.

## 9. Ограничения риска по умолчанию

Paper/Demo:

- риск на сделку: 0.5%;
- максимум открытых позиций: 3;
- максимум новых сделок в день: 6;
- остановка после дневного убытка 2%;
- isolated margin;
- плечо 2x;
- Stop Loss: 2 ATR;
- Take Profit: 3 ATR.

Live-пример:

- стратегия выключена;
- риск на сделку: 0.25%;
- одна позиция одновременно;
- максимум 3 сделки в день;
- дневной лимит убытка: 1%;
- плечо 1x.

## 10. Частые сообщения

`no signal` — данные получены, но условий входа нет.

`ATR filter` — рынок сейчас слишком спокойный или слишком волатильный.

`Binance orders disabled` — подключение возможно, но запуск был без
`--enable-orders`.

`Binance disconnected` — проблема ключей, IP whitelist, One-Way Mode, времени
сервера или сети.

`external Binance position` — по паре есть ручная или чужая позиция; бот её не трогает.

`Invalid API-Key (-2015)` — обычно ключ от другой среды, нет Futures-разрешения или
IP сервера отсутствует в whitelist.

## 11. Важные файлы

```text
bot.py                         основной процесс и веб-панель
binance_futures.py             подключение к Binance
backtest.py                    историческая проверка стратегии
config.example.json            Paper
config.demo.example.json       Binance Demo
config.live.example.json       Live с безопасными ограничениями
data/state_*.json              текущее состояние
data/trades_*.csv              журнал сделок
```
