import requests
import xml.etree.ElementTree as ET
from datetime import datetime

url = "https://boe.es/datosabiertos/api/boe/sumario/20260128"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "application/xml"
}

print(f"Checking URL: {url}")
response = requests.get(url, headers=headers)

if response.status_code == 200:
    root = ET.fromstring(response.content)
    
    for seccion in root.findall(".//seccion"):
        name = seccion.get("nombre", "")
        item_count = 0
        for dep in seccion.findall("departamento"):
            item_count += len(dep.findall("item"))
        
        print(f"Section '{name}': {item_count} items")
else:
    print(f"Error: {response.status_code}")
