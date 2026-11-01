# Demand forecasting methodology

## Implemented experiment

The application uses recorded daily demand by product, with optional warehouse attribution. For each planning calculation, it constructs the trailing 180 calendar-day series. Missing calendar dates are zero-filled. At least 14 distinct recorded dates are required before forecast and reorder outputs are presented.

Two transparent baselines are compared:

1. **Trailing mean:** average of up to the 28 days before the holdout; forecast this constant across the holdout and horizon.
2. **Naive:** repeat the last training observation across the holdout and horizon.

The final up-to-14-day holdout is kept chronologically after the training series. The model with lower holdout MAE is used. MAE is always reported; WAPE is reported only when total holdout demand is nonzero.

## Reproducibility and interpretation

The implementation is deterministic for fixed observations and current date. The experiment is recalculated from the current database; it does not persist run IDs, dataset hashes, selected model versions, training windows, or historical output snapshots. Therefore, a previous result cannot currently be independently reconstructed after data changes.

No measured accuracy result is included in this document. The application computes results for the signed-in organization and displays those values. The demo organization's demand is synthetic and must not be interpreted as customer or market evidence.

## Known methodological limits

- Missing dates are treated as zero rather than distinguished from missing data or closed periods.
- Stockout-censored demand, returns, promotions, product lifecycle, seasonal candidate models, intermittent-demand models, and forecast intervals are not handled.
- The chronological holdout is a single split rather than rolling-origin evaluation.
- MAE-based selection can favor a model with poor relative performance on low-volume items.
- Reorder safety stock uses population standard deviation and a fixed lead time. Demand independence, normality, and lead-time stability are assumptions, not validated findings.

The next defensible experiment should add a validated daily demand import with explicit missing/stockout flags, rolling-origin backtests, per-SKU baseline comparisons, and saved input/model versions before introducing more complex forecasting models.
