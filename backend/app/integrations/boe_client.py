"""
Compatibility shim: BoeClient ahora es un alias de BoeConnector.
Todo el código BOE consolidado está en boe_connector.py.
"""
from app.integrations.boe_connector import BoeConnector

# Alias para compatibilidad con imports existentes
BoeClient = BoeConnector
