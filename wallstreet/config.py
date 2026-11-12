"""Settings. Everything comes from environment variables (or a .env file next to the project); nothing secret is stored in code.

Safety switches that cannot be turned on by accident:
  REAL_TRADING is not a setting at all. The paper broker is the only order path, and the Schwab connector has no
  order-placing method (see broker/schwab.py).
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file():
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()


def env(name, default=None, cast=str):
    v = os.environ.get(name)
    if v in (None, ""):
        return default
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


DATA_DIR = Path(env("PWS_DATA_DIR", str(ROOT / "var")))
DB_PATH = DATA_DIR / "wallstreet.db"
LOG_PATH = DATA_DIR / "wallstreet.log"

# The SEC requires a descriptive User-Agent with a contact email on every request to sec.gov / data.sec.gov.
SEC_USER_AGENT = env("PWS_SEC_USER_AGENT", "ProjectWallStreet research (set PWS_SEC_USER_AGENT to 'Name email@domain')")
HTTP_USER_AGENT = "Mozilla/5.0 (ProjectWallStreet; paper research)"

# Paper account
PAPER_START_CASH = env("PWS_PAPER_CASH", 10000.0, float)
COMMISSION_PER_ORDER = env("PWS_COMMISSION", 0.0, float)      # most US brokers charge $0 on stocks
SLIPPAGE_BPS = env("PWS_SLIPPAGE_BPS", 5.0, float)             # 0.05% against you on every fill
FRACTIONAL = env("PWS_FRACTIONAL", 1, int) == 1                 # fractional shares, as most US brokers allow

# Risk department (all limits are hard: an order that breaks one is rejected, not resized silently)
RISK = {
    "max_position_pct": env("PWS_MAX_POSITION_PCT", 10.0, float),      # of account value, per stock
    "max_loss_per_trade_pct": env("PWS_MAX_LOSS_PER_TRADE_PCT", 1.0, float),  # stop distance x shares, of account
    "max_open_positions": env("PWS_MAX_OPEN_POSITIONS", 10, int),
    "daily_loss_limit_pct": env("PWS_DAILY_LOSS_LIMIT_PCT", 3.0, float),      # no new buys after this loss today
    "max_drawdown_pct": env("PWS_MAX_DRAWDOWN_PCT", 15.0, float),             # kill switch for the whole account
    "max_sector_pct": env("PWS_MAX_SECTOR_PCT", 35.0, float),
    "max_correlation": env("PWS_MAX_CORRELATION", 0.85, float),               # vs any open position, 60-day returns
}

# Default research universe: liquid large caps and the main index ETFs. Override with PWS_UNIVERSE="AAPL,MSFT,..."
DEFAULT_UNIVERSE = ("SPY,QQQ,IWM,DIA,AAPL,MSFT,NVDA,AMZN,GOOGL,META,AVGO,TSLA,JPM,V,MA,UNH,LLY,XOM,COST,WMT,HD,"
                    "PG,JNJ,ORCL,CRM,AMD,ADBE,CSCO,NFLX,PEP,KO,BAC,CRWD,PANW,ANET,PLTR,MU,QCOM,INTC,TXN")
UNIVERSE = [s.strip().upper() for s in env("PWS_UNIVERSE", DEFAULT_UNIVERSE).split(",") if s.strip()]
