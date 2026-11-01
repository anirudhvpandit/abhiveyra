# KPI dictionary

All metrics are organization-scoped and use current persisted records. Currency is currently displayed as USD. A zero value means no matching records or zero recorded quantity; it is not an estimate of unobserved business activity.

| KPI | Formula / source | Period and interpretation | Limitation |
|---|---|---|---|
| Inventory value | Sum of `on_hand_milli × product.unit_cost_cents`, converted from milli-cents to dollars | Current snapshot; physical on-hand at the current manually maintained unit cost | Not an accounting valuation method; excludes landed costs, historical cost layers, and FX |
| On-hand units | Sum of on-hand quantity across organization balances | Current snapshot across warehouses | Adds unlike units of measure; use as an operational total only |
| Reserved units | Sum of reserved quantity across balances | Current snapshot | Reservation workflow is not implemented |
| Available units | On-hand minus reserved | Current snapshot by balance | Does not include inbound POs |
| Open PO value | Sum of outstanding PO quantity × PO line unit cost | Current open/partial orders | Not a liability or invoice reconciliation |
| Replenishment signals | Count of warehouse/product recommendations with recorded demand and positive rounded order quantity | Current calculation; trailing 180 calendar days of recorded observations | Fixed lead-time and 95% service-level assumptions |
| MAE | Mean absolute difference between holdout actuals and a constant baseline prediction | Chronological holdout, up to 14 days | Does not indicate calibration or guarantee future error |
| WAPE | Sum of absolute errors divided by sum of actual holdout demand | Same holdout | Undefined and omitted when total actual demand is zero |
| Supplier order counts | Count of organization purchase orders by supplier | All recorded PO history | Not a delivery-performance score |

Metrics not present in the interface include inventory turnover, days inventory outstanding, fill rate, service level, carrying cost, excess stock value, supplier on-time performance, cash released, and profit savings. Required accounting, fulfilled demand, receipt timing, and cost inputs do not yet exist or are incomplete. No financial benefits are fabricated.
