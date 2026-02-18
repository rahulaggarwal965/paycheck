"""Yahoo Finance price fetcher with disk caching."""

import yfinance as yf
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Optional, Tuple
import pickle
import logging

logger = logging.getLogger(__name__)


class YahooPriceFetcher:
    """Yahoo Finance price fetcher with disk and memory caching."""
    
    def __init__(self, symbol: str, cache_dir: str, price_field: str = "Adj Close"):
        """Initialize the price fetcher.
        
        Args:
            symbol: Stock symbol (e.g., 'AAPL')
            cache_dir: Directory for caching price data
            price_field: Price field to use ('Adj Close', 'Close', etc.)
        """
        self.symbol = symbol.upper()
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.price_field = price_field
        self._cache = {}  # Memory cache
        
    def get_price(self, target_date: date, price_field: Optional[str] = None) -> Optional[float]:
        """Get price for a specific date.

        Handles weekends and holidays by finding the most recent trading day.

        Args:
            target_date: Date to get price for
            price_field: Price field to return (e.g. "Open", "Close"). Defaults to self.price_field.

        Returns:
            Price on the date, or None if not available
        """
        field = price_field or self.price_field

        # Check memory cache
        cached = self._cache.get(target_date)
        if cached is not None:
            if isinstance(cached, dict):
                return cached.get(field)
            # Legacy scalar cache entry — only valid for default field
            return cached if field == self.price_field else None

        # Check disk cache
        year_data = self._load_year_cache(target_date.year)
        if year_data and target_date in year_data:
            self._cache.update(year_data)
            entry = year_data[target_date]
            if isinstance(entry, dict):
                return entry.get(field)
            return entry if field == self.price_field else None

        # Fetch from yfinance if not in cache
        year_data = self._fetch_year_data(target_date.year)
        if year_data:
            self._save_year_cache(target_date.year, year_data)
            self._cache.update(year_data)
            entry = year_data.get(target_date)
            if entry is None:
                return None
            if isinstance(entry, dict):
                return entry.get(field)
            return entry if field == self.price_field else None

        return None
    
    def get_last_trading_day_price(self, year: int, month: int) -> Tuple[date, float]:
        """Get the last trading day of a month and its price.
        
        Args:
            year: Year
            month: Month (1-12)
            
        Returns:
            Tuple of (last_trading_date, price)
            
        Raises:
            ValueError: If no trading data available for the month
        """
        # Get month data
        start_date = date(year, month, 1)
        if month == 12:
            end_date = date(year + 1, 1, 1)
        else:
            end_date = date(year, month + 1, 1)
        
        # Fetch data for the month
        ticker = yf.Ticker(self.symbol)
        try:
            hist = ticker.history(start=start_date, end=end_date)
        except Exception as e:
            logger.error(f"Failed to fetch data for {self.symbol} in {year}-{month:02d}: {e}")
            raise ValueError(f"No trading data for {self.symbol} in {year}-{month:02d}")
        
        if hist.empty:
            raise ValueError(f"No trading data for {self.symbol} in {year}-{month:02d}")
        
        # Get last trading day
        last_date = hist.index[-1].date()
        
        # Handle different possible column names
        available_columns = hist.columns.tolist()
        price_column = None
        
        # Try to find the price column (prioritize Close over Adj Close for yfinance compatibility)
        for col_name in [self.price_field, 'Close', 'Adj Close', 'close', 'adj_close']:
            if col_name in available_columns:
                price_column = col_name
                break
        
        if price_column is None:
            logger.error(f"No suitable price column found. Available columns: {available_columns}")
            raise ValueError(f"No suitable price column found for {self.symbol}")
        
        last_price = float(hist.iloc[-1][price_column])
        
        return last_date, last_price
    
    def _load_year_cache(self, year: int) -> Optional[dict]:
        """Load year data from disk cache."""
        cache_file = self.cache_dir / f"{self.symbol}_{year}.pkl"
        if cache_file.exists():
            try:
                with open(cache_file, 'rb') as f:
                    return pickle.load(f)
            except Exception as e:
                logger.warning(f"Failed to load cache file {cache_file}: {e}")
                # Remove corrupted cache file
                cache_file.unlink(missing_ok=True)
        return None
    
    def _save_year_cache(self, year: int, year_data: dict) -> None:
        """Save year data to disk cache."""
        cache_file = self.cache_dir / f"{self.symbol}_{year}.pkl"
        try:
            with open(cache_file, 'wb') as f:
                pickle.dump(year_data, f)
        except Exception as e:
            logger.warning(f"Failed to save cache file {cache_file}: {e}")
    
    def _fetch_year_data(self, year: int) -> Optional[dict]:
        """Fetch full year data from yfinance.

        Returns dict of {date: {field: price, ...}} storing all available price fields.
        """
        ticker = yf.Ticker(self.symbol)
        start_date = date(year, 1, 1)
        end_date = date(year, 12, 31)

        try:
            hist = ticker.history(start=start_date, end=end_date)
        except Exception as e:
            logger.error(f"Failed to fetch year data for {self.symbol} in {year}: {e}")
            return None

        if hist.empty:
            logger.warning(f"No trading data for {self.symbol} in {year}")
            return None

        available_columns = hist.columns.tolist()

        # Determine which price columns to store
        price_columns = [c for c in available_columns
                         if c in ("Open", "Close", "Adj Close", "High", "Low",
                                  "open", "close", "adj_close", "high", "low")]

        if not price_columns:
            logger.error(f"No suitable price columns found. Available columns: {available_columns}")
            return None

        year_data = {}
        for idx, row in hist.iterrows():
            trading_date = idx.date()
            year_data[trading_date] = {col: float(row[col]) for col in price_columns}

        logger.info(f"Fetched {len(year_data)} trading days for {self.symbol} in {year}")
        return year_data
    
    def get_price_on_or_before(self, target_date: date, max_lookback_days: int = 10,
                              price_field: Optional[str] = None) -> Optional[float]:
        """Get price on target date or the most recent trading day before it.

        Args:
            target_date: Target date
            max_lookback_days: Maximum days to look back
            price_field: Price field to return (e.g. "Open", "Close"). Defaults to self.price_field.

        Returns:
            Price on the date or most recent trading day, or None if not found
        """
        for i in range(max_lookback_days + 1):
            check_date = target_date - timedelta(days=i)
            price = self.get_price(check_date, price_field=price_field)
            if price is not None:
                return price

        return None
    
    def clear_cache(self) -> None:
        """Clear both memory and disk cache."""
        self._cache.clear()
        
        # Remove disk cache files
        for cache_file in self.cache_dir.glob(f"{self.symbol}_*.pkl"):
            try:
                cache_file.unlink()
                logger.info(f"Removed cache file: {cache_file}")
            except Exception as e:
                logger.warning(f"Failed to remove cache file {cache_file}: {e}")
