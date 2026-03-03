import os
import qrcode
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors

def generate_gtd_page(data):
    """
    Gera um PDF A4 seguindo o layout GTD Master.
    data = {
        'date': '03/03/2026',
        'calendar': [{'time': '10:00', 'subject': 'Reunião...'}, ...],
        'tasks': {'@Computador': [...], '@Telefone': [...]},
        'waiting': [{'who': 'João', 'task': '...'}],
        'page_id': 'unique-uuid-or-id'
    }
    """
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    # --- Margens e Bordas ---
    p.setStrokeColor(colors.lightgrey)
    p.rect(1*cm, 1*cm, width-2*cm, height-2*cm)

    # --- QR Code ---
    qr = qrcode.QRCode(version=1, box_size=10, border=0)
    qr.add_data(data.get('page_id', '0000'))
    qr.make(fit=True)
    img_qr = qr.make_image(fill_color="black", back_color="white").convert('RGB')
    
    # Desenha o QR Code no PDF
    p.drawInlineImage(img_qr, width - 3.5*cm, height - 3.5*cm, width=2*cm, height=2*cm)

    # --- Cabeçalho ---
    p.setFont("Helvetica-Bold", 18)
    p.drawString(2*cm, height - 2.5*cm, "PaperSync 365")
    p.setFont("Helvetica", 12)
    p.drawString(2*cm, height - 3.1*cm, f"Data: {data.get('date', '')}")
    p.line(1*cm, height - 4*cm, width - 1*cm, height - 4*cm)

    y = height - 5*cm

    # --- Bloco 1: Paisagem Rígida (Calendário) ---
    p.setFont("Helvetica-Bold", 14)
    p.setFillColor(colors.darkblue)
    p.drawString(2*cm, y, "🗓️ Paisagem Rígida (Outlook/Calendar)")
    p.setFillColor(colors.black)
    p.setFont("Helvetica", 10)
    y -= 0.6*cm
    
    for event in data.get('calendar', [])[:5]:
        p.drawString(2.5*cm, y, f"[  ] {event.get('time', '')} - {event.get('subject', '')}")
        y -= 0.5*cm
    
    y -= 0.5*cm
    p.line(2*cm, y, width - 2*cm, y)
    y -= 1*cm

    # --- Bloco 2: Próximas Ações (Por Contexto) ---
    p.setFont("Helvetica-Bold", 14)
    p.setFillColor(colors.darkblue)
    p.drawString(2*cm, y, "⚡ Próximas Ações (To Do)")
    p.setFillColor(colors.black)
    y -= 0.8*cm

    tasks = data.get('tasks', {})
    for ctx, task_list in tasks.items():
        if not task_list: continue
        p.setFont("Helvetica-BoldOblique", 11)
        p.drawString(2.2*cm, y, f"{ctx}")
        y -= 0.5*cm
        p.setFont("Helvetica", 10)
        for t in task_list[:4]:
            p.drawString(2.8*cm, y, f"[  ] {t}")
            y -= 0.5*cm
        y -= 0.3*cm
        if y < 8*cm: break # Evita sair da página

    y -= 0.5*cm
    p.line(2*cm, y, width - 2*cm, y)
    y -= 1*cm

    # --- Bloco 3: Aguardando Resposta (Planner) ---
    p.setFont("Helvetica-Bold", 14)
    p.setFillColor(colors.darkblue)
    p.drawString(2*cm, y, "📡 Radar de Delegação (Planner)")
    p.setFillColor(colors.black)
    p.setFont("Helvetica", 10)
    y -= 0.6*cm

    for item in data.get('waiting', [])[:5]:
        p.drawString(2.5*cm, y, f"[  ] Para: {item.get('who', '')} | {item.get('task', '')}")
        y -= 0.5*cm

    # --- Bloco 4: Inbox / Captura (Rodapé) ---
    p.setStrokeColor(colors.black)
    p.rect(2*cm, 2*cm, width-4*cm, 3*cm)
    p.setFont("Helvetica-Bold", 12)
    p.drawString(2.2*cm, 4.6*cm, "📥 Captura Rápida (Inbox)")

    p.showPage()
    p.save()
    
    buffer.seek(0)
    return buffer
