# Trade Log

Sourced from: `PortfolioDownload.csv` (Fidelity export, May 6 2026)
Replicated in: Alpaca paper account PA38PL3UVB0D

---

## Orders Placed — 2026-05-06

### Stocks

| Symbol | Side | Qty | Order ID | Notes |
|---|---|---|---|---|
| AAPL | Buy | 3.021 | 1cd3dbf1-98cb-4806-a8fb-b2927ae29b73 | Filled |
| APLE | Buy | 78.581 | 65c844b7-addb-416d-8e6b-4d6c97049b0b | Filled |
| CTAS | Buy | 0.011 | 4c7c2dfc-a083-4104-b20c-4ea238fbe7b9 | Filled |
| ECC | Buy | 820 | c6cac34f-1d3d-48bd-9951-e032163ac68e | Rounded from 820.177 (not fractionable) |
| KO | Buy | 0.211 | 5b0f57ff-5f7f-4c8c-a5db-8855eb1f14cd | Filled |
| QQQ | Buy | 9.082 | 483d2252-73a1-4ebe-91a1-acb915a4b039 | Filled |
| TRTX | Buy | 1.494 | 8e27bed1-41ba-439e-bc37-b97772d812d3 | Filled |

### Options — GDX Bull Call Spread ($95/$105, Oct 16 2026)

| Action | Symbol | Qty | Order ID | Notes |
|---|---|---|---|---|
| Buy to open | GDX261016C00095000 | 2 | 96f41aa5-6953-470e-98a5-42d28ec2d7f7 | Filled (single-leg) |
| Buy to open | GDX261016C00095000 | 2 | e8af694a (mleg leg) | Filled (duplicate — see below) |
| Sell to open | GDX261016C00105000 | 2 | 2ebf3dd8 (mleg leg) | Filled |
| **Sell to close** | GDX261016C00095000 | 2 | 3e3d8d7d-0724-401b-914c-32c7f14c1e85 | Corrects duplicate — net $95 long = 2 |

**Net option position: long 2x GDX261016C00095000 / short 2x GDX261016C00105000**

---

## Position Snapshot — 2026-05-07

| Symbol | Qty | Avg Entry | Current Price | Market Value | Unrealized P/L |
|---|---|---|---|---|---|
| AAPL | 3.021 | $286.85 | $287.69 | $869.11 | +$2.54 |
| APLE | 78.581 | $13.94 | $13.30 | $1,045.13 | -$50.29 |
| CTAS | 0.011 | $169.24 | $190.28 | $2.09 | +$0.23 |
| ECC | 820 | $4.23 | $4.22 | $3,460.40 | -$8.20 |
| KO | 0.211 | $78.83 | $79.27 | $16.73 | +$0.09 |
| QQQ | 9.082 | $692.86 | $696.96 | $6,329.79 | +$37.24 |
| TRTX | 1.494 | $8.61 | $8.59 | $12.83 | -$0.03 |
| GDX261016C00095000 | +2 | $10.70 | $9.90 | $1,980.00 | -$160.00 |
| GDX261016C00105000 | -2 | $6.50 | $7.45 | -$1,490.00 | -$190.00 |
| **TOTAL** | | | | **$12,226.08** | **-$368.42** |

---

## Notes

- GDX forms a **bull call spread**: long $95 / short $105, expiry Oct 16 2026
- ECC qty rounded down to whole shares (not fractionable on Alpaca)
- Margin debit (~$4,008) from source account not replicated — paper account starts at $100k
- Original Fidelity portfolio net value: $8,366.97
