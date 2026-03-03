import os
import json
import time
from datetime import datetime

DB_FILE = "papersync_db.json"

def _load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"snapshots": {}, "inbox_captured": []}

def _save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def save_page_snapshot(page_id, data):
    """
    Salva o que foi impresso em uma folha específica.
    data = {
        'tasks': [...],
        'calendar': [...]
    }
    """
    db = _load_db()
    db["snapshots"][page_id] = {
        "timestamp": datetime.now().isoformat(),
        "content": data
    }
    _save_db(db)

def capture_inbox_note(note):
    """Adiciona uma nota capturada do papel à lista de espera para processamento."""
    db = _load_db()
    db["inbox_captured"].append({
        "text": note,
        "date": datetime.now().isoformat(),
        "processed": False
    })
    _save_db(db)

def get_unprocessed_inbox_notes():
    db = _load_db()
    return [n for n in db["inbox_captured"] if not n.get("processed", False)]

def mark_note_as_processed(note_text):
    db = _load_db()
    for n in db["inbox_captured"]:
        if n["text"] == note_text:
            n["processed"] = True
    _save_db(db)

def process_scan(image_bytes):
    """
    Processador de imagem (Versão Snapshot).
    Em uma implementação real com OpenCV, aqui faríamos a detecção do QR e marcas.
    """
    # Para o MVP, continuaremos usando a lógica baseada na sua foto real,
    # mas agora vinculada ao sistema de Captura e Snapshots.
    
    # Simulação de OCR de rodapé detetado na sua foto:
    note = "Adicionar no pedido de Carta de Circularização as contas que não estão listadas"
    capture_inbox_note(note)
    
    return {
        "page_id": "PS365-REAL-SCAN",
        "concluded_tasks": [
            "Pagamento Big Neth",
            "Foco: Contabilidade FECD",
            "Administração",
            "Cobrar do banco do brasil a guia do seguro patrimonial",
            "Pagar IPVA do Carro e da Moto"
        ],
        "inbox_notes": [note]
    }
