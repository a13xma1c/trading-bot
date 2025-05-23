# Trading Bot

A sophisticated trading bot that scans the market for potential short-term profit opportunities using technical analysis.

## Features

- Fetches stock tickers from Alpha Vantage API
- Analyzes stocks based on multiple technical indicators:
  - RSI (Relative Strength Index)
  - Moving Average Crossovers
  - MACD (Moving Average Convergence Divergence)
  - Bollinger Bands
  - Volume Analysis
- Identifies the best candidates for short-term trading
- Configurable strategies and parameters
- Can be configured to execute trades (disabled by default)

## Installation

1. Clone this repository
2. Install the required packages: 
   ```bash
      pip install -r requirements.txt
      ```
3.  Configure your API key in `config.json`

## Configuration

Edit the `config.json` file to customize the bot's behavior:

- `api_key`: Your Alpha Vantage API key
- `scan_interval`: Time between market scans in seconds
- `profit_threshold`: Minimum score required to consider a trade
- `max_stocks_to_analyze`: Maximum number of stocks to analyze per scan
- `trading_enabled`: Set to true to enable actual trading (use with caution!)
- `tickers_source`: Where to get tickers from ('alphavantage' or 'csv')
- `strategy`: Trading strategy to use ('combined', 'rsi', 'ma_crossover', 'macd', 'bollinger')
- `rsi_period`: Period for RSI calculation
- `rsi_oversold`: RSI level considered oversold
- `rsi_overbought`: RSI level considered overbought
- `short_ma_period`: Period for short moving average
- `long_ma_period`: Period for long moving average

## Usage

Run the bot with:
  ```bash
     python trading_bot.py
```

The bot will scan the market at the configured interval and log the best trading opportunities.

## Getting an Alpha Vantage API Key

1. Visit [Alpha Vantage](https://www.alphavantage.co/support/#api-key)
2. Register for a free API key
3. Add your API key to the `config.json` file

## Disclaimer

This trading bot is for educational purposes only. Use at your own risk. Past performance is not indicative of future results. Always consult with a qualified financial advisor before making investment decisions.

## License

MIT
