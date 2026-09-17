import os
import json
import logging
from datetime import datetime
import numpy as np
if not hasattr(np, "long"):
    np.long = np.int64
if not hasattr(np, "ulong"):
    np.ulong = np.uint64

import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score
import joblib

log = logging.getLogger("MLEngine")

JSON_FILE = "ml_training_data.json"
MODEL_FILE = "ml_model.joblib"

def extract_features(df):
    """Extracts features from the raw trade data for the ML model."""
    if df.empty:
        return df
        
    # Convert timestamp to datetime
    df['timestamp_entry'] = pd.to_datetime(df['timestamp_entry'])
    
    # Feature engineering
    df['hour_of_day'] = df['timestamp_entry'].dt.hour
    df['day_of_week'] = df['timestamp_entry'].dt.dayofweek
    
    # Target variable: 1 if profitable, 0 if loss or zero
    if 'net_profit_percent' in df.columns:
        df['is_profitable'] = (df['net_profit_percent'] > 0).astype(int)
        
    # Select features to use
    features = ['trade_size_usd', 'hour_of_day', 'day_of_week']
    
    return df[features], df.get('is_profitable')

def train_model():
    """Trains the ML model using the historical trade data."""
    log.info("Starting ML model retraining...")
    if not os.path.exists(JSON_FILE):
        log.warning(f"No training data found at {JSON_FILE}. Skipping training.")
        return False
        
    try:
        with open(JSON_FILE, 'r') as f:
            data = json.load(f)
            
        if len(data) < 100:
            log.info(f"Not enough data to train ML model (Need >= 100, got {len(data)}). Skipping.")
            return False
            
        # We only want to train on actioned trades (not timeouts)
        actioned_data = [t for t in data if t.get("exit_reason", "") != "TIMEOUT"]
        
        if len(actioned_data) < 100:
            log.info(f"Not enough actioned trades to train ML model (Got {len(actioned_data)}). Skipping.")
            return False
            
        df = pd.DataFrame(actioned_data)
        
        X, y = extract_features(df)
        
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        
        clf = RandomForestClassifier(n_estimators=100, random_state=42)
        clf.fit(X_train, y_train)
        
        y_pred = clf.predict(X_test)
        acc = accuracy_score(y_test, y_pred)
        log.info(f"ML Model trained successfully. Test Accuracy: {acc*100:.2f}%")
        
        joblib.dump(clf, MODEL_FILE)
        log.info(f"Model saved to {MODEL_FILE}")
        return True
        
    except Exception as e:
        log.error(f"Error training ML model: {e}")
        return False

def predict_trade(wallet: str, trade_size_usd: float) -> float:
    """
    Predicts the probability of a trade being profitable.
    Returns the probability as a float between 0.0 and 1.0.
    """
    if not os.path.exists(MODEL_FILE):
        # If model doesn't exist, we assume 100% confidence to allow trades to happen
        return 1.0
        
    try:
        clf = joblib.load(MODEL_FILE)
        
        now = datetime.utcnow()
        
        # Create a single row dataframe for prediction
        input_data = pd.DataFrame([{
            'trade_size_usd': trade_size_usd,
            'hour_of_day': now.hour,
            'day_of_week': now.weekday()
        }])
        
        # Predict probability of class '1' (profitable)
        proba = clf.predict_proba(input_data)[0]
        
        # In case the model only predicts one class
        if len(proba) == 1:
            predicted_class = clf.classes_[0]
            if predicted_class == 1:
                return 1.0
            else:
                return 0.0
                
        # proba[1] is the probability of class 1
        profit_prob = proba[1]
        
        return profit_prob
        
    except Exception as e:
        log.error(f"Error making ML prediction: {e}")
        # Default to allowing the trade on error
        return 1.0

