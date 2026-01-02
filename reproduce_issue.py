import json

def process_tcmb_items(items):
    if not items:
        return items
    
    # helper to check if a value is effectively null
    # The API might return None, "null", or empty string. The example shows null (None in python)
    
    # We need to maintain the last known non-null value for each key
    last_known_values = {}
    
    # Keys to track. We can dynamically discover them or just track all keys in the first item.
    # Dynamically tracking all keys seen so far is safer.
    
    processed_items = []
    
    for item in items:
        new_item = item.copy()
        
        for key, value in item.items():
            # If value is present, update last_known
            # We treat None as missing. 
            if value is not None:
                last_known_values[key] = value
            
            # If value is None, try to use last_known
            elif key in last_known_values:
                new_item[key] = last_known_values[key]
                
        processed_items.append(new_item)
        
    return processed_items

def run_test():
    data = {
        "totalCount": 33,
        "items": [
            {
                "Tarih": "31-12-2025",
                "TP_DK_USD_A_YTL": "42.8623",
                "TP_DK_USD_S_YTL": "42.9395",
                "TP_DK_EUR_A_YTL": "50.4532",
                "TP_DK_EUR_S_YTL": "50.5441",
                "UNIXTIME": { "$numberLong": "1767128400" }
            },
            {
                "Tarih": "01-01-2026",
                "TP_DK_USD_A_YTL": None,
                "TP_DK_USD_S_YTL": None,
                "TP_DK_EUR_A_YTL": None,
                "TP_DK_EUR_S_YTL": None,
                "UNIXTIME": { "$numberLong": "1767214800" }
            },
            {
                "Tarih": "02-01-2026",
                "TP_DK_USD_A_YTL": "42.8457",
                "TP_DK_USD_S_YTL": "42.9229",
                "TP_DK_EUR_A_YTL": "50.2859",
                "TP_DK_EUR_S_YTL": "50.3765",
                "UNIXTIME": { "$numberLong": "1767301200" }
            }
        ]
    }

    print("Running process_tcmb_items logic...")
    processed = process_tcmb_items(data["items"])
    
    # Check 01-01-2026 (index 1)
    target = processed[1]
    
    print(f"Date: {target['Tarih']}")
    print(f"USD A: {target['TP_DK_USD_A_YTL']} (Expected: 42.8623)")
    
    assert target['TP_DK_USD_A_YTL'] == "42.8623", f"Expected 42.8623, got {target['TP_DK_USD_A_YTL']}"
    assert target['TP_DK_EUR_S_YTL'] == "50.5441", f"Expected 50.5441, got {target['TP_DK_EUR_S_YTL']}"
    
    print("SUCCESS: Null values were filled correctly.")

if __name__ == "__main__":
    run_test()
