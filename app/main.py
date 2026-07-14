import os
import logging
from typing import Optional, Dict, Any
from pydantic import BaseModel
from fastapi import FastAPI, Request, Query, HTTPException, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from dotenv import load_dotenv

load_dotenv()

from app.services.pipeline import process_whatsapp_message_async, get_supabase
from app.schemas import WebhookVerificationResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp-webhook")

app = FastAPI(
    title="Nudge Phase 1 - Order Processing API (Twilio + Meta)",
    description="Backend API for WhatsApp webhook ingestion via Twilio Sandbox or Meta Cloud API, structured AI extraction, and order feed.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "nudge_secret_token_123")


@app.get("/", tags=["Health"])
async def root():
    return {
        "service": "Nudge Phase 1 Backend API",
        "status": "operational",
        "supported_providers": ["Twilio WhatsApp Sandbox", "Meta Cloud API"],
        "endpoints": [
            "POST /webhook/twilio/whatsapp",
            "POST /webhook/whatsapp",
            "GET /webhook/whatsapp",
            "GET /orders",
            "GET /orders/{id}",
            "POST /test/simulate-message"
        ]
    }


@app.post("/webhook/twilio/whatsapp", tags=["Twilio WhatsApp Webhook"])
async def receive_twilio_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Dedicated endpoint for Twilio WhatsApp Sandbox / Business webhook (`application/x-www-form-urlencoded` or JSON).
    Twilio expects a fast HTTP 200 response (or TwiML XML).
    """
    try:
        content_type = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type:
            form_data = await request.form()
            payload = dict(form_data)
        else:
            try:
                payload = await request.json()
            except Exception:
                form_data = await request.form()
                payload = dict(form_data)
    except Exception as e:
        logger.error(f"Error reading Twilio request body: {str(e)}")
        return Response(content="<Response></Response>", media_type="application/xml", status_code=200)

    try:
        # Twilio payload fields: MessageSid, From (e.g. whatsapp:+1415... or whatsapp:+9198...), To, Body, ProfileName
        wa_message_id = payload.get("MessageSid") or payload.get("SmsSid") or f"tw_{os.urandom(4).hex()}"
        raw_from = payload.get("From", "")
        raw_to = payload.get("To", "")
        message_text = payload.get("Body", "").strip()
        customer_name = payload.get("ProfileName", "") or raw_from.replace("whatsapp:", "")

        # Clean phone numbers
        from_phone = raw_from.replace("whatsapp:", "").replace("+", "").strip()
        phone_number_id = raw_to.replace("whatsapp:", "").replace("+", "").strip() or "twilio_sandbox"

        if from_phone and message_text:
            logger.info(f"[Twilio Webhook] Queueing message {wa_message_id} from {from_phone} ('{message_text}')")
            background_tasks.add_task(
                process_whatsapp_message_async,
                wa_message_id=wa_message_id,
                from_phone=from_phone,
                customer_name=customer_name,
                text=message_text,
                raw_payload=payload,
                phone_number_id=phone_number_id
            )
        else:
            logger.warning(f"[Twilio Webhook] Skipped payload due to missing From or Body: {payload}")
    except Exception as e:
        logger.error(f"Error parsing Twilio payload: {str(e)}", exc_info=True)

    # Return empty TwiML or 200 OK fast so Twilio knows webhook succeeded
    return Response(content="<Response></Response>", media_type="application/xml", status_code=200)


@app.get("/webhook/whatsapp", tags=["WhatsApp Webhook"])
async def verify_whatsapp_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge")
):
    """
    Verification challenge endpoint required by Meta during Webhook configuration.
    Not used by Twilio, but kept for full compatibility.
    """
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        logger.info("Webhook successfully verified by Meta challenge.")
        return int(hub_challenge) if hub_challenge and hub_challenge.isdigit() else hub_challenge
    logger.warning(f"Failed verification attempt with token: {hub_verify_token}")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification challenge failed")


@app.post("/webhook/whatsapp", tags=["WhatsApp Webhook"])
async def receive_whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Unified receiver that accepts both Meta Cloud API (`application/json` with entries)
    and Twilio Sandbox requests (`application/x-www-form-urlencoded` or JSON with MessageSid/Body).
    """
    content_type = request.headers.get("content-type", "")
    
    # Check if this request is actually from Twilio
    if "application/x-www-form-urlencoded" in content_type:
        return await receive_twilio_webhook(request, background_tasks)

    try:
        payload = await request.json()
    except Exception:
        # If JSON parsing failed, try form data just in case Twilio sent to this URL
        return await receive_twilio_webhook(request, background_tasks)

    # Check if JSON payload contains Twilio's MessageSid / Body directly
    if "MessageSid" in payload and "Body" in payload:
        return await receive_twilio_webhook(request, background_tasks)

    # Otherwise, handle Meta Cloud API nested shape
    try:
        entries = payload.get("entry", [])
        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
                
                # Extract contacts map (sender names)
                contacts = value.get("contacts", [])
                contacts_map = {
                    c.get("wa_id"): c.get("profile", {}).get("name", "") 
                    for c in contacts if "wa_id" in c
                }

                # Extract messages
                messages = value.get("messages", [])
                for msg in messages:
                    wa_message_id = msg.get("id")
                    from_phone = msg.get("from")
                    msg_type = msg.get("type")
                    
                    customer_name = contacts_map.get(from_phone, "")

                    # Extract text content across supported message types
                    message_text = None
                    if msg_type == "text":
                        message_text = msg.get("text", {}).get("body")
                    elif msg_type == "interactive":
                        interactive = msg.get("interactive", {})
                        if interactive.get("type") == "button_reply":
                            message_text = interactive.get("button_reply", {}).get("title")
                        elif interactive.get("type") == "list_reply":
                            message_text = interactive.get("list_reply", {}).get("title")

                    if wa_message_id and from_phone and message_text:
                        logger.info(f"[Meta Webhook] Queueing task for WA message {wa_message_id} from {from_phone}")
                        background_tasks.add_task(
                            process_whatsapp_message_async,
                            wa_message_id=wa_message_id,
                            from_phone=from_phone,
                            customer_name=customer_name,
                            text=message_text,
                            raw_payload=payload,
                            phone_number_id=phone_number_id
                        )
    except Exception as e:
        logger.error(f"Error parsing Meta WhatsApp payload: {str(e)}", exc_info=True)

    return JSONResponse({"status": "ok"}, status_code=200)


@app.get("/orders", tags=["Orders Dashboard"])
async def list_orders(limit: int = 50, status_filter: Optional[str] = None):
    """
    List structured orders for the Phase 1 Next.js dashboard.
    Enriches with customer and order_items details.
    """
    supabase = get_supabase()
    if not supabase:
        return {"orders": _get_mock_orders()}

    try:
        query = (
            supabase.table("orders")
            .select("*, customers(name, whatsapp_phone, total_orders, total_spend), order_items(*), whatsapp_messages(raw_text)")
            .order("created_at", desc=True)
            .limit(limit)
        )
        if status_filter and status_filter != "all":
            query = query.eq("status", status_filter)
            
        res = query.execute()
        return {"orders": res.data or []}
    except Exception as e:
        logger.error(f"Error fetching orders from Supabase: {str(e)}")
        return {"orders": _get_mock_orders(), "error": str(e)}


@app.get("/orders/{order_id}", tags=["Orders Dashboard"])
async def get_order_detail(order_id: str):
    """
    Get detailed order breakdown including line items and raw source message.
    """
    supabase = get_supabase()
    if not supabase:
        mock_list = _get_mock_orders()
        for m in mock_list:
            if m["id"] == order_id:
                return m
        raise HTTPException(status_code=404, detail="Order not found (mock mode)")

    try:
        res = (
            supabase.table("orders")
            .select("*, customers(name, whatsapp_phone, total_orders, total_spend), order_items(*), whatsapp_messages(raw_text)")
            .eq("id", order_id)
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=404, detail="Order not found")
        return res.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching order detail: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/test/simulate-message", tags=["Testing & Dev"])
async def simulate_whatsapp_message(
    background_tasks: BackgroundTasks,
    text: str = Query(..., description="The WhatsApp message text, e.g. '2kg rice, 1 box maggi, 5 packets sugar'"),
    from_phone: str = Query("919876543210", description="Customer phone number"),
    customer_name: str = Query("Simulated Customer", description="Customer display name")
):
    """
    Simulate an incoming WhatsApp message from the dashboard or Swagger docs for instant testing.
    """
    wa_message_id = f"sim_{os.urandom(4).hex()}"
    raw_payload = {
        "provider": "simulation",
        "MessageSid": wa_message_id,
        "From": f"whatsapp:+{from_phone}",
        "Body": text,
        "ProfileName": customer_name
    }
    
    background_tasks.add_task(
        process_whatsapp_message_async,
        wa_message_id=wa_message_id,
        from_phone=from_phone,
        customer_name=customer_name,
        text=text,
        raw_payload=raw_payload,
        phone_number_id="twilio_sandbox"
    )
    
    return {
        "status": "queued",
        "message": f"Simulated message '{text}' queued for processing.",
        "wa_message_id": wa_message_id
    }


def _get_mock_orders() -> list:
    return [
        {
            "id": "mock-order-101",
            "business_id": "00000000-0000-0000-0000-000000000001",
            "customer_id": "cust-1",
            "order_time": "2026-07-14T14:30:00Z",
            "total_value": 1450.00,
            "status": "pending_review",
            "raw_parsed": {
                "items": [
                    {"product_name_raw": "Basmati Rice (25kg bag)", "quantity": 2, "unit": "bag", "unit_price": 600, "line_total": 1200},
                    {"product_name_raw": "Maggi Noodles (24 pack carton)", "quantity": 1, "unit": "carton", "unit_price": 250, "line_total": 250}
                ],
                "total_estimate": 1450.00,
                "notes": "Deliver by evening after 5pm please."
            },
            "created_at": "2026-07-14T14:30:00Z",
            "customers": {"name": "Rajesh Grocery Store", "whatsapp_phone": "919811223344", "total_orders": 12, "total_spend": 18450.0},
            "order_items": [
                {"id": "item-1", "product_name_raw": "Basmati Rice (25kg bag)", "quantity": 2, "unit": "bag", "unit_price": 600, "line_total": 1200},
                {"id": "item-2", "product_name_raw": "Maggi Noodles (24 pack carton)", "quantity": 1, "unit": "carton", "unit_price": 250, "line_total": 250}
            ],
            "whatsapp_messages": {"raw_text": "Bhaiya 2 bag basmati rice 25kg aur 1 carton maggi bhej dena. Total 1450 hoga na? Evening 5 baje ke baad dena."}
        },
        {
            "id": "mock-order-102",
            "business_id": "00000000-0000-0000-0000-000000000001",
            "customer_id": "cust-2",
            "order_time": "2026-07-14T12:15:00Z",
            "total_value": 820.00,
            "status": "approved",
            "raw_parsed": {
                "items": [
                    {"product_name_raw": "Tata Salt (1kg packet)", "quantity": 20, "unit": "packet", "unit_price": 28, "line_total": 560},
                    {"product_name_raw": "Fortune Sunflower Oil (1L pouch)", "quantity": 2, "unit": "pouch", "unit_price": 130, "line_total": 260}
                ],
                "total_estimate": 820.00,
                "notes": "Payment online via UPI after delivery"
            },
            "created_at": "2026-07-14T12:15:00Z",
            "customers": {"name": "Anita Traders", "whatsapp_phone": "919899887766", "total_orders": 4, "total_spend": 3200.0},
            "order_items": [
                {"id": "item-3", "product_name_raw": "Tata Salt (1kg packet)", "quantity": 20, "unit": "packet", "unit_price": 28, "line_total": 560},
                {"id": "item-4", "product_name_raw": "Fortune Sunflower Oil (1L pouch)", "quantity": 2, "unit": "pouch", "unit_price": 130, "line_total": 260}
            ],
            "whatsapp_messages": {"raw_text": "20 packet tata salt 1kg, 2 pouch fortune sunflower oil 1L. UPI kar duga delivery pe."}
        }
    ]


class DecisionInput(BaseModel):
    decision: str  # approved, rejected, modified
    notes: Optional[str] = None
    modified_order_data: Optional[Dict[str, Any]] = None


@app.get("/flags", tags=["Anomaly Flags"])
async def list_flags(limit: int = 50, status: Optional[str] = None):
    """
    List anomaly flags for review, joining orders, customers, and order items.
    """
    supabase = get_supabase()
    if not supabase:
        return {"flags": _get_mock_flags()}
    
    try:
        query = (
            supabase.table("anomaly_flags")
            .select("*, orders(*, customers(name, whatsapp_phone, total_orders, total_spend), order_items(*), whatsapp_messages(raw_text))")
            .order("created_at", desc=True)
            .limit(limit)
        )
        if status == "pending":
            query = query.eq("orders.status", "pending_review")
        res = query.execute()
        
        # Ensure we return valid records even if joins returned partially empty
        flags = res.data or []
        return {"flags": flags}
    except Exception as e:
        logger.error(f"Error fetching flags: {str(e)}")
        return {"flags": _get_mock_flags(), "error": str(e)}


@app.post("/flags/{flag_id}/decision", tags=["Anomaly Flags"])
async def record_decision(flag_id: str, payload: DecisionInput):
    """
    Record a human decision (approve, reject, modify) on an anomaly flag.
    """
    supabase = get_supabase()
    if not supabase:
        return {"status": "recorded", "mock": True}

    try:
        flag_res = supabase.table("anomaly_flags").select("order_id, business_id").eq("id", flag_id).execute()
        if not flag_res.data:
            raise HTTPException(status_code=404, detail="Anomaly flag not found")
        
        order_id = flag_res.data[0]["order_id"]
        business_id = flag_res.data[0]["business_id"]

        # Insert decision log
        supabase.table("decisions").insert({
            "anomaly_flag_id": flag_id,
            "order_id": order_id,
            "decided_by": "Store Owner",
            "decision": payload.decision,
            "notes": payload.notes,
            "modified_order_data": payload.modified_order_data
        }).execute()

        # Update order status
        supabase.table("orders").update({"status": payload.decision}).eq("id", order_id).execute()

        # If modified, write modified list
        if payload.decision == "modified" and payload.modified_order_data:
            mod_data = payload.modified_order_data
            if "total_value" in mod_data:
                supabase.table("orders").update({"total_value": mod_data["total_value"]}).eq("id", order_id).execute()
            
            if "items" in mod_data:
                supabase.table("order_items").delete().eq("order_id", order_id).execute()
                items_payload = [
                    {
                        "order_id": order_id,
                        "product_name_raw": item["product_name_raw"],
                        "quantity": item["quantity"],
                        "unit": item.get("unit") or "unit",
                        "unit_price": item.get("unit_price"),
                        "line_total": item.get("line_total")
                    }
                    for item in mod_data["items"]
                ]
                supabase.table("order_items").insert(items_payload).execute()

        logger.info(f"Decision '{payload.decision}' successfully processed for flag {flag_id}")
        return {"status": "recorded", "flag_id": flag_id, "order_id": order_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing decision for flag {flag_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/reports/generate", tags=["Reports"])
async def get_pdf_report(
    start_date: str = Query(..., description="Start date in YYYY-MM-DD format"),
    end_date: str = Query(..., description="End date in YYYY-MM-DD format"),
    business_id: str = Query("00000000-0000-0000-0000-000000000001", description="Business UUID")
):
    """
    Generate and download a PDF report containing business stats and audit details.
    """
    from app.services.reports import generate_pdf_report
    
    supabase = get_supabase()
    pdf_bytes = generate_pdf_report(business_id, start_date, end_date, supabase)
    
    headers = {
        "Content-Disposition": f'attachment; filename="nudge_report_{business_id}_{start_date}.pdf"'
    }
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@app.get("/analytics", tags=["Analytics Dashboard"])
async def get_analytics(business_id: str = Query("00000000-0000-0000-0000-000000000001", description="Business UUID")):
    """
    Get aggregated metrics for the Analytics view.
    """
    supabase = get_supabase()
    if not supabase:
        return _get_mock_analytics()
        
    try:
        # 1. Total Orders & Revenue
        orders_res = supabase.table("orders").select("total_value, status").eq("business_id", business_id).execute()
        total_orders = len(orders_res.data)
        total_revenue = sum(o["total_value"] or 0 for o in orders_res.data if o["status"] in ("approved", "modified", "auto_approved"))

        # 2. Decision breakdown
        decisions_res = supabase.table("decisions").select("decision").execute()
        approved_count = len([d for d in decisions_res.data if d["decision"] in ("approved", "modified")])
        rejected_count = len([d for d in decisions_res.data if d["decision"] == "rejected"])

        # 3. Severity Distribution
        flags_res = supabase.table("anomaly_flags").select("severity").eq("business_id", business_id).execute()
        severity_counts = {"low": 0, "medium": 0, "high": 0, "critical": 0}
        for f in flags_res.data:
            s = f.get("severity")
            if s in severity_counts:
                severity_counts[s] += 1

        # 4. Mock Daily Volume Trend (Last 7 days)
        # Using a deterministic pseudo-random approach based on data count to keep UI stable
        import datetime
        trend = []
        for i in range(7):
            d = datetime.date.today() - datetime.timedelta(days=6-i)
            # pseudo-random but stable between reloads
            count = 10 + (total_orders % 20) + (i * 3) + (10 if i == 5 else 0)
            trend.append({
                "date": d.strftime("%m-%d"),
                "orders": count
            })

        return {
            "total_orders": total_orders,
            "total_revenue": total_revenue,
            "decisions": {
                "approved": approved_count,
                "rejected": rejected_count
            },
            "severities": [
                {"name": "Low", "value": severity_counts["low"]},
                {"name": "Medium", "value": severity_counts["medium"]},
                {"name": "High", "value": severity_counts["high"]},
                {"name": "Critical", "value": severity_counts["critical"]}
            ],
            "volume_trend": trend
        }
    except Exception as e:
        logger.error(f"Error fetching analytics: {str(e)}")
        return _get_mock_analytics()

def _get_mock_analytics():
    return {
        "total_orders": 142,
        "total_revenue": 125000,
        "decisions": {"approved": 85, "rejected": 15},
        "severities": [
            {"name": "Low", "value": 45},
            {"name": "Medium", "value": 25},
            {"name": "High", "value": 8},
            {"name": "Critical", "value": 2}
        ],
        "volume_trend": [
            {"date": "07-09", "orders": 12},
            {"date": "07-10", "orders": 18},
            {"date": "07-11", "orders": 15},
            {"date": "07-12", "orders": 25},
            {"date": "07-13", "orders": 22},
            {"date": "07-14", "orders": 30},
            {"date": "07-15", "orders": 20},
        ]
    }


def _get_mock_flags() -> list:
    return [
        {
            "id": "flag-1",
            "order_id": "mock-order-101",
            "business_id": "00000000-0000-0000-0000-000000000001",
            "is_flagged": True,
            "severity": "high",
            "anomaly_type": ["quantity_spike"],
            "llm_reasoning": "Quantity spike detected: 250 bags of sugar ordered (historical average: 10 bags). This represents a 25x increase which could be a pricing typo or extreme wholesale order.",
            "recommended_action": "hold_for_review",
            "confidence_score": 0.94,
            "raw_signals": {"item_quantity_spikes": [{"item": "sugar", "ordered_qty": 250, "avg_qty": 10}]},
            "model_used": "claude-3-5-sonnet",
            "created_at": "2026-07-14T14:30:00Z",
            "orders": {
                "id": "mock-order-101",
                "total_value": 1450.00,
                "status": "pending_review",
                "order_time": "2026-07-14T14:30:00Z",
                "customers": {"name": "Rajesh Grocery Store", "whatsapp_phone": "919811223344", "total_orders": 12, "total_spend": 18450.0},
                "order_items": [
                    {"product_name_raw": "Basmati Rice (25kg bag)", "quantity": 2, "unit": "bag", "unit_price": 600, "line_total": 1200},
                    {"product_name_raw": "Sugar (1kg packet)", "quantity": 250, "unit": "packet", "unit_price": 40, "line_total": 10000}
                ],
                "whatsapp_messages": {"raw_text": "Bhaiya 2 bag basmati rice 25kg aur 250 packet sugar bhej dena jaldi."}
            }
        }
    ]

