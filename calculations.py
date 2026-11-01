import math


def forecast_summary(daily_demand, horizon=14):
    """Compare a trailing-mean forecast with a last-observation naive baseline."""
    values = [max(0.0, float(value)) for value in daily_demand]
    if len(values) < 14:
        return {"available": False, "reason": "At least 14 daily observations are required."}

    holdout_size = min(14, max(7, len(values) // 5))
    training = values[:-holdout_size]
    actual = values[-holdout_size:]
    moving_average = sum(training[-28:]) / min(len(training), 28)
    naive = training[-1]

    def metrics(prediction):
        errors = [abs(observed - prediction) for observed in actual]
        denominator = sum(actual)
        return {
            "mae": round(sum(errors) / len(actual), 3),
            "wape": round(sum(errors) / denominator, 4) if denominator else None,
        }

    avg_metrics = metrics(moving_average)
    naive_metrics = metrics(naive)
    selected = moving_average if avg_metrics["mae"] <= naive_metrics["mae"] else naive
    selected_name = "28-day moving average" if selected == moving_average else "naive"
    return {
        "available": True,
        "model": selected_name,
        "observations": len(values),
        "holdout_days": holdout_size,
        "mae": min(avg_metrics["mae"], naive_metrics["mae"]),
        "wape": avg_metrics["wape"] if selected == moving_average else naive_metrics["wape"],
        "daily_forecast": round(selected, 3),
        "horizon": horizon,
        "forecast_total": round(selected * horizon, 3),
        "backtest": {
            "28-day moving average": avg_metrics,
            "naive": naive_metrics,
        },
    }


def replenishment(available_milli, daily_demand, lead_time_days,
                  minimum_order_milli, order_multiple_milli, service_z=1.645):
    """Return a reorder point and an order-up-to recommendation in milli-units."""
    if len(daily_demand) < 14:
        return {"available": False, "reason": "At least 14 daily observations are required."}
    values = [max(0.0, float(value)) for value in daily_demand]
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    daily_std = math.sqrt(variance)
    lead_time_days = max(0, int(lead_time_days))
    safety_stock = service_z * daily_std * math.sqrt(lead_time_days)
    reorder_point = mean * lead_time_days + safety_stock
    target_stock = mean * (lead_time_days + 14) + safety_stock
    raw_order = max(0, target_stock * 1000 - int(available_milli))
    multiple = max(1, int(order_multiple_milli))
    order = int(math.ceil(raw_order / multiple) * multiple) if raw_order else 0
    if order and minimum_order_milli:
        order = max(order, int(minimum_order_milli))
        order = int(math.ceil(order / multiple) * multiple)
    return {
        "available": True,
        "average_daily_demand": round(mean, 3),
        "daily_demand_std_dev": round(daily_std, 3),
        "lead_time_days": lead_time_days,
        "service_level": 0.95,
        "safety_stock": round(safety_stock, 3),
        "reorder_point": round(reorder_point, 3),
        "recommended_order_milli": order,
        "projected_stockout_days": (
            round(max(0, int(available_milli)) / (mean * 1000), 1) if mean else None
        ),
    }
