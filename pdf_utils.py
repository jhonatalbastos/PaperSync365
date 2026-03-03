import os
import qrcode
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit

from pypdf import PdfReader, PdfWriter

def draw_wrapped_line(p, text, x, y, max_width, checkbox=True, is_overdue=False):
    """Auxiliar para desenhar texto com quebra de linha e marcas de status."""
    limit_width = max_width - (1.5*cm if checkbox else 0.5*cm)
    lines = simpleSplit(text, p._fontname, p._fontsize, limit_width)
    
    current_y = y
    for i, line in enumerate(lines):
        if i == 0:
            if is_overdue:
                p.setFillColor(colors.red)
                p.circle(x - 0.5*cm, current_y + 0.15*cm, 2, fill=1)
                p.setFillColor(colors.black)
            
            if checkbox:
                p.drawString(x, current_y, "[  ] " + line)
            else:
                p.drawString(x, current_y, "• " + line)
        else:
            p.drawString(x + (0.7*cm if checkbox else 0.3*cm), current_y, line)
        
        current_y -= 0.45*cm
        if current_y < 1.0*cm: break
    return current_y

def generate_gtd_page(data):
    """
    Gera um PDF A4 usando o Papel Timbrado oficial como base e sobrepondo dados GTD.
    """
    # 1. Gerar o conteúdo dinâmico (Tarefas, QR, Data) em um buffer temporário
    temp_buffer = BytesIO()
    p = canvas.Canvas(temp_buffer, pagesize=A4)
    width, height = A4

    # --- QR Code (No topo direito, ajustado ao timbrado) ---
    qr = qrcode.QRCode(version=1, box_size=10, border=0)
    qr.add_data(data.get('page_id', '0000'))
    qr.make(fit=True)
    img_qr = qr.make_image(fill_color="black", back_color="white").convert('RGB')
    p.drawInlineImage(img_qr, width - 2.5*cm, height - 2.2*cm, width=1*cm, height=1*cm)

    # --- Informações Dinâmicas (Data e Título) ---
    p.setFont("Helvetica-Bold", 24)
    p.setFillColor(colors.HexColor("#0f172a"))
    p.drawString(1.5*cm, height - 3.5*cm, "Tarefas do Dia")
    
    p.setFont("Helvetica", 11)
    p.setFillColor(colors.grey)
    p.drawString(1.5*cm, height - 4.1*cm, f"Sincronizado em: {data.get('date', '')}")
    p.setStrokeColor(colors.HexColor("#cbd5e1"))
    p.line(1.5*cm, height - 4.5*cm, width - 1.5*cm, height - 4.5*cm)

    y = height - 5.5*cm
    max_w = width - 3.5*cm

    # --- 1. Calendário (Sem Checkbox) ---
    p.setFont("Helvetica-Bold", 13)
    p.setFillColor(colors.HexColor("#2563eb"))
    p.drawString(1.5*cm, y, "PAISAGEM RÍGIDA (Eventos do Dia)")
    y -= 0.6*cm
    p.setFont("Helvetica", 10)
    p.setFillColor(colors.black)
    
    for event in data.get('calendar', []):
        text = f"{event.get('time', '')} - {event.get('subject', '')}"
        y = draw_wrapped_line(p, text, 2*cm, y, max_w, checkbox=False)
        y -= 0.2*cm
        if y < 18*cm: break

    y -= 0.8*cm

    # --- 2. Próximas Ações (Priorizadas) ---
    p.setFont("Helvetica-Bold", 13)
    p.setFillColor(colors.HexColor("#2563eb"))
    p.drawString(1.5*cm, y, "PRÓXIMAS AÇÕES (To Do)")
    y -= 0.7*cm
    
    tasks_by_ctx = data.get('tasks', {})
    for ctx, task_list in tasks_by_ctx.items():
        if not task_list: continue
        p.setFont("Helvetica-BoldOblique", 11)
        p.setFillColor(colors.HexColor("#64748b"))
        p.drawString(1.8*cm, y, ctx.upper())
        y -= 0.5*cm
        p.setFont("Helvetica", 10)
        p.setFillColor(colors.black)
        for t in task_list:
            is_over = t.get('overdue', False) if isinstance(t, dict) else False
            title = t.get('title') if isinstance(t, dict) else t
            y = draw_wrapped_line(p, title, 2.2*cm, y, max_w, checkbox=True, is_overdue=is_over)
            y -= 0.1*cm
            if y < 10*cm: break
        y -= 0.4*cm

    # --- 3. Delegação (Com Plan/Bucket) ---
    if y > 8*cm:
        p.setFont("Helvetica-Bold", 13)
        p.setFillColor(colors.HexColor("#2563eb"))
        p.drawString(1.5*cm, y, "RADAR DE DELEGAÇÃO (Aguardando Resposta)")
        y -= 0.6*cm
        p.setFont("Helvetica", 10)
        for item in data.get('waiting', []):
            loc = f"[{item.get('plan', '')} > {item.get('bucket', '')}]"
            text = f"{item.get('task', '')} {loc}"
            y = draw_wrapped_line(p, text, 2*cm, y, max_w, checkbox=True)
            y -= 0.2*cm

    # --- 4. Captura Rápida Ampla com Linhas ---
    inbox_y_start = 1.5*cm
    inbox_height = 5*cm
    p.setStrokeColor(colors.HexColor("#cbd5e1"))
    p.setDash(1, 2) # Linhas pontilhadas
    
    # Desenha as linhas de escrita
    for i in range(1, 8):
        line_y = inbox_y_start + (i * 0.6*cm)
        p.line(1.5*cm, line_y, width - 1.5*cm, line_y)
    
    p.setDash() # Volta ao normal
    p.setStrokeColor(colors.HexColor("#94a3b8"))
    p.rect(1.5*cm, inbox_y_start, width - 3*cm, inbox_height, stroke=1, fill=0)
    
    p.setFont("Helvetica-Bold", 12)
    p.setFillColor(colors.HexColor("#475569"))
    p.drawString(1.8*cm, inbox_y_start + inbox_height - 0.5*cm, "📥 CAPTURA RÁPIDA (Inbox / Notas)")

    p.showPage()
    p.save()
    temp_buffer.seek(0)

    # 2. Mesclar com o papel timbrado oficial (Background)
    template_path = os.path.join(os.path.dirname(__file__), "assets", "template_fecd.pdf")
    if os.path.exists(template_path):
        try:
            overlay_pdf = PdfReader(temp_buffer)
            template_pdf = PdfReader(template_path)
            
            output = PdfWriter()
            page = template_pdf.pages[0]
            # O conteúdo GTD (overlay) é fundido sobre o papel timbrado
            page.merge_page(overlay_pdf.pages[0])
            output.add_page(page)
            
            final_buffer = BytesIO()
            output.write(final_buffer)
            final_buffer.seek(0)
            return final_buffer
        except Exception as e:
            # Fallback caso ocorra erro na mesclagem (ex: pypdf não instalado ou PDF corrompido)
            return temp_buffer
    
    return temp_buffer
