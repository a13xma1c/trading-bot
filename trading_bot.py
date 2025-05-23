import requests
import pandas as pd
import numpy as np
import time
import logging
import json
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("trading_bot.log"),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("TradingBot")


class TradingBot:
    def __init__(self, config_path="config.json"):
        """Initialize the trading bot with configuration."""
        self.config = self._load_config(config_path)
        self.api_key = self.config.get("api_key")
        self.scan_interval = self.config.get("scan_interval", 60)  # In seconds
        self.profit_threshold = self.config.get("profit_threshold", 0.02)  # 2%
        self.max_stocks_to_analyze = self.config.get("max_stocks_to_analyze", 50)

        # Strategy parameters
        self.strategy = self.config.get("strategy", "combined")
        self.rsi_period = self.config.get("rsi_period", 14)
        self.rsi_oversold = self.config.get("rsi_oversold", 30)
        self.rsi_overbought = self.config.get("rsi_overbought", 70)
        self.short_ma_period = self.config.get("short_ma_period", 5)
        self.long_ma_period = self.config.get("long_ma_period", 20)

        # You would replace this with actual trading API credentials
        self.trading_enabled = self.config.get("trading_enabled", False)

        logger.info(f"Trading Bot initialized. Strategy: {self.strategy}, Trading enabled: {self.trading_enabled}")

    def _load_config(self, config_path):
        """Load configuration from JSON file."""
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            # Return default config
            return {
                "api_key": "YOUR_API_KEY_HERE",
                "scan_interval": 60,
                "profit_threshold": 0.02,
                "max_stocks_to_analyze": 50,
                "trading_enabled": False,
                "tickers_source": "alphavantage",
                "strategy": "combined",
                "rsi_period": 14,
                "rsi_oversold": 30,
                "rsi_overbought": 70,
                "short_ma_period": 5,
                "long_ma_period": 20
            }

    def get_tickers(self):
        """Get list of tickers from the configured API."""
        if self.config.get("tickers_source") == "alphavantage":
            return self._get_tickers_alphavantage()
        elif self.config.get("tickers_source") == "csv":
            return self._get_tickers_from_csv()
        else:
            # Default tickers for testing
            logger.warning("Using default ticker list")
            return ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NVDA", "AMD", "INTC", "IBM"]

    def _get_tickers_alphavantage(self):
        """Get tickers from Alpha Vantage API."""
        try:
            url = f"https://www.alphavantage.co/query?function=LISTING_STATUS&apikey={self.api_key}"
            response = requests.get(url)

            if response.status_code != 200:
                logger.error(f"API request failed: {response.status_code}")
                return []

            # The response is a CSV with header
            content = response.text.strip().split("\\n")

            # Skip header and parse each line
            tickers = [line.split(",")[0] for line in content[1:self.max_stocks_to_analyze + 1]]

            logger.info(f"Retrieved {len(tickers)} tickers from Alpha Vantage")
            return tickers

        except Exception as e:
            logger.error(f"Error getting tickers from Alpha Vantage: {e}")
            return []

    def _get_tickers_from_csv(self):
        """Get tickers from a local CSV file."""
        try:
            df = pd.read_csv(self.config.get("tickers_csv_path", "tickers.csv"))
            tickers = df["Symbol"].tolist()[:self.max_stocks_to_analyze]
            logger.info(f"Loaded {len(tickers)} tickers from CSV")
            return tickers
        except Exception as e:
            logger.error(f"Error loading tickers from CSV: {e}")
            return []

    def get_stock_data(self, ticker, period="1d"):
        """Get stock data for the specified ticker."""
        try:
            # For intraday data (fast trading)
            url = f"https://www.alphavantage.co/query?function=TIME_SERIES_INTRADAY&symbol={ticker}&interval=5min&outputsize=full&apikey={self.api_key}"
            response = requests.get(url)

            if response.status_code != 200:
                logger.error(f"API request failed for {ticker}: {response.status_code}")
                return None

            data = response.json()

            # Check if we have valid data
            if "Time Series (5min)" not in data:
                logger.warning(f"No time series data for {ticker}")
                return None

            # Convert to DataFrame
            time_series = data["Time Series (5min)"]
            df = pd.DataFrame(time_series).T

            # Rename columns
            df.columns = ["open", "high", "low", "close", "volume"]

            # Convert columns to numeric
            for col in df.columns:
                df[col] = pd.to_numeric(df[col])

            # Add calculated fields
            df["returns"] = df["close"].pct_change()
            df["volatility"] = df["returns"].rolling(12).std()  # Rolling 1-hour volatility

            # Calculate indicators
            self._add_indicators(df)

            logger.debug(f"Successfully retrieved data for {ticker}")
            return df

        except Exception as e:
            logger.error(f"Error getting stock data for {ticker}: {e}")
            return None

    def _add_indicators(self, df):
        """Add technical indicators to the dataframe."""
        # Moving Averages
        df["short_ma"] = df["close"].rolling(window=self.short_ma_period).mean()
        df["long_ma"] = df["close"].rolling(window=self.long_ma_period).mean()

        # RSI
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)

        avg_gain = gain.rolling(window=self.rsi_period).mean()
        avg_loss = loss.rolling(window=self.rsi_period).mean()

        rs = avg_gain / avg_loss
        df["rsi"] = 100 - (100 / (1 + rs))

        # MACD
        df["ema_12"] = df["close"].ewm(span=12, adjust=False).mean()
        df["ema_26"] = df["close"].ewm(span=26, adjust=False).mean()
        df["macd"] = df["ema_12"] - df["ema_26"]
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]

        # Bollinger Bands
        df["middle_band"] = df["close"].rolling(window=20).mean()
        df["std_dev"] = df["close"].rolling(window=20).std()
        df["upper_band"] = df["middle_band"] + (df["std_dev"] * 2)
        df["lower_band"] = df["middle_band"] - (df["std_dev"] * 2)

        # Volume indicators
        df["volume_ma"] = df["volume"].rolling(window=20).mean()
        df["volume_ratio"] = df["volume"] / df["volume_ma"]

        return df

    def analyze_stock(self, ticker):
        """Analyze a stock to determine if it's a good candidate for trading."""
        df = self.get_stock_data(ticker)

        if df is None or len(df) < max(self.rsi_period, self.long_ma_period) + 10:
            return {"ticker": ticker, "score": 0, "reason": "Insufficient data"}

        try:
            # Get the most recent data
            recent_data = df.iloc[-30:]  # Last 30 data points (2.5 hours for 5-min data)

            # Calculate scores based on various strategies
            scores = {}

            # RSI Strategy
            rsi_value = recent_data["rsi"].iloc[-1]
            if rsi_value < self.rsi_oversold:
                # Oversold - potential buy
                rsi_score = (self.rsi_oversold - rsi_value) / self.rsi_oversold
            elif rsi_value > self.rsi_overbought:
                # Overbought - avoid
                rsi_score = 0
            else:
                # Neutral
                rsi_score = 0.5
            scores["rsi"] = rsi_score

            # Moving Average Crossover Strategy
            if recent_data["short_ma"].iloc[-1] > recent_data["long_ma"].iloc[-1] and \
                    recent_data["short_ma"].iloc[-2] <= recent_data["long_ma"].iloc[-2]:
                # Bullish crossover just occurred
                ma_score = 1.0
            elif recent_data["short_ma"].iloc[-1] > recent_data["long_ma"].iloc[-1]:
                # Short MA above long MA - bullish
                ma_score = 0.7
            else:
                # Short MA below long MA - bearish
                ma_score = 0.2
            scores["ma_crossover"] = ma_score

            # MACD Strategy
            if recent_data["macd"].iloc[-1] > recent_data["macd_signal"].iloc[-1] and \
                    recent_data["macd"].iloc[-2] <= recent_data["macd_signal"].iloc[-2]:
                # Bullish crossover just occurred
                macd_score = 1.0
            elif recent_data["macd"].iloc[-1] > recent_data["macd_signal"].iloc[-1]:
                # MACD above signal - bullish
                macd_score = 0.7
            else:
                # MACD below signal - bearish
                macd_score = 0.3
            scores["macd"] = macd_score
            # Bollinger Band Strategy
            close_price = recent_data["close"].iloc[-1]
            lower_band = recent_data["lower_band"].iloc[-1]
            upper_band = recent_data["upper_band"].iloc[-1]

            if close_price < lower_band:
                # Price below lower band - potential buy (oversold)
                bb_score = 0.9
            elif close_price > upper_band:
                # Price above upper band - avoid (overbought)
                bb_score = 0.1
            else:
                # Price within bands - neutral
                position = (close_price - lower_band) / (upper_band - lower_band)
                bb_score = 1 - position  # Higher score when closer to lower band
            scores["bollinger"] = bb_score

            # Volume Analysis
            vol_ratio = recent_data["volume_ratio"].iloc[-1]
            if vol_ratio > 2:
                # Volume spike - significant interest
                vol_score = 0.9
            elif vol_ratio > 1.5:
                # Above average volume
                vol_score = 0.7
            elif vol_ratio < 0.5:
                # Low volume - lack of interest
                vol_score = 0.3
            else:
                # Normal volume
                vol_score = 0.5
            scores["volume"] = vol_score

            # Recent price trend
            recent_returns = recent_data["returns"].iloc[-5:].mean()
            if recent_returns > 0.005:  # 0.5% average return
                trend_score = 0.8
            elif recent_returns > 0.002:  # 0.2% average return
                trend_score = 0.6
            elif recent_returns < -0.002:
                trend_score = 0.3
            else:
                trend_score = 0.5
            scores["trend"] = trend_score

            # Volatility consideration (penalize excessive volatility)
            recent_volatility = recent_data["volatility"].iloc[-1]
            if recent_volatility > 0.02:  # 2% volatility
                vol_penalty = 0.5
            elif recent_volatility > 0.01:  # 1% volatility
                vol_penalty = 0.3
            elif recent_volatility > 0.005:  # 0.5% volatility
                vol_penalty = 0.1
            else:
                vol_penalty = 0

            # Calculate final score based on strategy
            if self.strategy == "rsi":
                final_score = scores["rsi"]
            elif self.strategy == "ma_crossover":
                final_score = scores["ma_crossover"]
            elif self.strategy == "macd":
                final_score = scores["macd"]
            elif self.strategy == "bollinger":
                final_score = scores["bollinger"]
            else:  # combined strategy
                # Weighted average of all strategies
                weights = {
                    "rsi": 0.2,
                    "ma_crossover": 0.2,
                    "macd": 0.2,
                    "bollinger": 0.15,
                    "volume": 0.1,
                    "trend": 0.15
                }
                final_score = sum(scores[k] * weights[k] for k in weights) - vol_penalty
                # Prepare result
                result = {
                    "ticker": ticker,
                    "score": final_score,
                    "last_price": df["close"].iloc[-1],
                    "price_change_pct": df["returns"].iloc[-1] * 100 if not pd.isna(df["returns"].iloc[-1]) else 0,
                    "rsi": df["rsi"].iloc[-1],
                    "ma_signal": "bullish" if df["short_ma"].iloc[-1] > df["long_ma"].iloc[-1] else "bearish",
                    "macd_signal": "bullish" if df["macd"].iloc[-1] > df["macd_signal"].iloc[-1] else "bearish",
                    "bb_position": (df["close"].iloc[-1] - df["lower_band"].iloc[-1]) / (
                                df["upper_band"].iloc[-1] - df["lower_band"].iloc[-1]),
                    "volume_ratio": df["volume_ratio"].iloc[-1],
                    "volatility": df["volatility"].iloc[-1]
                }

                return result

        except Exception as e:
            logger.error(f"Error analyzing {ticker}: {e}")
            return {"ticker": ticker, "score": 0, "reason": f"Analysis error: {str(e)}"}

    def scan_market(self):
        """Scan the market for the best trading opportunities."""
        tickers = self.get_tickers()
        logger.info(f"Scanning {len(tickers)} tickers for trading opportunities")

        results = []
        for ticker in tickers:
            try:
                analysis = self.analyze_stock(ticker)
                results.append(analysis)
                # Add a small delay to avoid API rate limits
                time.sleep(0.2)
            except Exception as e:
                logger.error(f"Error processing {ticker}: {e}")

        # Sort results by score in descending order
        sorted_results = sorted([r for r in results if "score" in r], key=lambda x: x["score"], reverse=True)

        if not sorted_results:
            logger.warning("No viable trading candidates found")
            return None

        # Get top N candidates
        top_candidates = sorted_results[:min(5, len(sorted_results))]

        logger.info(f"Top trading candidates:")
        for i, candidate in enumerate(top_candidates, 1):
            logger.info(
                f"{i}. {candidate['ticker']} - Score: {candidate['score']:.4f}, Price: {candidate['last_price']}, RSI: {candidate.get('rsi', 'N/A')}")

        return top_candidates[0]  # Return the best candidate

    def execute_trade(self, stock_data):
        """Execute a trade based on the analysis results."""
        if not self.trading_enabled:
            logger.info(
                f"Trading disabled. Would have traded {stock_data['ticker']} (Score: {stock_data['score']:.4f})")
            logger.info(
                f"Analysis - RSI: {stock_data.get('rsi', 'N/A')}, MA Signal: {stock_data.get('ma_signal', 'N/A')}, MACD: {stock_data.get('macd_signal', 'N/A')}")
            return False

        # Implement your trading logic here
        # This would connect to your broker's API

        logger.info(f"Executed trade for {stock_data['ticker']}")
        return True

    def run(self):
        """Run the trading bot continuously."""
        logger.info("Trading bot started")

        while True:
            try:
                # Scan the market
                best_stock = self.scan_market()

                if best_stock and best_stock["score"] > self.profit_threshold:
                    # Execute trade
                    self.execute_trade(best_stock)
                else:
                    if best_stock:
                        logger.info(
                            f"Best candidate {best_stock['ticker']} score {best_stock['score']:.4f} below threshold {self.profit_threshold}")
                    else:
                        logger.info("No suitable trading opportunities found in this scan")

                logger.info(f"Waiting {self.scan_interval} seconds until next scan")
                time.sleep(self.scan_interval)

            except KeyboardInterrupt:
                logger.info("Bot stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                # Wait a bit before retrying
                time.sleep(5)


if __name__ == "__main__":
    bot = TradingBot()
    bot.run()