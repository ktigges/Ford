"""
Load and use the trained energy consumption model for predictions.

This module provides a simple API to:
1. Load the trained model and scaler from disk
2. Make predictions given trip features
3. Handle model versioning and error cases

Used by:
- Trip planner service (/predict/trip)
- Chat bot backend (/chat)
- Analytics dashboard

Author:      Kevin Tigges
Date:        2026-05-09
"""

import json
import logging
from pathlib import Path

from joblib import load

log = logging.getLogger(__name__)

# ── Model paths ────────────────────────────────────────────────────

MODEL_DIR = Path("models")
MODEL_PATH = MODEL_DIR / "energy_model.pkl"
SCALER_PATH = MODEL_DIR / "energy_scaler.pkl"
SCHEMA_PATH = MODEL_DIR / "energy_model_schema.json"

# ── Global state (lazy loaded) ────────────────────────────────────

_model = None
_scaler = None
_schema = None


def is_available() -> bool:
    """Check if model files exist and are accessible."""
    return all([MODEL_PATH.exists(), SCALER_PATH.exists(), SCHEMA_PATH.exists()])


def load_model():
    """Load model, scaler, and schema from disk (lazy loading)."""
    global _model, _scaler, _schema
    
    if _model is not None:
        return _model, _scaler, _schema
    
    if not is_available():
        raise FileNotFoundError(
            f"Model files not found in {MODEL_DIR}. "
            "Run 'python train_energy_model.py' first."
        )
    
    log.info(f"Loading model from {MODEL_PATH}")
    _model = load(MODEL_PATH)
    _scaler = load(SCALER_PATH)
    
    with open(SCHEMA_PATH, "r") as f:
        _schema = json.load(f)
    
    log.info(f"Model loaded: {_schema['model_type']} trained on {_schema['num_training_drives']} drives")
    
    return _model, _scaler, _schema


def predict_energy(
    distance_km: float,
    avg_speed_kmh: float,
    avg_ambient_temp_c: float = 20,
    avg_outside_temp_c: float = 20,
    duration_min: float | None = None,
    soc_drop_percent: float | None = None,
    **kwargs
) -> dict:
    """Predict energy consumption for a trip.
    
    Args:
        distance_km: Trip distance in kilometers
        avg_speed_kmh: Average speed during trip
        avg_ambient_temp_c: Average ambient (cabin) temperature
        avg_outside_temp_c: Average outside temperature
        duration_min: Trip duration in minutes (auto-calculated if not provided)
        soc_drop_percent: Expected SOC drop (auto-calculated if not provided)
        **kwargs: Additional optional features (max_speed_kmh, speed_variance, etc.)
    
    Returns:
        {
            "energy_used_kwh": float,      # Predicted energy consumption
            "trip_efficiency_kmh": float,  # Predicted km/kWh
            "confidence": str,             # "high", "medium", "low"
            "used_defaults": [str],        # Which features used defaults
            "model_info": {
                "training_date": str,
                "num_training_drives": int,
                "test_mae_kwh": float,
            }
        }
    """
    model, scaler, schema = load_model()
    
    used_defaults = []
    
    # Auto-calculate duration if not provided
    if duration_min is None:
        # Assume constant average speed
        duration_min = (distance_km / avg_speed_kmh * 60) if avg_speed_kmh > 0 else 0
        used_defaults.append("duration_min")
    
    # Auto-calculate SOC drop if not provided
    # Rough estimate: heavier trips use more battery
    if soc_drop_percent is None:
        soc_drop_percent = min(100, (distance_km / 200) * 40)  # ~0.2% per km baseline
        used_defaults.append("soc_drop_percent")
    
    # Get optional features with defaults
    max_speed_kmh = kwargs.get("max_speed_kmh", avg_speed_kmh * 1.5)  # Assume 50% above avg
    speed_variance = kwargs.get("speed_variance", avg_speed_kmh * 0.3)  # Assume 30% variance
    acceleration_aggression = kwargs.get("acceleration_aggression", 0)
    regen_kwh = kwargs.get("regen_kwh", distance_km * 0.1)  # Assume 10% regen
    trip_efficiency_kmh = kwargs.get("trip_efficiency_kmh", None)
    
    # Calculate trip_efficiency_kmh if not provided
    if trip_efficiency_kmh is None:
        trip_efficiency_kmh = distance_km / max(0.1, duration_min / 60 * avg_speed_kmh)
        used_defaults.append("trip_efficiency_kmh")
    
    # Build feature vector in schema order
    features = [
        distance_km,
        duration_min,
        avg_speed_kmh,
        max_speed_kmh,
        speed_variance,
        acceleration_aggression,
        avg_ambient_temp_c,
        avg_outside_temp_c,
        soc_drop_percent,
        regen_kwh,
        trip_efficiency_kmh,
    ]
    
    # Scale and predict
    import numpy as np
    X_scaled = scaler.transform([features])
    energy_predicted_kwh = float(model.predict(X_scaled)[0])
    
    # Clamp to reasonable range
    min_energy = schema["feature_scales"]["energy_used_kwh"]["min"]
    max_energy = schema["feature_scales"]["energy_used_kwh"]["max"]
    energy_predicted_kwh = max(min_energy * 0.5, min(max_energy * 2, energy_predicted_kwh))
    
    # Confidence based on how much we extrapolated
    in_training_range = (
        schema["feature_scales"]["distance_km"]["min"] <= distance_km <= schema["feature_scales"]["distance_km"]["max"]
        and schema["feature_scales"]["avg_speed_kmh"]["min"] <= avg_speed_kmh <= schema["feature_scales"]["avg_speed_kmh"]["max"]
    )
    
    if in_training_range and not used_defaults:
        confidence = "high"
    elif in_training_range:
        confidence = "medium"
    else:
        confidence = "low"
    
    return {
        "energy_used_kwh": energy_predicted_kwh,
        "trip_efficiency_kmh": energy_predicted_kwh / distance_km if distance_km > 0 else 0,
        "confidence": confidence,
        "used_defaults": used_defaults,
        "model_info": {
            "training_date": schema["training_date"],
            "num_training_drives": schema["num_training_drives"],
            "test_mae_kwh": 1.4461,  # From training output
        }
    }


def retrain_available() -> bool:
    """Check if there's new training data available since last training."""
    if not SCHEMA_PATH.exists():
        return False
    
    with open(SCHEMA_PATH, "r") as f:
        schema = json.load(f)
    
    # TODO: Query database for drives created after training_date
    # For now, always return True (user can run manually)
    return True


# ── Testing ────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Test prediction
    print("Testing energy prediction model...\n")
    
    result = predict_energy(
        distance_km=30,
        avg_speed_kmh=15,
        avg_ambient_temp_c=18,
        avg_outside_temp_c=10
    )
    
    print(f"Trip: 30 km at 15 km/h, temp 10°C")
    print(f"Predicted energy: {result['energy_used_kwh']:.2f} kWh")
    print(f"Trip efficiency: {result['trip_efficiency_kmh']:.2f} km/kWh")
    print(f"Confidence: {result['confidence']}")
    print(f"Model info: {result['model_info']}")
