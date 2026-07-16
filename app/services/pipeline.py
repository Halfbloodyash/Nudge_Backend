import os
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from supabase import create_client, Client
from dotenv import load_dotenv
load_dotenv()
from app.services.parser import extract_order_from_text
from app.schemas import ParsedOrderSchema

logger = logging.getLogger("sentrix-pipeline")

def get_supabase() -> Optional[Client]:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key or url == "https://your-project-ref.supabase.co":
        logger.warning("Supabase environment variables not configured properly.")
        return None
    try:
        return create_client(url, key)
    except Exception as e:
        logger.error(f"Failed to connect to Supabase: {str(e)}")
def _enrich_items_with_catalog_prices(items: list, business_id: str, supabase: Optional[Client]):
    if not supabase or not items:
        return
    try:
        prods_res = supabase.table("products").select("name, avg_price, unit").eq("business_id", business_id).execute()
        catalog = prods_res.data or []
        if not catalog:
            return
        
        for item in items:
            if not item.unit_price or item.unit_price <= 0:
                raw_clean = item.product_name_raw.lower().replace("-", " ").strip()
                raw_words = [w for w in raw_clean.split() if len(w) > 1 or w == "g"]
                
                best_match = None
                best_score = 0
                for p in catalog:
                    p_name_clean = p["name"].lower().replace("-", " ")
                    score = sum(1 for w in raw_words if w in p_name_clean)
                    if score > best_score:
                        best_score = score
                        best_match = p
                
                if best_match and best_score > 0 and best_match.get("avg_price"):
                    item.unit_price = float(best_match["avg_price"])
                    if not item.unit or item.unit == "unit":
                        item.unit = best_match.get("unit", "unit")
                    logger.info(f"[Catalog Enrichment] Matched '{item.product_name_raw}' to '{best_match['name']}' (@ ₹{item.unit_price})")
            
            # Ensure line_total is computed accurately
            if item.unit_price and item.unit_price > 0:
                item.line_total = float(item.quantity or 1.0) * float(item.unit_price)
    except Exception as e:
        logger.warning(f"Error enriching catalog prices: {e}")

async def process_whatsapp_message_async(
    wa_message_id: str,
    from_phone: str,
    customer_name: str,
    text: str,
    raw_payload: Dict[Any, Any],
    phone_number_id: str
):
    """
    Background worker that runs asynchronously after responding 200 OK to Meta.
    """
    logger.info(f"[LangGraph Node: parse] Starting ingestion for message {wa_message_id}")
    supabase = get_supabase()
    if not supabase:
        logger.error("Supabase client not available. Aborting message processing.")
        return

    # 1. Resolve or create Business ID
    business_id = "00000000-0000-0000-0000-000000000001" # Default fallback
    try:
        biz_res = supabase.table("businesses").select("id").eq("whatsapp_number", phone_number_id).execute()
        if biz_res.data:
            business_id = biz_res.data[0]["id"]
        else:
            supabase.table("businesses").upsert({
                "id": business_id,
                "name": "Sentrix Wholesale Hub",
                "whatsapp_number": phone_number_id
            }).execute()
    except Exception as e:
        logger.warning(f"Could not resolve business for {phone_number_id}: {str(e)}. Using fallback ID.")

    # 2. Resolve or create Customer ID
    customer_id = None
    try:
        cust_res = supabase.table("customers").select("*").eq("business_id", business_id).eq("whatsapp_phone", from_phone).execute()
        if cust_res.data:
            customer_id = cust_res.data[0]["id"]
            if customer_name and cust_res.data[0]["name"] != customer_name:
                supabase.table("customers").update({"name": customer_name}).eq("id", customer_id).execute()
        else:
            cust_insert = supabase.table("customers").insert({
                "business_id": business_id,
                "whatsapp_phone": from_phone,
                "name": customer_name or f"Partner ({from_phone[-4:]})",
                "total_orders": 0,
                "total_spend": 0.0
            }).execute()
            if cust_insert.data:
                customer_id = cust_insert.data[0]["id"]
    except Exception as e:
        logger.error(f"Error resolving customer: {str(e)}")

    # 3. Store raw message in whatsapp_messages table
    db_msg_id = None
    try:
        msg_insert = supabase.table("whatsapp_messages").insert({
            "business_id": business_id,
            "customer_id": customer_id,
            "wa_message_id": wa_message_id,
            "from_phone": from_phone,
            "raw_text": text,
            "raw_payload": raw_payload,
            "status": "pending"
        }).execute()
        if msg_insert.data:
            db_msg_id = msg_insert.data[0]["id"]
    except Exception as e:
        logger.error(f"Error inserting whatsapp_message: {str(e)}")

    if not customer_id or not db_msg_id:
        logger.error("Could not establish customer_id or db_msg_id. Aborting processing.")
        return

    # 4. Call LLM Parser
    try:
        parsed_order: ParsedOrderSchema = await extract_order_from_text(text)
        
        # Enrich missing line prices from Supabase product catalog
        if parsed_order.items:
            _enrich_items_with_catalog_prices(parsed_order.items, business_id, supabase)
        
        # Calculate total value estimate from items or top-level estimate
        total_val = sum((item.line_total or (item.quantity * item.unit_price if item.unit_price else 0)) for item in parsed_order.items) if parsed_order.items else parsed_order.total_estimate
        if not total_val or total_val <= 0:
            total_val = parsed_order.total_estimate or 0.0

        # 5. Insert structured order into orders table
        order_insert = supabase.table("orders").insert({
            "business_id": business_id,
            "customer_id": customer_id,
            "source_message_id": db_msg_id,
            "order_time": datetime.now(timezone.utc).isoformat(),
            "total_value": total_val or 0.0,
            "status": "pending_review",
            "raw_parsed": parsed_order.model_dump()
        }).execute()

        if order_insert.data and parsed_order.items:
            order_id = order_insert.data[0]["id"]
            items_payload = [
                {
                    "order_id": order_id,
                    "product_name_raw": item.product_name_raw,
                    "quantity": item.quantity,
                    "unit": item.unit or "unit",
                    "unit_price": item.unit_price,
                    "line_total": item.line_total or (item.quantity * item.unit_price if item.unit_price else None)
                }
                for item in parsed_order.items
            ]
            supabase.table("order_items").insert(items_payload).execute()

            # Retrieve previous order time for frequency signal comparisons
            customer_last_order_at = None
            try:
                last_order_res = (
                    supabase.table("orders")
                    .select("order_time")
                    .eq("customer_id", customer_id)
                    .neq("id", order_id)
                    .order("order_time", desc=True)
                    .limit(1)
                    .execute()
                )
                if last_order_res.data:
                    customer_last_order_at = last_order_res.data[0]["order_time"]
            except Exception as e:
                logger.error(f"Error fetching previous order timestamp: {str(e)}")

            # Construct order data representation for the agent
            order_data = {
                "id": order_id,
                "business_id": business_id,
                "customer_id": customer_id,
                "total_value": total_val or 0.0,
                "order_time": datetime.now(timezone.utc).isoformat(),
                "customer_last_order_at": customer_last_order_at,
                "items": [
                    {
                        "product_name_raw": item.product_name_raw,
                        "quantity": item.quantity,
                        "unit": item.unit
                    }
                    for item in parsed_order.items
                ]
            }

            # Trigger anomaly detection LangGraph agent asynchronously
            try:
                from app.services.agent import anomaly_agent
                logger.info(f"Triggering anomaly detection LangGraph workflow for order: {order_id}")
                await anomaly_agent.ainvoke({
                    "order": order_data,
                    "profile": {},
                    "signals": {},
                    "result": {},
                    "customer_id": customer_id,
                    "order_id": order_id,
                    "business_id": business_id
                })
            except Exception as e:
                logger.error(f"Failed to run anomaly detection LangGraph: {str(e)}", exc_info=True)

            # Update customer stats (total_orders, last_order_at, total_spend)
            try:
                cust_data = supabase.table("customers").select("total_orders, total_spend").eq("id", customer_id).execute()
                if cust_data.data:
                    curr_orders = cust_data.data[0].get("total_orders") or 0
                    curr_spend = float(cust_data.data[0].get("total_spend") or 0.0)
                    new_spend = curr_spend + float(total_val or 0.0)
                    
                    supabase.table("customers").update({
                        "total_orders": curr_orders + 1,
                        "total_spend": round(new_spend, 2),
                        "last_order_at": datetime.now(timezone.utc).isoformat()
                    }).eq("id", customer_id).execute()
            except Exception as e:
                logger.error(f"Error updating customer stats: {str(e)}")

        # 6. Mark whatsapp_messages as processed
        supabase.table("whatsapp_messages").update({
            "processed": True,
            "processing_error": None
        }).eq("id", db_msg_id).execute()

        logger.info(f"Successfully processed order from {from_phone} (WA ID: {wa_message_id})")

    except Exception as e:
        logger.error(f"Failed processing WA message {wa_message_id}: {str(e)}", exc_info=True)
        if db_msg_id:
            supabase.table("whatsapp_messages").update({
                "processed": True,
                "processing_error": str(e)
            }).eq("id", db_msg_id).execute()
