import os
import logging
from datetime import datetime
from typing import Dict, Any
from twilio.rest import Client

logger = logging.getLogger("sentrix-notifications")

def send_owner_alert_notification(business_id: str, flag: Dict[str, Any], order: Dict[str, Any], supabase: Any):
    """
    Sends real-time WhatsApp alert to the store owner using Twilio WhatsApp Sandbox
    when High or Critical anomalies are detected, and logs details in the notifications table.
    """
    logger.info(f"Triggering owner alert notification for business: {business_id}, order: {order.get('id')}")
    
    if not supabase:
        logger.error("Supabase client not available for notifications logging.")
        return

    # 1. Fetch business details
    try:
        biz_res = supabase.table("businesses").select("name, owner_phone, owner_email").eq("id", business_id).execute()
        if not biz_res.data:
            logger.error(f"Business not found for ID: {business_id}")
            return
            
        business = biz_res.data[0]
        owner_phone = business.get("owner_phone")
        owner_email = business.get("owner_email")
        business_name = business.get("name", "Sentrix Store")
    except Exception as e:
        logger.error(f"Error fetching business details for notification: {str(e)}")
        return

    # Check if we have recipient phone or email
    recipient = owner_phone or owner_email or "Store Owner"
    channel = "whatsapp" if owner_phone else "email"
    
    message_body = (
        f"⚠️ *Sentrix AI Alert: {flag.get('severity', 'HIGH').upper()} Anomaly Flagged*\n\n"
        f"• *Store:* {business_name}\n"
        f"• *Order ID:* {order.get('id', '')[:8]}\n"
        f"• *Value:* ₹{order.get('total_value', 0.0):.2f}\n"
        f"• *Reason:* {flag.get('llm_reasoning', '')}\n"
        f"• *Recommended Action:* {flag.get('recommended_action', 'hold_for_review').upper()}\n\n"
        f"Please check your Sentrix Review Panel to approve or reject."
    )

    # 2. Twilio WhatsApp notification dispatch
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_wa = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
    
    success = False
    error_msg = None
    
    # If owner phone is configured, try sending Twilio WhatsApp Sandbox message
    if owner_phone and sid and token and not sid.startswith("ACxxxx"):
        try:
            # Clean recipient phone number (ensure prefix format matches Twilio)
            to_wa = owner_phone if owner_phone.startswith("whatsapp:") else f"whatsapp:+{owner_phone.replace('+', '')}"
            
            client = Client(sid, token)
            client.messages.create(
                body=message_body,
                from_=from_wa,
                to=to_wa
            )
            success = True
            logger.info(f"Twilio WhatsApp alert successfully sent to owner phone: {to_wa}")
        except Exception as e:
            error_msg = f"Twilio API error: {str(e)}"
            logger.error(error_msg, exc_info=True)
    else:
        error_msg = "Twilio credentials or owner phone number not configured in environment."
        logger.warning(error_msg)

    # 3. Log notification audit details to the database
    try:
        supabase.table("notifications").insert({
            "business_id": business_id,
            "anomaly_flag_id": flag.get("id"),
            "channel": channel,
            "recipient": recipient,
            "message": message_body,
            "status": "sent" if success else "failed",
            "sent_at": datetime.utcnow().isoformat()
        }).execute()
        logger.info(f"Logged notification dispatch status ('{'sent' if success else 'failed'}') in database.")
    except Exception as e:
        logger.error(f"Error logging notification record to Supabase: {str(e)}")
