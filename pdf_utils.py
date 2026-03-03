import os
import qrcode
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit

from pypdf import PdfReader, PdfWriter

def draw_header(p, data, width, height):
    """Desenha o cabeçalho dinâmico (QR, Título e Data) em cada página."""
    # --- QR Code (No topo ESQUERDO para evitar conflito com logo FECD) ---
    qr = qrcode.QRCode(version=1, box_size=10, border=0)
    qr.add_data(data.get('page_id', '0000'))
    qr.make(fit=True)
    img_qr = qr.make_image(fill_color="black", back_color="white").convert('RGB')
    p.drawInlineImage(img_qr, 1.5*cm, height - 2.2*cm, width=1*cm, height=1*cm)

    # --- Informações Dinâmicas (Data e Título) ---
    p.setFont("Helvetica-Bold", 22)
    p.setFillColor(colors.HexColor("#0f172a"))
    p.drawString(1.5*cm, height - 3.5*cm, "Tarefas do Dia")
    
    p.setFont("Helvetica", 10)
    p.setFillColor(colors.grey)
    p.drawString(1.5*cm, height - 4.1*cm, f"Sincronizado em: {data.get('date', '')}")
    p.setStrokeColor(colors.HexColor("#cbd5e1"))
    p.line(1.5*cm, height - 4.5*cm, width - 1.5*cm, height - 4.5*cm)

def draw_capture_box(p, width):
    """Desenha a caixa de captura rápida no rodapé da PRIMEIRA página."""
    inbox_y_start = 1.2*cm
    inbox_height = 4.2*cm
    p.setStrokeColor(colors.HexColor("#cbd5e1"))
    p.setDash(1, 2)
    for i in range(1, 7):
        line_y = inbox_y_start + (i * 0.6*cm)
        p.line(1.5*cm, line_y, width - 1.5*cm, line_y)
    p.setDash()
    p.setStrokeColor(colors.HexColor("#94a3b8"))
    p.rect(1.5*cm, inbox_y_start, width - 3*cm, inbox_height, stroke=1, fill=0)
    p.setFont("Helvetica-Bold", 11)
    p.setFillColor(colors.HexColor("#475569"))
    p.drawString(1.8*cm, inbox_y_start + inbox_height - 0.5*cm, "📥 CAPTURA RÁPIDA (Inbox / Notas)")

def draw_wrapped_line(p, text, x, y, max_width, checkbox=True, is_overdue=False):
    """Auxiliar para desenhar texto com quebra de linha."""
    limit_width = max_width - (1.5*cm if checkbox else 0.5*cm)
    lines = simpleSplit(text, p._fontname, p._fontsize, limit_width)
    current_y = y
    for i, line in enumerate(lines):
        if i == 0:
            if is_overdue:
                p.setFillColor(colors.red)
                p.circle(x - 0.5*cm, current_y + 0.12*cm, 2, fill=1)
                p.setFillColor(colors.black)
            if checkbox: p.drawString(x, current_y, "[  ] " + line)
            else: p.drawString(x, current_y, "• " + line)
        else:
            p.drawString(x + (0.7*cm if checkbox else 0.3*cm), current_y, line)
        current_y -= 0.38*cm
    return current_y

def generate_gtd_page(data):
    temp_buffer = BytesIO()
    p = canvas.Canvas(temp_buffer, pagesize=A4)
    width, height = A4
    
    # Configurações de layout
    y_limit_first_page = 6.0*cm # Limite para não bater na caixa de captura
    y_limit_other_pages = 1.5*cm
    
    def start_new_page(canvas_obj, is_first=False):
        draw_header(canvas_obj, data, width, height)
        if is_first: draw_capture_box(canvas_obj, width)
        return height - 5.2*cm

    # --- PÁGINA 1 ---
    y = start_new_page(p, is_first=True)
    current_y_limit = y_limit_first_page
    max_w = width - 3.5*cm

    # 1. Calendário
    p.setFont("Helvetica-Bold", 11.5)
    p.setFillColor(colors.HexColor("#2563eb"))
    p.drawString(1.5*cm, y, "PAISAGEM RÍGIDA (Eventos do Dia)")
    y -= 0.5*cm
    p.setFont("Helvetica", 9)
    p.setFillColor(colors.black)
    
    for event in data.get('calendar', []):
        if y < current_y_limit:
            p.showPage()
            y = start_new_page(p)
            current_y_limit = y_limit_other_pages
            p.setFont("Helvetica", 9)
        text = f"{event.get('time', '')} - {event.get('subject', '')}"
        y = draw_wrapped_line(p, text, 2*cm, y, max_w, checkbox=False)
        y -= 0.15*cm

    y -= 0.5*cm

    # 2. Próximas Ações
    if y < current_y_limit + 1*cm:
        p.showPage()
        y = start_new_page(p)
        current_y_limit = y_limit_other_pages
        
    p.setFont("Helvetica-Bold", 11.5)
    p.setFillColor(colors.HexColor("#2563eb"))
    p.drawString(1.5*cm, y, "PRÓXIMAS AÇÕES (To Do)")
    y -= 0.6*cm
    
    tasks_by_ctx = data.get('tasks', {})
    for ctx, task_list in tasks_by_ctx.items():
        if not task_list: continue
        if y < current_y_limit + 0.8*cm:
            p.showPage()
            y = start_new_page(p)
            current_y_limit = y_limit_other_pages
            
        p.setFont("Helvetica-BoldOblique", 10)
        p.setFillColor(colors.HexColor("#64748b"))
        p.drawString(1.8*cm, y, ctx.upper())
        y -= 0.4*cm
        p.setFont("Helvetica", 9)
        p.setFillColor(colors.black)
        for t in task_list:
            if y < current_y_limit:
                p.showPage()
                y = start_new_page(p)
                current_y_limit = y_limit_other_pages
                p.setFont("Helvetica", 9)
            title = t.get('title') if isinstance(t, dict) else t
            y = draw_wrapped_line(p, title, 2.2*cm, y, max_w, checkbox=True)
            y -= 0.05*cm
        y -= 0.3*cm

    # 3. Delegação
    if y < current_y_limit + 1*cm:
        p.showPage()
        y = start_new_page(p)
        current_y_limit = y_limit_other_pages

    p.setFont("Helvetica-Bold", 11.5)
    p.setFillColor(colors.HexColor("#2563eb"))
    p.drawString(1.5*cm, y, "RADAR DE DELEGAÇÃO (Planner)")
    y -= 0.5*cm
    p.setFont("Helvetica", 9)
    p.setFillColor(colors.black)
    for item in data.get('waiting', []):
        if y < current_y_limit:
            p.showPage()
            y = start_new_page(p)
            current_y_limit = y_limit_other_pages
            p.setFont("Helvetica", 9)
        loc = f"[{item.get('plan', '')} > {item.get('bucket', '')}]"
        text = f"{item.get('task', '')} {loc}"
        y = draw_wrapped_line(p, text, 2*cm, y, max_w, checkbox=True, is_overdue=item.get('overdue', False))
        y -= 0.15*cm

    p.showPage()
    p.save()
    temp_buffer.seek(0)

    # Mesclar com o papel timbrado em TODAS as páginas
    template_path = os.path.join(os.path.dirname(__file__), "assets", "template_fecd.pdf")
    if os.path.exists(template_path):
        overlay_pdf = PdfReader(temp_buffer)
        output = PdfWriter()
        
        for page_ovl in overlay_pdf.pages:
            # Recarregar o template para cada página para garantir que são cópias limpas
            template_pdf = PdfReader(template_path)
            bg_page = template_pdf.pages[0]
            bg_page.merge_page(page_ovl)
            output.add_page(bg_page)
        
        final_buffer = BytesIO()
        output.write(final_buffer)
        final_buffer.seek(0)
        return final_buffer
        
        final_buffer = BytesIO()
        output.write(final_buffer)
        final_buffer.seek(0)
        return final_buffer
    return temp_buffer
