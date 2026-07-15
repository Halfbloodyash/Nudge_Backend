import logging
from typing import Dict, Any, TypedDict
from langgraph.graph import StateGraph, END
from app.services.anomaly import compute_customer_profile, detect_signals, call_llm_for_reasoning, check_inventory_risk
from app.services.pipeline import get_supabase

logger = logging.getLogger("sentrix-agent")

class AgentState(TypedDict):
    order: Dict[str, Any]
    profile: Dict[str, Any]
    signals: Dict[str, Any]
    result: Dict[str, Any]
    customer_id: str
    order_id: str
    business_id: str


def load_context_node(state: AgentState) -> AgentState:
    """
    Node 1: Compute customer profile and extract statistical signals (including inventory risks).
    """
    logger.info(f"[LangGraph Node: load_context] Starting context resolution for order {state.get('order_id')}")
    supabase = get_supabase()
    
    # 1. Compute profile
    profile = compute_customer_profile(state["customer_id"], supabase)
    state["profile"] = profile

    # 2. Detect signals
    signals = detect_signals(state["order"], profile)
    
    # 3. Add inventory stock checks
    signals["inventory"] = check_inventory_risk(state["order"], supabase)
    
    state["signals"] = signals
    return state


async def reason_node(state: AgentState) -> AgentState:
    """
    Node 2: Classify anomaly with LLM using signals + context.
    """
    logger.info(f"[LangGraph Node: reason] Executing LLM classification for order {state.get('order_id')}")
    result = await call_llm_for_reasoning(state["order"], state["profile"], state["signals"])
    state["result"] = result
    return state


def persist_node(state: AgentState) -> AgentState:
    """
    Node 3: Write results to anomaly_flags table in Supabase and trigger notifications for high/critical anomalies.
    """
    logger.info(f"[LangGraph Node: persist] Saving results to DB for order {state.get('order_id')}")
    supabase = get_supabase()
    if not supabase:
        logger.error("Supabase client unavailable. Skipping persistence.")
        return state

    res = state["result"]
    order_id = state["order_id"]
    business_id = state["business_id"]

    try:
        # Save into anomaly_flags table
        flag_insert = supabase.table("anomaly_flags").insert({
            "order_id": order_id,
            "business_id": business_id,
            "is_flagged": res.get("is_flagged", False),
            "severity": res.get("severity", "low"),
            "anomaly_type": res.get("anomaly_type", []),
            "llm_reasoning": res.get("llm_reasoning", ""),
            "recommended_action": res.get("recommended_action", "approve"),
            "confidence_score": res.get("confidence_score", 1.00),
            "raw_signals": state.get("signals", {}),
            "model_used": "groq/llama-3.3-70b"
        }).execute()
        
        # If flagged, update order status to 'pending_review'
        if res.get("is_flagged", False):
            supabase.table("orders").update({"status": "pending_review"}).eq("id", order_id).execute()
        else:
            supabase.table("orders").update({"status": "auto_approved"}).eq("id", order_id).execute()

        logger.info(f"Successfully saved anomaly flags for order {order_id}. Flagged: {res.get('is_flagged')}")
        
        # Trigger real-time notifications for High & Critical anomalies
        if flag_insert.data and res.get("severity") in ["high", "critical"]:
            try:
                from app.services.notifications import send_owner_alert_notification
                send_owner_alert_notification(
                    business_id=business_id,
                    flag=flag_insert.data[0],
                    order=state["order"],
                    supabase=supabase
                )
            except Exception as e:
                logger.error(f"Error executing alert notification dispatch: {str(e)}")
                
    except Exception as e:
        logger.error(f"Error persisting anomaly flags: {str(e)}", exc_info=True)

    return state


# Build the state graph
workflow = StateGraph(AgentState)

# Add nodes
workflow.add_node("load_context", load_context_node)
workflow.add_node("reason", reason_node)
workflow.add_node("persist", persist_node)

# Set workflow layout
workflow.set_entry_point("load_context")
workflow.add_edge("load_context", "reason")
workflow.add_edge("reason", "persist")
workflow.add_edge("persist", END)

# Compile
anomaly_agent = workflow.compile()
