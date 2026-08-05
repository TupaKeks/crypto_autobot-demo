# MT5 Demo broker shortlist for Poland/EU

Checked: 2026-08-05. This is a technical shortlist for Demo validation, not a
recommendation to deposit money or trade Live. Product availability must be
confirmed during registration and again inside the exact MT5 account.

## Decision

1. Keep Binance Demo as the only active forward-validation environment.
2. Test IC Markets EU first for MT5 Demo connectivity and broker-native data.
3. Test Pepperstone EU second if its terminal provides a better overlap with the
   current ten-symbol strategy basket.
4. Use Admirals as the Polish-language fallback.

No MT5 result may reuse the Binance backtest metrics. Every broker has different
symbols, candles, spread, swaps, minimum volume and execution.

## Shortlist

### 1. IC Markets EU

- EU entity: IC Markets (EU) Ltd, CySEC licence 362/18.
- Free Demo and MT5 are available.
- The official MT5 page explicitly allows scalping and automated trading.
- The official trading-hours table lists crypto CFD sessions Monday through
  Sunday, with short daily and Friday/Saturday breaks.
- The published MT4/MT5 crypto list includes BTC, ETH, ADA, XLM, SOL and other
  symbols. Only ADA, ETH and XLM are confirmed overlaps with the current ten
  Binance strategy symbols, so the current five-trades-per-day expectation does
  not transfer to this broker.

Official sources:

- https://www.icmarkets.eu/en/forex-trading-platform-metatrader/metatrader-5
- https://www.icmarkets.eu/en/open-trading-account/demo
- https://www.icmarkets.eu/en/trading-markets/cryptocurrency
- https://www.icmarkets.eu/en/trading-pricing/trading-hours
- https://www.icmarkets.eu/en/company/legal-documents

### 2. Pepperstone EU

- MT5 Demo is available with virtual funds; the current getting-started page
  states a 30-day Demo period.
- The official crypto page states 24/7 crypto CFD trading and MT5 support.
- Expert Advisors are allowed on MT5.
- Exact symbol names and the overlap with the strategy basket must be discovered
  inside the account before any backtest or order test.

Official sources:

- https://pepperstone.com/en-eu/help-and-support/getting-started/
- https://pepperstone.com/en-eu/markets/cryptocurrencies/
- https://pepperstone.com/en-eu/ways-to-trade/trading-hours/
- https://pepperstone.com/en-eu/education/which-platforms-can-i-use-expert-advisors/
- https://pepperstone.com/en-eu/legal-documents/

### 3. Admirals Poland

- Polish-language onboarding, MT5 Demo and crypto CFDs are available.
- MT5 supports Expert Advisors and strategy testing.
- Exact weekend sessions and the full crypto symbol basket were not proven from
  the public product page. They must be checked in Market Watch/Specification.

Official sources:

- https://admiralmarkets.com/pl/trading-platforms/metatrader-5
- https://admiralmarkets.com/pl/start-trading/forex-demo
- https://admiralmarkets.com/pl/products/cryptocurrencies

## Runtime constraint

The official `MetaTrader5` Python package communicates with a running terminal.
Its current PyPI release provides Windows x86-64 wheels only. A broker may offer
an MT5 user interface for macOS, but this Python adapter still needs native
Windows or a Windows VPS for dependable 24/7 execution.

Official sources:

- https://www.mql5.com/en/docs/python_metatrader5
- https://pypi.org/project/MetaTrader5/

## Safe validation sequence

1. User opens an MT5 Demo account and installs the broker's MT5 terminal on
   Windows. No deposit is required for this stage.
2. Log in to Demo and enable algorithmic trading in the terminal.
3. Set `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` and, if necessary,
   `MT5_TERMINAL_PATH` as environment variables.
4. Run symbol discovery. It calls `symbols_get` and sends no checks or orders:

   ```bash
   python crypto_autobot/bot.py \
     --config crypto_autobot/config.mt5-demo.asymmetric-15m.example.json \
     --discover-mt5-symbols
   ```

5. Review `recommended_symbol_map`. Missing or ambiguous symbols must be mapped
   manually from Market Watch; never guess a suffix.
6. Run `--check`. It validates account environment, history, trade permissions,
   order capabilities and a minimum-volume request through `order_check`, with
   `orders_sent: 0`.
7. Backtest the strategy on that broker's MT5 bars and real spread/swap model.
   Rebuild the basket if the current ten symbols are unavailable.
8. Only after a positive broker-native holdout, make one smallest-volume Demo
   order with server-side SL/TP.
9. Accumulate at least 30 qualified days and 100 strategic Demo trades before
   any Live discussion.

