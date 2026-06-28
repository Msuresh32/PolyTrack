# Sharp Wallet Ingestion

`ingest_sharp_wallets.py` builds category-specific wallet pools from
PolymarketAnalytics and Polymarket's public positions API.

## Sources

- Historical category leaderboard:
  `https://legacy.polymarketanalytics.com/api/traders-tag-performance`
- Current open positions:
  `https://data-api.polymarket.com/positions`

## Default Categories

- WNBA
- MLB
- FIFA World Cup, normalized to dashboard category `FIFA WC`

## Default Filters

- At least 25 resolved markets
- At least 50 total positions
- At least $10,000 category P/L
- At least 52% category win rate
- At least 2% estimated ROI
- At least $250 open category exposure
- At least one category position resolving within 2 days
- At most 10 selected wallets per category

The selected file is `sharp_wallets_selected.json`.
Rejected candidates and reasons are saved to `sharp_wallets_rejected.json`.

## Dashboard Integration

The dashboard loads `SHARP_WALLETS_FILE`, defaulting to
`sharp_wallets_selected.json`.

Selected wallets are category-scoped. For example, a wallet selected for MLB
only contributes MLB positions unless it is also manually listed in `.env`.
Manual `.env` wallets remain trusted across categories.

The dashboard uses the expanded wallet set for:

- category-specific sharp wallet pools
- aligned wallet count
- opposing wallet count
- net sharp alignment
- conviction scoring
- detail drawer aligned/opposing wallet sections
