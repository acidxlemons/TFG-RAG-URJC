import os
import logging
from typing import List, Optional
from fastapi import APIRouter, BackgroundTasks, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

class SharePointNotificationItem(BaseModel):
    subscriptionId: str
    clientState: str
    resource: str
    changeType: str

class SharePointWebhookPayload(BaseModel):
    value: List[SharePointNotificationItem]

async def process_sharepoint_change(resource: str, change_type: str):
    """Procesa cambio de SharePoint (placeholder)"""
    logger.info(f"Procesando cambio de SharePoint: {change_type} - {resource}")
    pass

@router.post("/sharepoint")
async def sharepoint_webhook(
    payload: SharePointWebhookPayload,
    background_tasks: BackgroundTasks,
    validation_token: Optional[str] = Query(None, alias="validationtoken"),
):
    """Endpoint para webhooks de SharePoint"""
    if validation_token:
        logger.info("Validando webhook de SharePoint...")
        return {"validationResponse": validation_token}

    for notification in payload.value:
        logger.info(f"Notificación de SharePoint: {notification.changeType} en {notification.resource}")
        expected_state = os.getenv("WEBHOOK_SECRET", "changeme")
        if notification.clientState != expected_state:
            logger.warning("clientState inválido en notificación de SharePoint")
            continue

        background_tasks.add_task(
            process_sharepoint_change,
            resource=notification.resource,
            change_type=notification.changeType,
        )

    return {"status": "accepted"}
