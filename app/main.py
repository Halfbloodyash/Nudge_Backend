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
    title="Sentrix Phase 1 - Order Processing API (Twilio + Meta)",
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

VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "sentrix_secret_token_123")


@app.get("/", tags=["Health"])
async def root():
    return {
        "service": "Sentrix Phase 1 Backend API",
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


@app.get("/customers", tags=["Customer Intelligence"])
async def list_customers(limit: int = 50, search: Optional[str] = None):
    """
    List customers enriched with their behavioral intelligence profiles.
    """
    supabase = get_supabase()
    if not supabase:
        return {"customers": _get_mock_customers()}

    try:
        query = (
            supabase.table("customers")
            .select("*, customer_profiles(*)")
            .order("total_orders", desc=True)
            .limit(limit)
        )
        if search:
            query = query.ilike("name", f"%{search}%")
            
        res = query.execute()
        return {"customers": res.data or []}
    except Exception as e:
        logger.error(f"Error fetching customers: {str(e)}")
        return {"customers": _get_mock_customers(), "error": str(e)}


@app.get("/customers/{customer_id}/profile", tags=["Customer Intelligence"])
async def get_customer_profile_detail(customer_id: str):
    """
    Get detailed customer profile, behavioral habits, risk score, and recent orders/flags.
    """
    supabase = get_supabase()
    if not supabase:
        for c in _get_mock_customers():
            if c["id"] == customer_id:
                return _enrich_mock_profile(c)
        return _enrich_mock_profile(_get_mock_customers()[0])

    try:
        # 1. Fetch customer + profile
        c_res = supabase.table("customers").select("*, customer_profiles(*)").eq("id", customer_id).execute()
        if not c_res.data:
            raise HTTPException(status_code=404, detail="Customer not found")
        customer = c_res.data[0]
        
        # 2. Fetch recent orders
        orders_res = (
            supabase.table("orders")
            .select("id, order_time, total_value, status, raw_parsed")
            .eq("customer_id", customer_id)
            .order("order_time", desc=True)
            .limit(20)
            .execute()
        )
        recent_orders = orders_res.data or []

        # 3. Fetch recent anomaly flags triggered by this customer's orders
        order_ids = [o["id"] for o in recent_orders]
        flags_data = []
        if order_ids:
            flags_res = (
                supabase.table("anomaly_flags")
                .select("id, order_id, is_flagged, severity, anomaly_type, llm_reasoning, recommended_action, created_at")
                .in_("order_id", order_ids)
                .order("created_at", desc=True)
                .execute()
            )
            flags_data = flags_res.data or []

        profile = customer.get("customer_profiles") or {}
        if isinstance(profile, list) and len(profile) > 0:
            profile = profile[0]
        elif isinstance(profile, list):
            profile = {}

        # Compute Favourite Product from common_items
        common_items = profile.get("common_items") or []
        favourite_product = None
        if common_items:
            sorted_items = sorted(common_items, key=lambda x: x.get("frequency", 0), reverse=True)
            favourite_product = sorted_items[0]

        # Calculate Risk Score (0 - 100)
        risk_score = 12.0  # base low risk
        if customer.get("is_flagged_risk"):
            risk_score += 40.0
        for f in flags_data:
            if f.get("severity") == "critical":
                risk_score += 35.0
            elif f.get("severity") == "high":
                risk_score += 25.0
            elif f.get("severity") == "medium":
                risk_score += 10.0
        risk_score = min(round(risk_score, 1), 100.0)

        return {
            "customer": customer,
            "profile": profile,
            "favourite_product": favourite_product,
            "risk_score": risk_score,
            "recent_orders": recent_orders,
            "recent_flags": flags_data
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching customer profile detail: {str(e)}")
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


@app.post("/orders/{order_id}/decision", tags=["Orders"])
async def record_order_decision(order_id: str, payload: DecisionInput):
    """
    Directly record a human decision (approve, reject, modify) on an order by its order_id.
    This ensures Human-in-the-Loop review works for all pending orders even if no anomaly flag ID is selected.
    """
    supabase = get_supabase()
    if not supabase:
        return {"status": "recorded", "mock": True, "order_id": order_id, "decision": payload.decision}

    try:
        # Update order status in orders table
        supabase.table("orders").update({"status": payload.decision}).eq("id", order_id).execute()

        # Check if there is an anomaly flag for this order to also record in decisions table
        flag_res = supabase.table("anomaly_flags").select("id").eq("order_id", order_id).execute()
        if flag_res.data and len(flag_res.data) > 0:
            flag_id = flag_res.data[0]["id"]
            supabase.table("decisions").insert({
                "anomaly_flag_id": flag_id,
                "order_id": order_id,
                "decided_by": "Store Owner (Human-In-The-Loop)",
                "decision": payload.decision,
                "notes": payload.notes or "Direct Order Review"
            }).execute()

        logger.info(f"Direct order decision '{payload.decision}' processed for order {order_id}")
        return {"status": "recorded", "order_id": order_id, "decision": payload.decision}
    except Exception as e:
        logger.error(f"Error processing direct order decision for {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


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
    status: Optional[str] = Query(None, description="Optional order status filter"),
    severity: Optional[str] = Query(None, description="Optional anomaly severity filter"),
    business_id: str = Query("00000000-0000-0000-0000-000000000001", description="Business UUID")
):
    """
    Generate and download a PDF report containing business stats and audit details.
    """
    from app.services.reports import generate_pdf_report
    
    supabase = get_supabase()
    pdf_bytes = generate_pdf_report(business_id, start_date, end_date, supabase, status, severity)
    
    headers = {
        "Content-Disposition": f'attachment; filename="sentrix_report_{business_id}_{start_date}.pdf"'
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
        # 1. Total Orders & Data
        orders_res = supabase.table("orders").select("*, customers(name)").eq("business_id", business_id).execute()
        orders = orders_res.data or []
        total_orders = len(orders)

        # 2. Order Distribution
        from collections import defaultdict
        dist = defaultdict(int)
        orders_today_count = 0
        import datetime
        today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        
        for o in orders:
            cust = o.get("customers") or {}
            name = cust.get("name", "Unknown")
            dist[name] += 1
            if o.get("order_time", "").startswith(today_str):
                orders_today_count += 1
                
        # Sort distribution
        sorted_dist = sorted([{"name": k, "count": v} for k, v in dist.items()], key=lambda x: x["count"], reverse=True)
        
        # Add percentage
        for item in sorted_dist:
            item["percentage"] = round((item["count"] / total_orders * 100), 1) if total_orders > 0 else 0
            
        most_active = sorted_dist[0] if sorted_dist else {"name": "-", "count": 0}

        return {
            "total_orders": total_orders,
            "order_distribution": sorted_dist,
            "most_active_customer": most_active,
            "orders_today": orders_today_count,
            "highest_deviation": "-"
        }
    except Exception as e:
        logger.error(f"Error fetching analytics: {str(e)}")
        return _get_mock_analytics()

def _get_mock_analytics():
    return {
        "total_orders": 32,
        "order_distribution": [
            {"name": "Rahul", "count": 7, "percentage": 21.9},
            {"name": "Meera", "count": 5, "percentage": 15.6},
            {"name": "Priya", "count": 5, "percentage": 15.6},
            {"name": "Arjun", "count": 4, "percentage": 12.5},
            {"name": "Sanjay", "count": 4, "percentage": 12.5},
            {"name": "Pujith", "count": 2, "percentage": 6.3},
            {"name": "Ram", "count": 2, "percentage": 6.3},
            {"name": "Divya", "count": 2, "percentage": 6.3},
            {"name": "Rajesh", "count": 1, "percentage": 3.1}
        ],
        "most_active_customer": {"name": "Rahul", "count": 7},
        "orders_today": 0,
        "highest_deviation": "-"
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


def _get_mock_customers() -> list:
    return [
        {
            "id": "cust-101",
            "business_id": "00000000-0000-0000-0000-000000000001",
            "name": "Rajesh Grocery Store",
            "whatsapp_phone": "+91 98112 23344",
            "total_orders": 120,
            "total_spend": 60000.00,
            "first_order_at": "2026-01-10T09:15:00Z",
            "last_order_at": "2026-07-15T09:30:00Z",
            "is_flagged_risk": True,
            "customer_profiles": {
                "customer_id": "cust-101",
                "avg_order_value": 500.00,
                "stddev_order_value": 45.20,
                "avg_order_frequency_days": 1.20,
                "stddev_order_frequency_days": 0.35,
                "typical_order_hour_start": 9,
                "typical_order_hour_end": 11,
                "common_items": [
                    {"product_name": "Milk", "avg_qty": 4.5, "frequency": 118},
                    {"product_name": "Bread", "avg_qty": 2.0, "frequency": 45},
                    {"product_name": "Sugar", "avg_qty": 1.0, "frequency": 12}
                ],
                "last_recomputed_at": "2026-07-15T08:00:00Z"
            }
        },
        {
            "id": "cust-102",
            "business_id": "00000000-0000-0000-0000-000000000001",
            "name": "Rahul Provision Store",
            "whatsapp_phone": "+91 98223 34455",
            "total_orders": 35,
            "total_spend": 28400.00,
            "first_order_at": "2026-03-01T14:20:00Z",
            "last_order_at": "2026-07-14T16:45:00Z",
            "is_flagged_risk": False,
            "customer_profiles": {
                "customer_id": "cust-102",
                "avg_order_value": 810.00,
                "stddev_order_value": 65.00,
                "avg_order_frequency_days": 3.50,
                "stddev_order_frequency_days": 0.80,
                "typical_order_hour_start": 14,
                "typical_order_hour_end": 18,
                "common_items": [
                    {"product_name": "Basmati Rice", "avg_qty": 1.0, "frequency": 32},
                    {"product_name": "Toor Dal", "avg_qty": 2.0, "frequency": 28}
                ],
                "last_recomputed_at": "2026-07-14T17:00:00Z"
            }
        },
        {
            "id": "cust-103",
            "business_id": "00000000-0000-0000-0000-000000000001",
            "name": "Priya Daily Mart",
            "whatsapp_phone": "+91 98334 45566",
            "total_orders": 14,
            "total_spend": 7200.00,
            "first_order_at": "2026-05-12T10:00:00Z",
            "last_order_at": "2026-07-13T11:10:00Z",
            "is_flagged_risk": False,
            "customer_profiles": {
                "customer_id": "cust-103",
                "avg_order_value": 514.00,
                "stddev_order_value": 30.00,
                "avg_order_frequency_days": 5.00,
                "stddev_order_frequency_days": 1.10,
                "typical_order_hour_start": 10,
                "typical_order_hour_end": 12,
                "common_items": [
                    {"product_name": "Amul Butter", "avg_qty": 2.0, "frequency": 14},
                    {"product_name": "Cheese Slice", "avg_qty": 1.0, "frequency": 10}
                ],
                "last_recomputed_at": "2026-07-13T12:00:00Z"
            }
        }
    ]


def _enrich_mock_profile(customer: dict) -> dict:
    profile = customer.get("customer_profiles") or {}
    common_items = profile.get("common_items") or []
    favourite_product = None
    if common_items:
        sorted_items = sorted(common_items, key=lambda x: x.get("frequency", 0), reverse=True)
        favourite_product = sorted_items[0]

    risk_score = 12.0
    if customer.get("is_flagged_risk"):
        risk_score = 88.5

    # Generate mock recent orders & flags matching the ₹500 vs ₹45,000 concept!
    recent_orders = []
    recent_flags = []
    if customer["id"] == "cust-101":
        recent_orders = [
            {"id": "mock-order-101", "order_time": "2026-07-15T09:30:00Z", "total_value": 45000.00, "status": "pending_review", "raw_parsed": {"items": [{"product_name_raw": "Sugar (1kg packet)", "quantity": 250, "unit": "packet", "unit_price": 40, "line_total": 10000}, {"product_name_raw": "Basmati Rice (25kg bag)", "quantity": 58, "unit": "bag", "unit_price": 600, "line_total": 35000}]}},
            {"id": "mock-order-100", "order_time": "2026-07-14T09:15:00Z", "total_value": 510.00, "status": "approved", "raw_parsed": {"items": [{"product_name_raw": "Milk", "quantity": 5, "unit": "packet", "unit_price": 60, "line_total": 300}, {"product_name_raw": "Bread", "quantity": 3, "unit": "packet", "unit_price": 70, "line_total": 210}]}},
            {"id": "mock-order-099", "order_time": "2026-07-13T10:05:00Z", "total_value": 480.00, "status": "approved", "raw_parsed": {"items": [{"product_name_raw": "Milk", "quantity": 4, "unit": "packet", "unit_price": 60, "line_total": 240}, {"product_name_raw": "Sugar", "quantity": 2, "unit": "packet", "unit_price": 120, "line_total": 240}]}},
            {"id": "mock-order-098", "order_time": "2026-07-12T09:40:00Z", "total_value": 520.00, "status": "approved", "raw_parsed": {"items": [{"product_name_raw": "Milk", "quantity": 5, "unit": "packet", "unit_price": 60, "line_total": 300}]}}
        ]
        recent_flags = [
            {
                "id": "flag-101",
                "order_id": "mock-order-101",
                "is_flagged": True,
                "severity": "critical",
                "anomaly_type": ["value_spike", "quantity_spike"],
                "llm_reasoning": "Extreme value anomaly detected: Today's order total is ₹45,000 compared to the customer's historical average order size of ₹500 (Z-score: 984.5). Additionally, quantity spike of 250 packets of sugar observed.",
                "recommended_action": "hold_for_review",
                "created_at": "2026-07-15T09:30:05Z"
            }
        ]
    elif customer["id"] == "cust-102":
        recent_orders = [
            {"id": "mock-order-102", "order_time": "2026-07-14T16:45:00Z", "total_value": 810.00, "status": "approved", "raw_parsed": {"items": [{"product_name_raw": "Basmati Rice", "quantity": 1, "unit": "bag", "unit_price": 810, "line_total": 810}]}}
        ]
    else:
        recent_orders = [
            {"id": "mock-order-103", "order_time": "2026-07-13T11:10:00Z", "total_value": 514.00, "status": "approved", "raw_parsed": {"items": [{"product_name_raw": "Amul Butter", "quantity": 2, "unit": "pack", "unit_price": 257, "line_total": 514}]}}
        ]

    return {
        "customer": customer,
        "profile": profile,
        "favourite_product": favourite_product,
        "risk_score": risk_score,
        "recent_orders": recent_orders,
        "recent_flags": recent_flags
    }


