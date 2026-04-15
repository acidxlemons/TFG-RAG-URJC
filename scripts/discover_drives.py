"""
Script para descubrir las bibliotecas de documentos (drives) en un sitio SharePoint.
Usa las credenciales de Azure del archivo .env
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")

# Site ID del sitio a explorar — obtén el tuyo desde:
# GET https://graph.microsoft.com/v1.0/sites?search=<nombre-sitio>
# o desde Graph Explorer: https://developer.microsoft.com/en-us/graph/graph-explorer
SITE_ID = os.getenv("SHAREPOINT_SITE_ID", "tuempresa.sharepoint.com,XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX,YYYYYYYY-YYYY-YYYY-YYYY-YYYYYYYYYYYY")

def get_access_token():
    """Obtiene token de acceso de Azure AD"""
    url = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": AZURE_CLIENT_ID,
        "client_secret": AZURE_CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials"
    }
    response = requests.post(url, data=data)
    response.raise_for_status()
    return response.json()["access_token"]

def list_drives(site_id: str, token: str):
    """Lista todas las bibliotecas de documentos (drives) en un sitio"""
    url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

def main():
    print("[*] Obteniendo token de Azure AD...")
    token = get_access_token()
    print("[OK] Token obtenido\n")
    
    print(f"[*] Listando drives en sitio: {SITE_ID}\n")
    drives = list_drives(SITE_ID, token)
    
    print("=" * 60)
    print("BIBLIOTECAS DE DOCUMENTOS ENCONTRADAS:")
    print("=" * 60)
    
    for drive in drives.get("value", []):
        print(f"\n[DRIVE] Nombre: {drive.get('name')}")
        print(f"        Drive ID: {drive.get('id')}")
        print(f"        Tipo: {drive.get('driveType')}")
        print(f"        Web URL: {drive.get('webUrl')}")
    
    print("\n" + "=" * 60)
    print("USA el 'Drive ID' correspondiente en la configuracion (sharepoint_sites.json)")
    print("=" * 60)

if __name__ == "__main__":
    main()
