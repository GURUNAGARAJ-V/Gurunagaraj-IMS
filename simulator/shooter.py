import requests
import threading

API_URL = "[http://127.0.0.1:8000/ingest](http://127.0.0.1:8000/ingest)"

def fire_signals(component_id, severity, count):
    """Fires a specified number of payloads to simulate a system crash."""
    print(f"Firing {count} signals for {component_id}...")
    for i in range(count):
        payload = {
            "component_id": component_id, 
            "severity": severity, 
            "payload": {"error": "Timeout", "trace": str(i)}
        }
        requests.post(API_URL, json=payload)

if __name__ == "__main__":
    # Simulate simultaneous failures across different components
    t1 = threading.Thread(target=fire_signals, args=("RDBMS_NODE_01", "P0_CRITICAL", 200))
    t2 = threading.Thread(target=fire_signals, args=("CACHE_CLUSTER_05", "P2_WARNING", 150))
    
    t1.start()
    t2.start()
    
    t1.join()
    t2.join()
    
    print("\nCheck backend console! 350 errors sent, but only 2 Tickets should be created due to debouncing.")
