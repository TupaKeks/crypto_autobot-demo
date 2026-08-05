# Crypto Autobot: инструкция от запуска до Binance

## 1. Как работает бот

Бот каждые 15 секунд проверяет, появилась ли новая закрытая 15-минутная свеча на одной
из 10 выбранных USDT-пар. Решение принимается только по закрытым свечам.

Short-сигнал появляется, когда EMA48 ниже EMA144, медленная EMA наклонена вниз,
цена откатила к EMA21, RSI вернулся ниже 55, а подтверждающая свеча, ADX, объём
и ATR прошли фильтры. Long работает зеркально. Поскольку Long оказался слабее
на истории, его риск ограничен `0.025%` баланса против `0.15%` для Short.
Stop Loss равен `1.8 ATR`, Take Profit `2.8 ATR`, номинальный RR `1:1.56`.

В Demo/Live бот:

1. Проверяет Binance, One-Way Mode, доступный USDT-баланс и лимит позиций.
2. Устанавливает isolated margin и заданное плечо.
3. Ставит post-only лимитный вход на откате `0.1 ATR` и отменяет его через одну свечу.
4. После заполнения сразу ставит `STOP_MARKET closePosition` и post-only
   `LIMIT reduceOnly` Take Profit.
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
python3 -m venv crypto_autobot/.venv
crypto_autobot/.venv/bin/python -m pip install -r crypto_autobot/requirements.txt
crypto_autobot/.venv/bin/python crypto_autobot/bot.py \
  --config crypto_autobot/config.paper.asymmetric-15m.example.json \
  --once
```

Если по парам появилось `no signal` или сообщение `ATR filter`, всё работает:
бот получил свечи, но входа сейчас нет.

Запусти постоянный Paper-режим:

```bash
crypto_autobot/.venv/bin/python crypto_autobot/bot.py \
  --config crypto_autobot/config.paper.asymmetric-15m.example.json
```

Не закрывай это окно Terminal. Открой в браузере:

```text
http://127.0.0.1:8090
```

Бейдж `Цикл #...: OK` означает, что сканирование действительно завершалось
недавно. Адрес `http://127.0.0.1:8090/health` дополнительно показывает возраст
heartbeat, число ошибок последнего цикла и количество последовательных сбоев.

Остановить бота: вернись в Terminal и нажми `Control + C`.

## 3. Создание Binance Demo

Используй [официальную Binance Futures Demo Trading](https://demo.binance.com/).
Demo и основной Binance используют разные API-ключи. Актуальное описание среды есть
в [Binance Futures Quick Start](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/quick-start).

В профилях Binance Demo публичные свечи берутся с Binance Production, чтобы
стратегия видела основной рынок. API-ключ, баланс, стакан и все ордера при этом
остаются исключительно в Demo. В панели это показано двумя отдельными бейджами.

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
crypto_autobot/.venv/bin/python crypto_autobot/bot.py \
  --config crypto_autobot/config.demo.asymmetric-15m.example.json \
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
crypto_autobot/.venv/bin/python crypto_autobot/bot.py \
  --config crypto_autobot/config.demo.asymmetric-15m.example.json \
  --enable-orders
```

Теперь бот ждёт сетап. Пока его нет, в панели будут `no signal`, `ATR filter` или
другая конкретная причина. Когда сетап появится, бот отправит Demo-вход и два
защитных ордера.

Данные Demo сохраняются отдельно:

```text
crypto_autobot/data/asymmetric_15m_demo/state_demo.json
crypto_autobot/data/asymmetric_15m_demo/state_demo.json.bak1
crypto_autobot/data/asymmetric_15m_demo/state_demo.json.bak2
crypto_autobot/data/asymmetric_15m_demo/trades_demo.csv
```

Основной JSON записывается атомарно. Две резервные копии ротируются при значимых
изменениях: новой позиции, изменении баланса, сделки или forward-статистики. Если
основной файл повреждён, бот автоматически берёт самую свежую валидную копию,
оставляет повреждённый файл с суффиксом `.corrupt-<время>` и показывает
`State: восстановлен` в панели. Если повреждены все поколения, бот не создаёт
пустой счёт и не теряет историю молча, а останавливается для ручной проверки.

### Внутридневной Demo-профиль

Этот профиль работает по закрытым 15-минутным свечам, проверяет рынок каждые
15 секунд и держит не больше четырёх позиций или ожидающих заявок одновременно.

Самый простой запуск на Mac: дважды нажми файл
`crypto_autobot/start_asymmetric_demo.command`. Введи Demo key и скрытый secret;
скрипт сначала проверит подключение без ордеров, затем запустит бота.

```bash
caffeinate -i crypto_autobot/.venv/bin/python crypto_autobot/bot.py \
  --config crypto_autobot/config.demo.asymmetric-15m.example.json \
  --enable-orders
```

После сигнала бот ставит post-only лимитную заявку на откате `0.1 ATR`. Если
цена не вернулась к заявке в течение следующей 15-минутной свечи, заявка
отменяется. После заполнения бот сразу добавляет рыночный защитный Stop Loss и
reduce-only лимитный Take Profit. Оба ордера проверяются каждый цикл; при пропаже
любого из них бот отправляет аварийное закрытие.

За исполненными входами следит отдельный watchdog с интервалом 2 секунды. Он не
рассчитывает новые сигналы и не меняет логику стратегии: его задача только быстро
активировать SL/TP и закрыть позицию, если защита исчезла. В Demo/Live endpoint
`/health` становится `degraded`, если watchdog перестал обновляться.

После обрыва сети, сна или остановки поставщика котировок бот не использует старый
сигнал: если закрытая свеча старше двух активных таймфреймов, новый вход блокируется
со статусом `stale market data` до восстановления свежей истории.

### Автозапуск на Mac без открытого Terminal

Дважды нажми `crypto_autobot/install_macos_demo_service.command`, введи Demo key
и скрытый secret. Установщик сначала выполнит безопасную проверку подключения без
ордера: баланс, One-Way Mode, доступность всех пар, объём истории и свежесть
последней свечи. Только после успешной проверки он сохранит ключи в macOS Keychain и
запустит сервис через `launchd`.

Сервис автоматически запускается после входа в macOS и перезапускается после
сбоя. Demo-панель открывается на `http://127.0.0.1:8091`, а Paper может
параллельно работать на `8090`. Логи находятся в
`~/Library/Application Support/CryptoAutobot/crypto_autobot/data/launchd`.
Остановить и удалить
автозапуск можно файлом `crypto_autobot/uninstall_macos_demo_service.command`.
Ключи при удалении сервиса остаются в Keychain, чтобы случайно не потерять их.

Это не заменяет удаленный сервер: выключенный Mac и MacBook с закрытой крышкой
торговать не будут. `caffeinate` защищает только от обычного сна при открытой крышке.

В интерфейсе появится блок `Проверка рыночного ордера`. Выбери пару и нажми
`Test Long` или `Test Short`. После подтверждения бот отправит небольшой MARKET
ордер только на Binance Demo и сразу добавит Stop Loss и Take Profit. Кнопка
заблокирована в Paper и Live.

Перед проверкой добавь виртуальные USDT в кошелек Binance Futures Demo. На
270-дневном validation профиль дал `4.72` сделки/день, win rate `45.60%`,
`PF 1.179`; при повышенных расходах `PF 1.106`. Подтверждающие следующие 60 дней:
`4.77` сделки/день, win rate `50.35%`, `PF 1.237`; стресс `PF 1.156`. Точный
RR `1:2` не выбран, потому что его validation win rate был ниже `45%`, а PF хуже.
Live остаётся запрещён до длительной Demo-проверки. Полный отчёт лежит в
`crypto_autobot/research/asymmetric_risk_audit.json`.

Панель сама ведёт forward-validation и показывает девять проверок в разделе
`Готовность к Live`. Для допуска нужны одновременно:

- не менее 30 активных дней Demo-наблюдения (день засчитывается только при работающем сканере);
- не менее 100 закрытых Demo-сделок;
- средняя частота от 3.5 до 6.5 входов в день;
- win rate не ниже 45% и profit factor не ниже 1.10;
- нижняя граница приблизительного 95% интервала среднего PnL на сделку выше нуля;
- положительная доходность и максимальная просадка не выше 10%;
- номинальный RR не ниже 1:1.5.

Backtest не засчитывается в этот gate. Если фактическая Demo-выборка не прошла
хотя бы один пункт, кнопка `Live` остаётся недоступной.

В отдельном разделе `Надёжность выборки` показаны интервалы win rate, expectancy,
частоты, результат в `R`, payoff и break-even WR. До накопления достаточного числа
сделок широкие интервалы и статус «ещё не подтверждён» являются нормальными.

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
  --config crypto_autobot/config.paper.asymmetric-15m.example.json \
  --enable-orders
```

Для управления режимами на удалённом сервере задай отдельный пароль:

```bash
export DASHBOARD_CONTROL_TOKEN="ПРИДУМАЙ_ДЛИННЫЙ_ПАРОЛЬ"
```

Тогда в панели появится поле `Код управления`. Без этого пароля удалённое
переключение заблокировано.

`Live` дополнительно требует пройденный Demo gate, live-ключи, параметр
`--allow-live-ui` при запуске и точную фразу подтверждения в окне интерфейса.
Для реальных ордеров всё ещё нужен `--enable-orders`. Основной Live-профиль имеет
плечо 1x, риск `0.05%` для Short и `0.01%` для Long, максимум две позиции и
дневной стоп `0.5%`.

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
python3 crypto_autobot/portfolio_backtest.py \
  --config crypto_autobot/config.paper.asymmetric-15m.example.json \
  --days 240 \
  --test-days 60
```

В расчёт включены maker-комиссия `2 bps` для post-only входа и лимитного тейка,
taker-комиссия `5 bps` и проскальзывание `2 bps` для стопа и выхода по времени.
Итоговый отчёт:

```text
crypto_autobot/data/walkforward_report.json
```

Смотри не только на доходность, но и на число сделок, максимальную просадку и
результаты каждой пары. Один удачный период ничего не гарантирует; настройки должны
переживать разные рыночные режимы и после этого проверяться в Demo.

## 7. GitHub CI и постоянный запуск

GitHub Actions теперь используется только как CI: запускает тесты, проверяет
компиляцию Python и публикует статическую status-page. В workflow нет расписания,
Binance secrets и отправки ордеров.

3 августа 2026 года проверка GitHub-hosted runner получила от Binance `HTTP 451`
из-за локации IP. Даже без этого GitHub Actions не подходит для торговли:
одноразовый runner завершается и не может каждые 2 секунды проверять
заполнение лимитного входа и наличие Stop Loss/Take Profit.

Старые Repository secrets `BINANCE_DEMO_API_KEY`, `BINANCE_DEMO_API_SECRET` и
`STATE_ENCRYPTION_PASSWORD` больше не используются workflow и их можно удалить из
`Settings` → `Secrets and variables` → `Actions`.

Для Binance Demo используй один из двух непрерывных вариантов:

1. Mac: `install_macos_demo_service.command` хранит ключи в Keychain и запускает
   бота через `launchd` после входа в macOS. Demo-панель доступна на
   `http://127.0.0.1:8091` и не мешает Paper-процессу на `8090`.
2. Европейский VPS: `compose.demo.yml` перезапускает контейнер и хранит
   состояние в постоянном `data/`.

### Постоянный VPS через Docker

Для европейского VPS подготовлен `compose.demo.yml`. Он перезапускает Demo-бота
после сбоя или перезагрузки сервера и сохраняет статистику в `data/`. Перед
запуском задай ключи только в текущем окружении или защищенном хранилище сервера:

```bash
export BINANCE_DEMO_API_KEY="ТВОЙ_DEMO_KEY"
export BINANCE_DEMO_API_SECRET="ТВОЙ_DEMO_SECRET"
export DASHBOARD_CONTROL_TOKEN="ДЛИННЫЙ_СЛУЧАЙНЫЙ_ПАРОЛЬ"
docker compose -f crypto_autobot/compose.demo.yml up -d --build
```

Порт панели привязан к `127.0.0.1` сервера и не выставлен открыто в интернет.
Открывай его через SSH-туннель:

```bash
ssh -L 8090:127.0.0.1:8090 user@SERVER_IP
```

После этого панель доступна локально по `http://127.0.0.1:8090`. Бесплатные
web-сервисы Render и Koyeb засыпают без входящего трафика, поэтому для безопасной
непрерывной торговли они не подходят. GitHub Actions остается только CI.

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

Asymmetric 15m Paper/Demo:

- риск Short: 0.15%;
- риск Long: 0.025%;
- максимум открытых позиций или ожидающих заявок: 4;
- максимум новых сделок в день: 12;
- остановка после дневного убытка 1.2%;
- isolated margin;
- плечо 2x;
- Stop Loss: 1.8 ATR;
- Take Profit: 2.8 ATR.

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

## 11. Подключение MT5 Demo

Перед переходом к другому брокеру через MT5 нужно заново проверить стратегию на его котировках,
спреде, комиссии, swap и минимальном лоте. Текущие Binance-цифры нельзя переносить на MT5.

Адаптер `mt5_broker.py` уже подключён к общему торговому контуру и умеет:

- подключаться к локальному MT5-терминалу через официальный Python-модуль;
- получать закрытые свечи напрямую из терминала MT5, а не с Binance;
- рассчитывать lot по стоимости стопа у брокера;
- ставить market и limit-заявки с прикреплёнными SL/TP;
- отказываться от сделки, если минимальный lot превысит заданный риск.

MT5-профиль запускается отдельно, а его статистика сохраняется в
`state_mt5_demo.json` и `trades_mt5_demo.csv`. Сначала открой Demo-счёт у
конкретного MT5-брокера и в `config.mt5-demo.asymmetric-15m.example.json` замени
`symbol_map` на точные названия криптосимволов этого брокера.

Логин, пароль и сервер не записываются в JSON. Перед запуском задай их в окружении:

```bash
export MT5_LOGIN="НОМЕР_DEMO_СЧЁТА"
export MT5_PASSWORD="ПАРОЛЬ_DEMO_СЧЁТА"
export MT5_SERVER="ТОЧНОЕ_ИМЯ_DEMO_СЕРВЕРА"
```

На Windows-машине с установленным и запущенным терминалом MT5 сначала проверь
подключение без ордеров:

```bash
python -m pip install -r crypto_autobot/requirements-mt5.txt
python crypto_autobot/bot.py \
  --config crypto_autobot/config.mt5-demo.asymmetric-15m.example.json \
  --check
```

И только после успешной проверки запусти Demo-ордера:

```bash
python crypto_autobot/bot.py \
  --config crypto_autobot/config.mt5-demo.asymmetric-15m.example.json \
  --enable-orders
```

Для постоянного MT5-запуска нужна Windows-машина или Windows VPS с постоянно
запущенным терминалом MetaTrader 5. Фоновый запуск настраивается там через
Task Scheduler или менеджер Windows-служб. `install_macos_demo_service.command`
относится только к Binance Demo и MT5 не устанавливает.

По официальной схеме MetaQuotes Python-модуль подключается к работающему терминалу через
[`initialize`](https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py), а заявки отправляются через
[`order_send`](https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py). История закрытых баров читается через
[`copy_rates_from_pos`](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrompos_py): позиция `0` является
текущим формирующимся баром, поэтому бот начинает с позиции `1`.
Конкретный MT5 Demo нельзя считать проверенным, пока не выбран брокер и не сделан
маленький защищённый тестовый ордер на его сервере.

## 12. Важные файлы

```text
bot.py                         основной процесс и веб-панель
binance_futures.py             подключение к Binance
mt5_broker.py                  рабочий адаптер MT5
backtest.py                    историческая проверка стратегии
config.paper.asymmetric-15m.example.json  Paper 15m
config.demo.asymmetric-15m.example.json   Binance Demo 15m
config.mt5-demo.asymmetric-15m.example.json  MT5 Demo 15m
config.live.example.json       Live с безопасными ограничениями
data/state_*.json              текущее состояние
data/state_*.json.bak1/.bak2   ротационные резервные копии
data/trades_*.csv              журнал сделок
```
