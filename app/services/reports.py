import logging
from io import BytesIO
from datetime import datetime
from collections import Counter
from typing import Any, List, Dict
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

logger = logging.getLogger("nudge-reports")

def generate_pdf_report(business_id: str, start_date: str, end_date: str, supabase: Any, status: str = None, severity: str = None) -> bytes:
    """
    Generates a stylized PDF report using ReportLab containing summary statistics,
    orders count, anomaly alerts, and human decisions log.
    """
    logger.info(f"Generating PDF report for business {business_id} from {start_date} to {end_date}")
    
    # Default fallback data if Supabase connection is offline
    orders = []
    flags = []
    business_name = "Nudge Demo Wholesale"
    
    if supabase:
        try:
            # Get Business Name
            biz_res = supabase.table("businesses").select("name").eq("id", business_id).execute()
            if biz_res.data:
                business_name = biz_res.data[0].get("name", business_name)
                
            # Query orders in date range
            ord_query = (
                supabase.table("orders")
                .select("*, customers(name, whatsapp_phone)")
                .eq("business_id", business_id)
                .gte("order_time", f"{start_date}T00:00:00")
                .lte("order_time", f"{end_date}T23:59:59")
            )
            if status:
                ord_query = ord_query.eq("status", status)
            
            ord_res = ord_query.order("order_time", desc=True).execute()
            orders = ord_res.data or []
            
            # Query anomaly flags in date range
            flg_query = (
                supabase.table("anomaly_flags")
                .select("*, orders(id, total_value, status, customers(name))")
                .eq("business_id", business_id)
                .gte("created_at", f"{start_date}T00:00:00")
                .lte("created_at", f"{end_date}T23:59:59")
            )
            if severity:
                flg_query = flg_query.eq("severity", severity)
                
            flg_res = flg_query.execute()
            flags = flg_res.data or []
        except Exception as e:
            logger.error(f"Error querying database for PDF report generation: {str(e)}")

    # Calculate statistics
    total_orders = len(orders)
    total_val = sum(float(o.get("total_value") or 0.0) for o in orders)
    flagged_orders_count = sum(1 for f in flags if f.get("is_flagged", False))
    
    # Order Status counts
    statuses = [o.get("status") for o in orders]
    status_counts = Counter(statuses)
    
    approved_count = status_counts.get("approved", 0) + status_counts.get("auto_approved", 0)
    rejected_count = status_counts.get("rejected", 0)
    modified_count = status_counts.get("modified", 0)
    pending_count = status_counts.get("pending_review", 0)

    # Anomaly severity counts
    severities = [f.get("severity") for f in flags if f.get("is_flagged", False)]
    sev_counts = Counter(severities)

    # Build the document
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=0.5*inch,
        rightMargin=0.5*inch,
        topMargin=0.5*inch,
        bottomMargin=0.5*inch
    )
    
    styles = getSampleStyleSheet()
    
    # Custom color palette (sleek tech navy)
    primary_color = colors.HexColor("#0f172a")
    secondary_color = colors.HexColor("#10b981")
    text_color = colors.HexColor("#334155")
    bg_light = colors.HexColor("#f8fafc")
    border_color = colors.HexColor("#e2e8f0")

    # Typography styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=22,
        leading=26,
        textColor=primary_color,
        spaceAfter=6
    )
    
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=12,
        leading=14,
        textColor=colors.HexColor("#64748b"),
        spaceAfter=20
    )
    
    h2_style = ParagraphStyle(
        'Header2',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=14,
        leading=16,
        textColor=primary_color,
        spaceBefore=15,
        spaceAfter=10
    )

    body_style = ParagraphStyle(
        'DocBody',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=13,
        textColor=text_color
    )
    
    th_style = ParagraphStyle(
        'TableHead',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=11,
        textColor=colors.white
    )

    td_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=11,
        textColor=text_color
    )

    story = []

    # Title & Subtitle Header
    story.append(Paragraph("Nudge AI Order Processing Report", title_style))
    story.append(Paragraph(f"Business: {business_name} | Date Range: {start_date} to {end_date}", subtitle_style))
    story.append(Spacer(1, 10))

    # Metric summary table
    summary_data = [
        [
            Paragraph("<b>Total Value Flow</b>", td_style),
            Paragraph("<b>Total Orders</b>", td_style),
            Paragraph("<b>Flagged Audits</b>", td_style),
            Paragraph("<b>Rejection Rate</b>", td_style)
        ],
        [
            Paragraph(f"<font size=12 color='#10b981'><b>₹{total_val:,.2f}</b></font>", td_style),
            Paragraph(f"<font size=12><b>{total_orders}</b></font>", td_style),
            Paragraph(f"<font size=12 color='#f43f5e'><b>{flagged_orders_count}</b></font>", td_style),
            Paragraph(f"<font size=12><b>{(rejected_count / total_orders * 100) if total_orders > 0 else 0:.1f}%</b></font>", td_style)
        ]
    ]
    summary_table = Table(summary_data, colWidths=[2.0*inch, 1.5*inch, 1.5*inch, 1.5*inch])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), bg_light),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOX', (0, 0), (-1, -1), 1, border_color),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, border_color),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 20))

    # Order Review status group
    story.append(Paragraph("Decision Reviews Breakdown", h2_style))
    status_data = [
        [Paragraph("<b>Status Code</b>", th_style), Paragraph("<b>Count</b>", th_style), Paragraph("<b>Percentage</b>", th_style)],
        [Paragraph("Auto Approved / Approved", td_style), Paragraph(str(approved_count), td_style), Paragraph(f"{(approved_count / total_orders * 100) if total_orders > 0 else 0:.1f}%", td_style)],
        [Paragraph("Modified & Approved", td_style), Paragraph(str(modified_count), td_style), Paragraph(f"{(modified_count / total_orders * 100) if total_orders > 0 else 0:.1f}%", td_style)],
        [Paragraph("Rejected", td_style), Paragraph(str(rejected_count), td_style), Paragraph(f"{(rejected_count / total_orders * 100) if total_orders > 0 else 0:.1f}%", td_style)],
        [Paragraph("Pending Human Review", td_style), Paragraph(str(pending_count), td_style), Paragraph(f"{(pending_count / total_orders * 100) if total_orders > 0 else 0:.1f}%", td_style)],
    ]
    status_table = Table(status_data, colWidths=[3.0*inch, 1.5*inch, 2.0*inch])
    status_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), primary_color),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, bg_light]),
        ('BOX', (0, 0), (-1, -1), 0.5, border_color),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, border_color),
    ]))
    story.append(status_table)
    story.append(Spacer(1, 20))

    # Detailed flagged anomaly table
    story.append(Paragraph("Flagged Anomalies Audit Logs", h2_style))
    
    if not flags:
        story.append(Paragraph("No anomalies were flagged by the LangGraph auditor during this period.", body_style))
    else:
        flag_data = [
            [
                Paragraph("<b>Order Date</b>", th_style), 
                Paragraph("<b>Customer</b>", th_style), 
                Paragraph("<b>Value</b>", th_style), 
                Paragraph("<b>Severity</b>", th_style),
                Paragraph("<b>AI Reasoning / Flags</b>", th_style)
            ]
        ]
        
        for flag in flags[:15]: # Show top 15 flags
            order_data = flag.get("orders") or {}
            cust_data = order_data.get("customers") or {}
            
            created_at_dt = datetime.fromisoformat(flag.get("created_at", "").replace("Z", "+00:00"))
            date_str = created_at_dt.strftime("%Y-%m-%d %H:%M")
            
            client_name = cust_data.get("name", "Unknown")
            value_str = f"₹{float(order_data.get('total_value') or 0.0):,.2f}"
            sev = flag.get("severity", "low").upper()
            reasoning = flag.get("llm_reasoning", "No details")
            
            flag_data.append([
                Paragraph(date_str, td_style),
                Paragraph(client_name, td_style),
                Paragraph(value_str, td_style),
                Paragraph(f"<font color='{'#f43f5e' if sev in ['HIGH','CRITICAL'] else '#d97706'}'><b>{sev}</b></font>", td_style),
                Paragraph(reasoning, td_style),
            ])
            
        flag_table = Table(flag_data, colWidths=[1.1*inch, 1.2*inch, 0.9*inch, 0.8*inch, 3.5*inch])
        flag_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), primary_color),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, bg_light]),
            ('BOX', (0, 0), (-1, -1), 0.5, border_color),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, border_color),
        ]))
        story.append(flag_table)

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes
