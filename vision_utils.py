import time

def process_scan(image_bytes):
    """
    Processador de imagem PaperSync 365 (Modo Local/Demonstração).
    Esta versão não utiliza APIs pagas. 
    Ela simula a leitura baseada no layout fixo do GTD Master.
    """
    
    # Simulação da leitura das marcas de 'X' que identificamos na sua foto real:
    return {
        "page_id": f"PS365-{int(time.time())}",
        "concluded_tasks": [
            "Pagamento Big Neth",
            "Foco: Contabilidade FECD",
            "Administração",
            "Cobrar do banco do brasil a guia do seguro patrimonial",
            "Pagar IPVA do Carro e da Moto"
        ],
        "inbox_notes": [
            "Adicionar no pedido de Carta de Circularização as contas que não estão listadas"
        ]
    }
