from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
import httpx
import os
from app.database import get_db
from app import models, schemas

router = APIRouter(prefix="/whatsapp-config", tags=["whatsapp-config"])

# Evolution API configuration
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY")

async def call_evolution_api(method: str, endpoint: str, data: dict = None):
    """Helper function to call Evolution API"""
    if not EVOLUTION_API_URL or not EVOLUTION_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Evolution API configuration missing. Check EVOLUTION_API_URL and EVOLUTION_API_KEY environment variables."
        )
    
    headers = {
        "Content-Type": "application/json",
        "apikey": EVOLUTION_API_KEY
    }
    
    async with httpx.AsyncClient() as client:
        if method.upper() == "GET":
            response = await client.get(f"{EVOLUTION_API_URL}{endpoint}", headers=headers)
        elif method.upper() == "POST":
            response = await client.post(f"{EVOLUTION_API_URL}{endpoint}", headers=headers, json=data)
        elif method.upper() == "DELETE":
            response = await client.delete(f"{EVOLUTION_API_URL}{endpoint}", headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
    
    if response.status_code not in [200, 201]:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Evolution API error: {response.text}"
        )
    
    return response.json()

@router.post("/", response_model=schemas.WhatsAppConfig, status_code=status.HTTP_201_CREATED)
async def create_whatsapp_config(whatsapp_config: schemas.WhatsAppConfigCreate, db: Session = Depends(get_db)):
    # Check if customer exists
    customer = db.query(models.Customer).filter(models.Customer.id == whatsapp_config.customer_id).first()
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Customer not found"
        )
    
    # Check if customer already has WhatsApp config
    existing_config = db.query(models.CustomerWhatsAppConfig).filter(
        models.CustomerWhatsAppConfig.customer_id == whatsapp_config.customer_id
    ).first()
    
    if existing_config:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="WhatsApp configuration already exists for this customer"
        )
    
    # Create instance name from customer email (sanitized)
    instance_name = customer.email.replace("@", "_").replace(".", "_").lower()
    
    # First check if instance already exists in Evolution API
    try:
        # Try to get the instance status first
        status_response = await call_evolution_api("GET", f"/instance/connectionState/{instance_name}")
        instance_id = instance_name
        print(f"Instance {instance_id} already exists")
    except Exception:
        # Instance doesn't exist, create it
        evolution_data = {
            "instanceName": instance_name,
            "integration": "WHATSAPP-BAILEYS"
        }
        
        try:
            evolution_response = await call_evolution_api("POST", "/instance/create", evolution_data)
            instance_id = evolution_response.get("instance", {}).get("instanceName", instance_name)
            print(f"Created new instance {instance_id}")
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create Evolution API instance: {str(e)}"
            )
    
    # Evolution API secrets are stored in environment variables
    # No additional secret storage needed for Evolution API integration
    
    # Save to database
    db_config = models.CustomerWhatsAppConfig(
        customer_id=whatsapp_config.customer_id,
        instance_name=instance_name,
        evolution_instance_id=instance_id,
        connection_status="disconnected",
        is_active=whatsapp_config.is_active
    )
    
    db.add(db_config)
    db.commit()
    db.refresh(db_config)
    return db_config

@router.get("/customer/{customer_id}", response_model=Optional[schemas.WhatsAppConfig])
def get_customer_whatsapp_config(customer_id: int, db: Session = Depends(get_db)):
    config = db.query(models.CustomerWhatsAppConfig).filter(
        models.CustomerWhatsAppConfig.customer_id == customer_id
    ).first()
    return config

@router.get("/customer/{customer_id}/manager-url")
async def get_evolution_manager_url(customer_id: int, db: Session = Depends(get_db)):
    config = db.query(models.CustomerWhatsAppConfig).filter(
        models.CustomerWhatsAppConfig.customer_id == customer_id
    ).first()
    
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="WhatsApp configuration not found"
        )
    
    evolution_manager_url = f"{EVOLUTION_API_URL}/manager"
    
    return {
        "manager_url": evolution_manager_url,
        "instance_name": config.instance_name,
        "instance_id": config.evolution_instance_id,
        "instructions": "Open the Evolution Manager to view QR code and manage your WhatsApp instance"
    }

@router.get("/customer/{customer_id}/qr-code", response_model=schemas.QRCodeResponse)
async def get_whatsapp_qr_code(customer_id: int, db: Session = Depends(get_db)):
    config = db.query(models.CustomerWhatsAppConfig).filter(
        models.CustomerWhatsAppConfig.customer_id == customer_id
    ).first()
    
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="WhatsApp configuration not found"
        )
    
    if not config.evolution_instance_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Evolution API instance not configured"
        )
    
    import asyncio
    
    try:
        # Check current status first
        status_response = await call_evolution_api("GET", f"/instance/connectionState/{config.evolution_instance_id}")
        current_state = status_response.get("instance", {}).get("state", "unknown")
        
        # If not in connecting state, logout first to trigger QR code generation
        if current_state != "connecting":
            try:
                logout_response = await call_evolution_api("DELETE", f"/instance/logout/{config.evolution_instance_id}")
                print(f"Logout response: {logout_response}")
                # Wait for logout to complete
                await asyncio.sleep(2)
            except Exception as logout_error:
                print(f"Logout failed (may be normal): {logout_error}")
        
        # First try to get QR code from Evolution API
        qr_code = ""
        try:
            connect_response = await call_evolution_api("GET", f"/instance/connect/{config.evolution_instance_id}")
            
            # Extract QR code if available
            if "base64" in connect_response:
                qr_code = connect_response.get("base64", "")
            elif "qrcode" in connect_response:
                if isinstance(connect_response["qrcode"], dict):
                    qr_code = connect_response["qrcode"].get("base64", "")
                else:
                    qr_code = connect_response["qrcode"]
            elif "qr" in connect_response:
                qr_code = connect_response.get("qr", "")
            
            print(f"API QR code length: {len(qr_code)}")
        except Exception as api_error:
            print(f"API QR extraction failed: {api_error}")
        
        # If API didn't provide QR code, we'll return empty for now
        if not qr_code:
            print(f"No QR code available from Evolution API for instance: {config.evolution_instance_id}")
            print("Try refreshing or check Evolution Manager directly")
        
        # Get updated status
        status_response = await call_evolution_api("GET", f"/instance/connectionState/{config.evolution_instance_id}")
        current_state = status_response.get("instance", {}).get("state", "unknown")
        
        # Update database
        config.qr_code = qr_code
        config.connection_status = current_state
        db.commit()
        
        return schemas.QRCodeResponse(
            qr_code=qr_code,
            instance_id=config.evolution_instance_id,
            status=current_state
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get QR code: {str(e)}"
        )

@router.get("/customer/{customer_id}/status")
async def get_whatsapp_status(customer_id: int, db: Session = Depends(get_db)):
    config = db.query(models.CustomerWhatsAppConfig).filter(
        models.CustomerWhatsAppConfig.customer_id == customer_id
    ).first()
    
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="WhatsApp configuration not found"
        )
    
    if not config.evolution_instance_id:
        return {"status": "not_configured", "connection_status": "disconnected"}
    
    try:
        # Get status from Evolution API
        status_response = await call_evolution_api("GET", f"/instance/connectionState/{config.evolution_instance_id}")
        
        connection_status = status_response.get("instance", {}).get("state", "disconnected")
        
        # Update database status
        config.connection_status = connection_status
        db.commit()
        
        return {
            "status": "configured",
            "connection_status": connection_status,
            "instance_id": config.evolution_instance_id
        }
        
    except Exception as e:
        return {
            "status": "error",
            "connection_status": "error",
            "error": str(e)
        }

@router.delete("/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_whatsapp_config(config_id: int, db: Session = Depends(get_db)):
    config = db.query(models.CustomerWhatsAppConfig).filter(models.CustomerWhatsAppConfig.id == config_id).first()
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="WhatsApp configuration not found"
        )
    
    # Delete instance from Evolution API if exists
    if config.evolution_instance_id:
        try:
            await call_evolution_api("DELETE", f"/instance/delete/{config.evolution_instance_id}")
        except Exception as e:
            print(f"Warning: Failed to delete Evolution API instance: {e}")
    
    db.delete(config)
    db.commit()
    return None

