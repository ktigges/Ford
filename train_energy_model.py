"""Ford Lightning source file.

Author: Kevin Tigges
Copyright (c) 2026 Kevin Tigges
License: Open source prototype software
Notice: Use at your own risk.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from joblib import dump, load
from sklearn.model_selection import train_test_split, cross_val_score
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

import db

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────

MODEL_DIR = Path("models")
MODEL_PATH = MODEL_DIR / "energy_model.pkl"
SCALER_PATH = MODEL_DIR / "energy_scaler.pkl"
SCHEMA_PATH = MODEL_DIR / "energy_model_schema.json"

MIN_DRIVES = 3  # Minimum drives required to train
MIN_DRIVE_DISTANCE_KM = 5  # Filter out short drives (parking lot movements)

# ── Feature Engineering ────────────────────────────────────────────

def extract_drive_features(drive_id: int, drive_row: dict, drive_points: list[dict]) -> dict | None:
    """Extract ML features from a single drive.
    
    Returns dict of features, or None if invalid/insufficient data.
    """
    if not drive_points:
        return None
    
    # Basic drive metadata
    distance_km = drive_row.get("distance_km") or 0
    duration_sec = drive_row.get("duration_sec") or 0
    energy_used_kwh = drive_row.get("energy_used_kwh") or 0
    
    # Filter out short/invalid drives
    if distance_km < MIN_DRIVE_DISTANCE_KM or duration_sec < 60 or energy_used_kwh <= 0:
        return None
    
    # Environmental
    avg_ambient_temp_c = drive_row.get("avg_ambient_temp_c")
    avg_outside_temp_c = drive_row.get("avg_outside_temp_c")
    weather_temp_c = drive_row.get("weather_temp_c")
    weather_humidity_pct = drive_row.get("weather_humidity_pct")
    weather_pressure_hpa = drive_row.get("weather_pressure_hpa")
    precipitation_mm = drive_row.get("precipitation_mm")
    wind_speed_avg_kmh = drive_row.get("wind_speed_avg_kmh")
    headwind_component_kmh = drive_row.get("headwind_component_kmh")
    tailwind_component_kmh = drive_row.get("tailwind_component_kmh")
    sidewind_component_kmh = drive_row.get("sidewind_component_kmh")
    avg_altitude_m = drive_row.get("avg_altitude_m")
    elevation_gain_m = drive_row.get("elevation_gain_m")
    elevation_loss_m = drive_row.get("elevation_loss_m")
    net_elevation_change_m = drive_row.get("net_elevation_change_m")
    
    # Derived stats from drive_points
    points_df = pd.DataFrame(drive_points)
    
    # Speed statistics
    speeds = points_df["speed_kmh"].dropna()
    if len(speeds) < 2:
        return None
    
    avg_speed = speeds.mean()
    max_speed = speeds.max()
    speed_std = speeds.std()
    
    # Acceleration/aggressiveness (speed changes per minute)
    if duration_sec > 60:
        speed_changes = speeds.diff().dropna().abs().sum()
        acceleration_aggression = speed_changes / (duration_sec / 60)
    else:
        acceleration_aggression = 0
    
    # Battery/efficiency
    socs = points_df["soc_percent"].dropna()
    if len(socs) > 1:
        soc_start = socs.iloc[0]
        soc_end = socs.iloc[-1]
        soc_drop = soc_start - soc_end
    else:
        soc_drop = (drive_row.get("start_soc_percent") or 0) - (drive_row.get("end_soc_percent") or 0)
    
    # Regen captured (if available)
    regen_kwh = drive_row.get("regen_energy_kwh") or 0
    
    # Trip efficiency: km per kWh used
    trip_efficiency_kmh = distance_km / energy_used_kwh if energy_used_kwh > 0 else 0
    
    # Duration-based features
    duration_min = duration_sec / 60
    
    features = {
        "drive_id": drive_id,
        "distance_km": distance_km,
        "duration_min": duration_min,
        "avg_speed_kmh": avg_speed,
        "max_speed_kmh": max_speed,
        "speed_variance": speed_std,
        "acceleration_aggression": acceleration_aggression,
        "avg_ambient_temp_c": avg_ambient_temp_c or 20,  # Default to ~room temp
        "avg_outside_temp_c": avg_outside_temp_c or 20,
        "weather_temp_c": weather_temp_c if weather_temp_c is not None else (avg_outside_temp_c or 20),
        "weather_humidity_pct": weather_humidity_pct if weather_humidity_pct is not None else 50,
        "weather_pressure_hpa": weather_pressure_hpa if weather_pressure_hpa is not None else 1013,
        "precipitation_mm": precipitation_mm if precipitation_mm is not None else 0,
        "wind_speed_avg_kmh": wind_speed_avg_kmh if wind_speed_avg_kmh is not None else 0,
        "headwind_component_kmh": headwind_component_kmh if headwind_component_kmh is not None else 0,
        "tailwind_component_kmh": tailwind_component_kmh if tailwind_component_kmh is not None else 0,
        "sidewind_component_kmh": sidewind_component_kmh if sidewind_component_kmh is not None else 0,
        "avg_altitude_m": avg_altitude_m if avg_altitude_m is not None else 0,
        "elevation_gain_m": elevation_gain_m if elevation_gain_m is not None else 0,
        "elevation_loss_m": elevation_loss_m if elevation_loss_m is not None else 0,
        "net_elevation_change_m": net_elevation_change_m if net_elevation_change_m is not None else 0,
        "soc_drop_percent": soc_drop,
        "regen_kwh": regen_kwh,
        "trip_efficiency_kmh": trip_efficiency_kmh,
        # Target
        "energy_used_kwh": energy_used_kwh,
    }
    
    return features


def load_training_data() -> pd.DataFrame | None:
    """Load all drives + drive_points from database and extract features.
    
    Returns DataFrame with features, or None if insufficient data.
    """
    log.info("Loading training data from database...")
    
    # Fetch all completed drives with energy data
    drives = db.fetch_all("""
        SELECT 
            id, vin, distance_km, duration_sec, energy_used_kwh,
            start_soc_percent, end_soc_percent,
            avg_ambient_temp_c, avg_outside_temp_c,
            weather_temp_c, weather_humidity_pct, weather_pressure_hpa, precipitation_mm,
            wind_speed_avg_kmh, headwind_component_kmh, tailwind_component_kmh, sidewind_component_kmh,
            avg_altitude_m, elevation_gain_m, elevation_loss_m, net_elevation_change_m,
            regen_energy_kwh, created_at
        FROM drives
        WHERE 
            in_progress = FALSE 
            AND energy_used_kwh > 0 
            AND distance_km > %s
        ORDER BY created_at DESC
    """, (MIN_DRIVE_DISTANCE_KM,))
    
    if len(drives) < MIN_DRIVES:
        log.warning(f"Insufficient drives: {len(drives)} < {MIN_DRIVES}")
        return None
    
    log.info(f"Found {len(drives)} completed drives")
    
    features_list = []
    
    for drive in drives:
        drive_id = drive["id"]
        
        # Fetch drive points for this drive
        points = db.fetch_all("""
            SELECT 
                speed_kmh, odometer_km, latitude, longitude,
                soc_percent, energy_remaining_kwh,
                battery_temp_c, recorded_at
            FROM drive_points
            WHERE drive_id = %s
            ORDER BY recorded_at ASC
        """, (drive_id,))
        
        if not points:
            continue
        
        # Extract features
        features = extract_drive_features(drive_id, drive, points)
        if features:
            features_list.append(features)
        else:
            log.debug(f"Skipped drive {drive_id}: insufficient data")
    
    if not features_list:
        log.error("No valid training data extracted")
        return None
    
    df = pd.DataFrame(features_list)
    log.info(f"Extracted {len(df)} valid drives with features")
    log.info(f"\nDataset Summary:\n{df[['distance_km', 'avg_speed_kmh', 'energy_used_kwh', 'trip_efficiency_kmh']].describe()}")
    
    return df


# ── Model Training ────────────────────────────────────────────────

def train_model(df: pd.DataFrame) -> tuple[xgb.XGBRegressor, StandardScaler]:
    """Train XGBoost energy prediction model.
    
    Returns (model, scaler).
    """
    log.info("Preparing features for training...")
    
    # Feature columns (exclude target and metadata)
    feature_cols = [
        "distance_km", "duration_min", "avg_speed_kmh", "max_speed_kmh",
        "speed_variance", "acceleration_aggression",
        "avg_ambient_temp_c", "avg_outside_temp_c",
        "weather_temp_c", "weather_humidity_pct", "weather_pressure_hpa", "precipitation_mm",
        "wind_speed_avg_kmh", "headwind_component_kmh", "tailwind_component_kmh", "sidewind_component_kmh",
        "avg_altitude_m", "elevation_gain_m", "elevation_loss_m", "net_elevation_change_m",
        "soc_drop_percent", "regen_kwh", "trip_efficiency_kmh"
    ]
    
    X = df[feature_cols].copy()
    y = df["energy_used_kwh"].copy()
    
    # Handle any remaining NaNs
    X = X.fillna(X.mean())
    
    log.info(f"Features shape: {X.shape}")
    log.info(f"Target shape: {y.shape}")
    log.info(f"Target range: {y.min():.2f} - {y.max():.2f} kWh")
    
    # Scale features
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    
    # Train/test split
    X_train, X_test, y_train, y_test = train_test_split(
        X_scaled, y, test_size=0.2, random_state=42
    )
    
    log.info(f"Train set: {len(X_train)} drives, Test set: {len(X_test)} drives")
    
    # Train XGBoost
    log.info("Training XGBoost model...")
    model = xgb.XGBRegressor(
        n_estimators=100,
        max_depth=5,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        verbosity=0
    )
    
    model.fit(X_train, y_train, verbose=False)
    
    # Evaluate
    y_pred_train = model.predict(X_train)
    y_pred_test = model.predict(X_test)
    
    train_r2 = r2_score(y_train, y_pred_train)
    test_r2 = r2_score(y_test, y_pred_test)
    test_mae = mean_absolute_error(y_test, y_pred_test)
    test_rmse = np.sqrt(mean_squared_error(y_test, y_pred_test))
    
    log.info(f"\n{'='*60}")
    log.info("Model Performance:")
    log.info(f"  Train R²: {train_r2:.4f}")
    log.info(f"  Test R²:  {test_r2:.4f}")
    log.info(f"  Test MAE: {test_mae:.4f} kWh")
    log.info(f"  Test RMSE: {test_rmse:.4f} kWh")
    log.info(f"{'='*60}\n")
    
    # Cross-validation
    log.info("Running 5-fold cross-validation...")
    cv_scores = cross_val_score(model, X_scaled, y, cv=5, scoring='r2')
    log.info(f"  CV R² scores: {cv_scores}")
    log.info(f"  Mean CV R²: {cv_scores.mean():.4f} (+/- {cv_scores.std():.4f})")
    
    # Feature importance
    log.info("\nFeature Importance:")
    importance_df = pd.DataFrame({
        "feature": feature_cols,
        "importance": model.feature_importances_
    }).sort_values("importance", ascending=False)
    
    for _, row in importance_df.iterrows():
        log.info(f"  {row['feature']:30s} {row['importance']:6.4f}")
    
    return model, scaler


# ── Model Persistence ──────────────────────────────────────────────

def save_model(model: xgb.XGBRegressor, scaler: StandardScaler, df: pd.DataFrame) -> None:
    """Save model, scaler, and metadata to disk."""
    MODEL_DIR.mkdir(exist_ok=True)
    
    # Save model and scaler
    dump(model, MODEL_PATH)
    dump(scaler, SCALER_PATH)
    
    log.info(f"Saved model to {MODEL_PATH}")
    log.info(f"Saved scaler to {SCALER_PATH}")
    
    # Save schema/metadata
    schema = {
        "model_type": "XGBRegressor",
        "target": "energy_used_kwh",
        "features": [
            "distance_km", "duration_min", "avg_speed_kmh", "max_speed_kmh",
            "speed_variance", "acceleration_aggression",
            "avg_ambient_temp_c", "avg_outside_temp_c",
            "weather_temp_c", "weather_humidity_pct", "weather_pressure_hpa", "precipitation_mm",
            "wind_speed_avg_kmh", "headwind_component_kmh", "tailwind_component_kmh", "sidewind_component_kmh",
            "avg_altitude_m", "elevation_gain_m", "elevation_loss_m", "net_elevation_change_m",
            "soc_drop_percent", "regen_kwh", "trip_efficiency_kmh"
        ],
        "feature_scales": {
            "distance_km": {"min": float(df["distance_km"].min()), "max": float(df["distance_km"].max())},
            "avg_speed_kmh": {"min": float(df["avg_speed_kmh"].min()), "max": float(df["avg_speed_kmh"].max())},
            "energy_used_kwh": {"min": float(df["energy_used_kwh"].min()), "max": float(df["energy_used_kwh"].max())},
        },
        "training_date": datetime.now().isoformat(),
        "num_training_drives": len(df),
    }
    
    with open(SCHEMA_PATH, "w") as f:
        json.dump(schema, f, indent=2)
    
    log.info(f"Saved schema to {SCHEMA_PATH}")


# ── Main ───────────────────────────────────────────────────────────

def main():
    """Main training pipeline."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    
    log.info("=" * 60)
    log.info("Ford EV Energy Model Training")
    log.info("=" * 60)
    
    # Initialize database
    db.init_pool()
    
    try:
        # Load training data
        df = load_training_data()
        if df is None or len(df) < MIN_DRIVES:
            log.error("Failed to load sufficient training data")
            return False
        
        # Train model
        model, scaler = train_model(df)
        
        # Save model and metadata
        save_model(model, scaler, df)
        
        log.info("\n✓ Training complete!")
        log.info(f"✓ Model saved to {MODEL_PATH}")
        log.info(f"✓ Ready for inference")
        
        return True
        
    except Exception as e:
        log.exception(f"Training failed: {e}")
        return False
    
    finally:
        db.close_pool()


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
