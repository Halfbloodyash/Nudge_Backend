import os
import json
import logging
from dotenv import load_dotenv
load_dotenv()
import statistics
from datetime import datetime
from collections import defaultdict
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from typing_extensions import Literal

logger = logging.getLogger("sentrix-anomaly")

class AnomalyResult(BaseModel):
    is_flagged: bool = Field(..., description="Whether this order should be flagged as anomalous/suspicious")
    severity: Literal["low", "medium", "high", "critical"] = Field(..., description="Severity level of the anomaly")
    anomaly_type: List[str] = Field(..., description="Types of anomalies detected (e.g. ['quantity_spike', 'unusual_time', 'new_item', 'value_spike'])")
    llm_reasoning: str = Field(..., description="Human-readable explanation of why it was flagged or cleared, meant for the business owner")
    recommended_action: Literal["approve", "hold_for_review", "contact_customer", "check_inventory", "escalate", "reject"] = Field(..., description="Recommended action for the business owner")
    confidence_score: float = Field(..., description="Confidence score between 0.000 and 1.000")


def compute_customer_profile(customer_id: str, supabase: Any) -> Dict[str, Any]:
    """
    Computes a customer's rolling behavioral profile from their order history
    and updates the customer_profiles table in Supabase.
    """
    logger.info(f"Computing customer profile for customer: {customer_id}")
    
    try:
        # Fetch up to last 50 orders (excluding rejected orders)
        res = (
            supabase.table("orders")
            .select("total_value, order_time, id")
            .eq("customer_id", customer_id)
            .neq("status", "rejected")
            .order("order_time", desc=True)
            .limit(50)
            .execute()
        )
        orders = res.data or []
        
        if len(orders) < 3:
            logger.info(f"Customer {customer_id} has insufficient history ({len(orders)} orders). Skipping profile computation.")
            profile = {
                "insufficient_history": True,
                "order_count": len(orders)
            }
            # Upsert simple profile
            supabase.table("customer_profiles").upsert({
                "customer_id": customer_id,
                "common_items": [],
                "last_recomputed_at": datetime.utcnow().isoformat()
            }).execute()
            return profile

        # Extract values
        values = [float(o["total_value"] or 0.0) for o in orders]
        avg_value = statistics.mean(values)
        stddev_value = statistics.stdev(values) if len(values) > 1 else 0.0

        # Gaps/Frequency in days between consecutive orders
        # Sort in ascending order
        times = sorted([datetime.fromisoformat(o["order_time"].replace("Z", "+00:00")) for o in orders])
        gaps = [(times[i+1] - times[i]).total_seconds() / 86400.0 for i in range(len(times)-1)]
        avg_gap = statistics.mean(gaps) if gaps else 0.0
        stddev_gap = statistics.stdev(gaps) if len(gaps) > 1 else 0.0

        # Typical order hours (UTC)
        hours = [t.hour for t in times]
        typical_start = min(hours)
        typical_end = max(hours)

        # Common Items
        order_ids = [o["id"] for o in orders]
        items_res = (
            supabase.table("order_items")
            .select("product_name_raw, quantity")
            .in_("order_id", order_ids)
            .execute()
        )
        item_rows = items_res.data or []
        
        item_stats = defaultdict(list)
        for row in item_rows:
            item_stats[row["product_name_raw"]].append(float(row["quantity"] or 0.0))

        common_items = [
            {
                "product_name": name,
                "avg_qty": round(statistics.mean(qtys), 2),
                "frequency": len(qtys)
            }
            for name, qtys in item_stats.items()
        ]

        profile_data = {
            "customer_id": customer_id,
            "avg_order_value": round(avg_value, 2),
            "stddev_order_value": round(stddev_value, 2),
            "avg_order_frequency_days": round(avg_gap, 2),
            "stddev_order_frequency_days": round(stddev_gap, 2),
            "typical_order_hour_start": typical_start,
            "typical_order_hour_end": typical_end,
            "common_items": common_items,
            "last_recomputed_at": datetime.utcnow().isoformat()
        }

        supabase.table("customer_profiles").upsert(profile_data).execute()
        
        # Return dict with helper metadata
        profile_data["insufficient_history"] = False
        profile_data["order_count"] = len(orders)
        return profile_data

    except Exception as e:
        logger.error(f"Error computing customer profile: {str(e)}", exc_info=True)
        return {"insufficient_history": True, "order_count": 0, "error": str(e)}


def detect_signals(order: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    """
    Rule-assisted signal detection comparing order details with rolling customer profile.
    """
    signals = {}

    if profile.get("insufficient_history"):
        signals["new_customer"] = True
        return signals

    # Get values safely
    order_val = float(order.get("total_value") or 0.0)
    avg_val = float(profile.get("avg_order_value") or 0.0)
    stddev_val = float(profile.get("stddev_order_value") or 0.0)
    avg_gap = profile.get("avg_order_frequency_days")

    # 1. Z-Score Order Value Spike
    if stddev_val > 5.0:  # Avoid division by tiny stddev
        z = (order_val - avg_val) / stddev_val
        signals["value_zscore"] = round(z, 2)
        signals["value_spike"] = z > 2.5
    else:
        signals["value_zscore"] = 0.0
        signals["value_spike"] = order_val > (avg_val * 3.0) and order_val > 500.0

    # 2. Frequency Anomaly (Ordered much sooner than normal)
    # Note: Requires customer_last_order_at if available
    last_order_at = order.get("customer_last_order_at")
    if last_order_at and avg_gap and avg_gap > 0:
        try:
            curr_time = datetime.fromisoformat(order["order_time"].replace("Z", "+00:00"))
            prev_time = datetime.fromisoformat(last_order_at.replace("Z", "+00:00"))
            days_since = (curr_time - prev_time).total_seconds() / 86400.0
            signals["days_since_last_order"] = round(days_since, 2)
            signals["unusual_frequency"] = days_since < (avg_gap * 0.3)
        except Exception:
            signals["unusual_frequency"] = False
    else:
        signals["unusual_frequency"] = False

    # 3. Unusual Hour Check
    try:
        hour = datetime.fromisoformat(order["order_time"].replace("Z", "+00:00")).hour
        typ_start = profile.get("typical_order_hour_start", 0)
        typ_end = profile.get("typical_order_hour_end", 23)
        signals["order_hour"] = hour
        
        # If range spans across midnight
        if typ_start <= typ_end:
            signals["unusual_time"] = not (typ_start <= hour <= typ_end)
        else:
            signals["unusual_time"] = not (hour >= typ_start or hour <= typ_end)
    except Exception:
        signals["unusual_time"] = False

    # 4. New / Unusual Items
    order_items = order.get("items") or []
    profile_items = profile.get("common_items") or []
    
    known_items = {i["product_name"].lower().strip() for i in profile_items}
    ordered_items = {i.get("product_name_raw", "").lower().strip() for i in order_items}
    
    new_items = list(ordered_items - known_items)
    signals["new_items"] = new_items

    # 5. Quantity Spike per Item
    item_spikes = []
    common_qty_map = {i["product_name"].lower().strip(): i["avg_qty"] for i in profile_items}
    
    for item in order_items:
        name = item.get("product_name_raw", "").lower().strip()
        qty = float(item.get("quantity") or 0.0)
        avg_qty = common_qty_map.get(name)
        if avg_qty and avg_qty > 0 and qty > (avg_qty * 3.0):
            item_spikes.append({
                "item": item.get("product_name_raw"),
                "ordered_qty": qty,
                "avg_qty": avg_qty
            })
            
    signals["item_quantity_spikes"] = item_spikes
    return signals


def check_inventory_risk(order: Dict[str, Any], supabase: Any) -> Dict[str, Any]:
    """
    Checks if order items exceed manually entered inventory levels or drop below reorder thresholds.
    """
    business_id = order.get("business_id")
    risk_items = []
    
    if not supabase:
        return {"inventory_risks": []}
        
    for item in order.get("items") or []:
        name = item.get("product_name_raw", "")
        qty = float(item.get("quantity") or 0.0)
        
        try:
            prod_res = (
                supabase.table("products")
                .select("id, name, current_stock, reorder_threshold")
                .eq("business_id", business_id)
                .ilike("name", f"%{name}%")
                .execute()
            )
            
            if prod_res.data:
                product = prod_res.data[0]
                curr_stock = product.get("current_stock")
                reorder_thresh = product.get("reorder_threshold")
                
                if curr_stock is not None:
                    curr_stock = float(curr_stock)
                    projected = curr_stock - qty
                    
                    if projected < 0:
                        risk_items.append({
                            "product": product["name"],
                            "requested": qty,
                            "available": curr_stock,
                            "shortfall": abs(projected),
                            "type": "out_of_stock"
                        })
                    elif reorder_thresh is not None and projected < float(reorder_thresh):
                        risk_items.append({
                            "product": product["name"],
                            "requested": qty,
                            "available": curr_stock,
                            "shortfall": 0.0,
                            "note": f"Drops below threshold ({reorder_thresh})",
                            "type": "reorder_warning"
                        })
        except Exception as e:
            logger.error(f"Error checking inventory for product {name}: {str(e)}")
            
    return {"inventory_risks": risk_items}


async def call_llm_for_reasoning(order: Dict[str, Any], profile: Dict[str, Any], signals: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calls Groq AI with tool use to reason over signals and classify the anomaly.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key or api_key == "your-groq-api-key":
        logger.warning("No GROQ_API_KEY found. Mocking anomaly detection decision.")
        # Basic heuristic anomaly detection in mock mode
        is_flagged = signals.get("value_spike", False) or len(signals.get("item_quantity_spikes", [])) > 0 or signals.get("unusual_time", False)
        severity = "medium"
        anomaly_types = []
        reason_list = []
        if signals.get("value_spike"):
            severity = "high"
            anomaly_types.append("value_spike")
            reason_list.append(f"Order total value is significantly higher than average (Z-score: {signals.get('value_zscore')})")
        if signals.get("item_quantity_spikes"):
            anomaly_types.append("quantity_spike")
            for sp in signals["item_quantity_spikes"]:
                reason_list.append(f"Spike in {sp['item']}: ordered {sp['ordered_qty']} (avg: {sp['avg_qty']})")
        if signals.get("unusual_time"):
            anomaly_types.append("unusual_time")
            reason_list.append("Order received at an unusual time outside typical hours")
            
        reasoning = "; ".join(reason_list) if reason_list else "Order patterns correspond to historical client behavior."
        
        return AnomalyResult(
            is_flagged=is_flagged,
            severity=severity if is_flagged else "low",
            anomaly_type=anomaly_types,
            llm_reasoning=f"[MOCK DETECTOR] {reasoning}",
            recommended_action="hold_for_review" if is_flagged else "approve",
            confidence_score=0.90 if is_flagged else 1.00
        ).model_dump()

    try:
        import groq
        client = groq.AsyncGroq(api_key=api_key)
        
        tools = [{
            "type": "function",
            "function": {
                "name": "classify_anomaly",
                "description": "Classify whether this order is anomalous based on profile and signals, and explain why.",
                "parameters": AnomalyResult.model_json_schema()
            }
        }]

        prompt = f"""
You are reviewing a newly placed customer order for potential anomalies before approval. Use your judgment to determine if this requires flag review.

CUSTOMER PROFILE:
{json.dumps(profile, default=str, indent=2)}

CURRENT ORDER:
{json.dumps(order, default=str, indent=2)}

DETECTED STATISTICAL SIGNALS:
{json.dumps(signals, default=str, indent=2)}

DIRECTIONS:
1. Review the order, historical average quantities, values, and times.
2. Determine if the detected signals represent a genuine anomaly (e.g. suspicious bulk order, typo quantity like 200 instead of 20, unusual late night run, or completely foreign catalog items).
3. If it looks like a normal restocking patterns or minor variation, DO NOT flag it (is_flagged = false). Use logical shop-owner reasoning.
4. Output your decision through the `classify_anomaly` tool. Explain your reasoning in simple language.
"""

        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert wholesale distributor risk audit agent. Keep explanations friendly but concise."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "classify_anomaly"}}
        )

        tool_calls = response.choices[0].message.tool_calls
        if tool_calls:
            arguments = json.loads(tool_calls[0].function.arguments)
            return AnomalyResult.model_validate(arguments).model_dump()
                
        return AnomalyResult(
            is_flagged=False,
            severity="low",
            anomaly_type=[],
            llm_reasoning="Failed to extract structured response block from LLM.",
            recommended_action="approve",
            confidence_score=0.5
        ).model_dump()
        
    except Exception as e:
        logger.error(f"Error calling Groq for anomaly reasoning: {str(e)}", exc_info=True)
        return AnomalyResult(
            is_flagged=False,
            severity="low",
            anomaly_type=[],
            llm_reasoning=f"Error executing LLM reasoning: {str(e)}",
            recommended_action="approve",
            confidence_score=0.5
        ).model_dump()
