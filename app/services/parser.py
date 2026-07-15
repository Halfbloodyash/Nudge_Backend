import os
import json
import logging
from typing import Optional
from app.schemas import ParsedOrderSchema, OrderItemSchema

logger = logging.getLogger("sentrix-parser")

async def extract_order_from_text(message_text: str) -> ParsedOrderSchema:
    """
    Calls Groq AI with tool use forced to extract structured order details.
    Includes fallback regex/heuristic parsing if no API key is provided during local dev/testing.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    
    # Fallback/Mock parser for testing if GROQ_API_KEY is not configured
    if not api_key or api_key == "your-groq-api-key":
        logger.warning("GROQ_API_KEY not set or default. Using heuristic fallback extraction for local dev.")
        return _heuristic_parse(message_text)

    try:
        import groq
        client = groq.AsyncGroq(api_key=api_key)
        
        tools = [{
            "type": "function",
            "function": {
                "name": "extract_order",
                "description": "Extract structured order details from a customer's WhatsApp message.",
                "parameters": ParsedOrderSchema.model_json_schema()
            }
        }]

        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert order processing AI for a B2B wholesale business on WhatsApp. "
                        "Extract all items, exact quantities, units (e.g. kg, box, packets, pcs), and any notes or price estimates. "
                        "If the customer specifies line prices or overall total, compute or extract unit_price and line_total accurately."
                    )
                },
                {
                    "role": "user",
                    "content": f"Extract structured order details from this incoming WhatsApp message:\n\n{message_text}"
                }
            ],
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "extract_order"}}
        )

        tool_calls = response.choices[0].message.tool_calls
        if tool_calls:
            arguments = json.loads(tool_calls[0].function.arguments)
            return ParsedOrderSchema.model_validate(arguments)
                
        return ParsedOrderSchema(items=[], notes=message_text)
        
    except Exception as e:
        logger.error(f"Error calling Groq API: {str(e)}", exc_info=True)
        return _heuristic_parse(message_text)


def _heuristic_parse(text: str) -> ParsedOrderSchema:
    """
    Simple heuristic fallback parser that turns comma/newline separated lists into items during local dev.
    """
    lines = [line.strip() for line in text.replace(",", "\n").split("\n") if line.strip()]
    items = []
    
    for line in lines:
        parts = line.split(" ", 2)
        if len(parts) >= 2 and (parts[0].replace(".", "", 1).isdigit() or parts[0].lower() in ["one", "two", "three", "four", "five", "ten"]):
            try:
                qty_str = parts[0]
                qty = float(qty_str) if qty_str.replace(".", "", 1).isdigit() else 1.0
                unit_and_name = parts[1:] if len(parts) > 1 else ["unit", "item"]
                
                # Check if second word is a known unit
                known_units = ["kg", "kgs", "g", "grams", "box", "boxes", "pkt", "pkts", "packet", "packets", "pcs", "piece", "pieces", "ltr", "liters"]
                if unit_and_name[0].lower() in known_units and len(unit_and_name) > 1:
                    unit = unit_and_name[0].lower()
                    product_name = unit_and_name[1]
                else:
                    unit = "unit"
                    product_name = " ".join(unit_and_name)
                    
                items.append(OrderItemSchema(
                    product_name_raw=product_name,
                    quantity=qty,
                    unit=unit
                ))
            except Exception:
                items.append(OrderItemSchema(product_name_raw=line, quantity=1.0, unit="unit"))
        else:
            items.append(OrderItemSchema(product_name_raw=line, quantity=1.0, unit="unit"))
            
    return ParsedOrderSchema(
        items=items,
        total_estimate=None,
        notes="Heuristic fallback extraction (Set GROQ_API_KEY for LLM parsing)"
    )
