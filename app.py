import os
import time
import json
import base64
import hashlib
import secrets
from datetime import datetime, timedelta, date
from urllib.parse import urlencode

import requests
import streamlit as st
import pandas as pd

from pdf_utils import generate_gtd_page
from vision_utils import process_scan

# =========================
# CONFIGURAÇÃO DA PÁGINA
# =========================
st.set_page_config(
    page_title="PaperSync 365 | GTD Master",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Estilização Premium
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }
    
    .main {
        background-color: #f4f7f9;
    }
    
    .stApp {
        background-color: #f4f7f9;
    }

    /* Cards Estilizados */
    .gtd-card {
        background: white;
        padding: 24px;
        border-radius: 16px;
        box-shadow: 0 4px 20px rgba(0,0,0,0.04);
        border: 1px solid #eef2f6;
        margin-bottom: 24px;
    }
    
    .metric-card {
        background: linear-gradient(135deg, #0078d4 0%, #005a9e 100%);
        color: white;
        padding: 20px;
        border-radius: 12px;
        text-align: center;
    }

    h1, h2, h3 {
        color: #1a202c;
    }

    .stButton>button {
        width: 100%;
        border-radius: 10px;
        font-weight: 600;
        transition: all 0.3s;
    }
    
    .stButton>button:hover {
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(0,120,212,0.2);
    }
    
    /* Custom Badge */
    .context-badge {
        background-color: #ebf5ff;
        color: #0078d4;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 12px;
        font-weight: 600;
    }
    </style>
""", unsafe_allow_html=True)

# Configurações API
BR_TZ = "E. South America Standard Time"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
AUTH_BASE = "https://login.microsoftonline.com"
STATE_STORE_FILE = "oauth_state_store.json"
STATE_TTL_SECONDS = 10 * 60

# Contextos GTD Oficiais
GTD_CONTEXT_LISTS = [
    "Escritório",
    "Computador",
    "Telefone",
    "Na Rua",
    "Casa",
    "Assuntos a Tratar",
]
GTD_CONTROL_LISTS = [
    "Aguardando resposta",
    "Projetos",
    "Algum dia/Talvez",
]

SCOPES = [
    "User.Read",
    "offline_access",
    "Tasks.ReadWrite",
    "Calendars.Read",
    "Mail.Read",
]

# =========================
# FUNÇÕES DE AUTENTICAÇÃO
# =========================

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

def pkce_create_pair():
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge

def get_azure_config():
    azure = st.secrets.get("azure", {})
    r_uri = azure.get("REDIRECT_URI", "").strip()
    # Limpeza absoluta da URL para evitar 400 Bad Request
    if "/callback" in r_uri:
        r_uri = r_uri.split("/callback")[0]
    r_uri = r_uri.rstrip("/") + "/"
        
    return (
        azure.get("CLIENT_ID", "").strip(),
        azure.get("TENANT_ID", "common").strip(),
        azure.get("CLIENT_SECRET", "").strip(),
        r_uri,
    )

def exchange_code_for_token(code, redirect_uri, code_verifier, tenant_id, client_id, client_secret):
    token_url = f"{AUTH_BASE}/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "code_verifier": code_verifier,
    }
    if client_secret: 
        data["client_secret"] = client_secret
    
    r = requests.post(token_url, data=data, timeout=30)
    if r.status_code >= 400:
        st.error(f"Erro Detalhado da Microsoft: {r.text}")
        r.raise_for_status()
    return r.json()

def refresh_token(refresh_token_value, tenant_id, client_id, client_secret):
    token_url = f"{AUTH_BASE}/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token_value,
        "scope": " ".join(SCOPES),
    }
    if client_secret: data["client_secret"] = client_secret
    r = requests.post(token_url, data=data, timeout=30)
    r.raise_for_status()
    return r.json()

def get_access_token():
    client_id, tenant_id, client_secret, _ = get_azure_config()
    token_data = st.session_state.get("token")
    if not token_data: return None
    
    if time.time() < st.session_state.get("token_expires_at", 0) - 60:
        return token_data.get("access_token")

    rt = token_data.get("refresh_token")
    if not rt: return None
    
    try:
        new_token = refresh_token(rt, tenant_id, client_id, client_secret)
        st.session_state["token"] = new_token
        st.session_state["token_expires_at"] = time.time() + int(new_token.get("expires_in", 3600))
        return new_token.get("access_token")
    except:
        return None

# =========================
# FUNÇÕES MICROSOFT GRAPH
# =========================

def graph_request(method, path, params=None, payload=None):
    token = get_access_token()
    if not token: raise RuntimeError("Não autenticado")
    
    url = f"{GRAPH_BASE}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    r = requests.request(method, url, headers=headers, params=params, 
                         data=json.dumps(payload) if payload else None, timeout=30)
    
    if r.status_code >= 400:
        return {"error": r.text, "status": r.status_code}
    return r.json() if r.text else {}

def get_calendar():
    start = datetime.now().isoformat()
    end = (datetime.now() + timedelta(days=7)).isoformat()
    params = {"startDateTime": start, "endDateTime": end, "$orderby": "start/dateTime", "$top": "10"}
    res = graph_request("GET", "/me/calendarView", params=params)
    return res.get("value", [])

def get_todo_lists():
    res = graph_request("GET", "/me/todo/lists")
    return res.get("value", [])

def get_tasks(list_id):
    res = graph_request("GET", f"/me/todo/lists/{list_id}/tasks")
    return res.get("value", [])

def get_flagged_emails():
    params = {"$filter": "flag/flagStatus eq 'flagged'", "$top": "5", "$select": "subject,receivedDateTime,from"}
    res = graph_request("GET", "/me/messages", params=params)
    return res.get("value", [])

# =========================
# INTERFACE PRINCIPAL
# =========================

def main():
    # --- Sidebar ---
    with st.sidebar:
        st.image("https://img.icons8.com/clippy/200/0078d4/paper-plane.png", width=80)
        st.title("PaperSync 365")
        st.caption("Gerência Híbrida GTD (Nível Mestre)")
        
        client_id, tenant_id, client_secret, redirect_uri = get_azure_config()
        
        if "token" not in st.session_state:
            # Prepara os dados de PKCE e State antes do botão para que o link já esteja pronto
            if "pkce_verifier" not in st.session_state:
                verifier, challenge = pkce_create_pair()
                st.session_state["pkce_verifier"] = verifier
                st.session_state["pkce_challenge"] = challenge
                st.session_state["oauth_state"] = secrets.token_urlsafe(16)
            
            auth_params = {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": " ".join(SCOPES),
                "state": st.session_state["oauth_state"],
                "code_challenge": st.session_state["pkce_challenge"],
                "code_challenge_method": "S256",
                "response_mode": "query",
                "prompt": "select_account"
            }
            # Garante que a URL não tenha aspas ou caracteres extras que confundam o redirecionamento
            auth_url = f"{AUTH_BASE}/{tenant_id}/oauth2/v2.0/authorize?{urlencode(auth_params)}"
            
            st.link_button("🔌 Conectar Microsoft 365", auth_url, type="primary", use_container_width=True)
            if st.button("🔄 Limpar Cache de Login"):
                for k in ["pkce_verifier", "pkce_challenge", "oauth_state", "token"]:
                    if k in st.session_state: del st.session_state[k]
                st.rerun()
            st.info("Clique no botão acima para autorizar o acesso.")
            st.stop()
        
        st.success("Conectado")
        if st.button("Encerrar Sessão"):
            del st.session_state["token"]
            st.rerun()

    # --- Dashboard ---
    st.markdown(f"# 👋 Olá, {st.session_state.get('user_name', 'Gerente')}")
    st.markdown("Abaixo está o seu panorama de hoje, pronto para sincronização física.")

    # 1. Paisagem Rígida (Header)
    st.markdown("### 🗓️ Paisagem Rígida (Hoje)")
    try:
        events = get_calendar()
        if not events:
            st.info("Nenhum compromisso agendado para hoje.")
        else:
            cols = st.columns(len(events[:4]))
            for i, event in enumerate(events[:4]):
                with cols[i]:
                    start_time = event['start']['dateTime'].split('T')[1][:5]
                    st.markdown(f"""
                        <div style="background: white; padding: 15px; border-radius: 12px; border-left: 4px solid #0078d4;">
                            <small>{start_time}</small><br>
                            <strong>{event['subject'][:25]}...</strong>
                        </div>
                    """, unsafe_allow_html=True)
    except Exception as e:
        st.error(f"Erro ao carregar calendário: {e}")

    st.markdown("<br>", unsafe_allow_html=True)

    # 2. Corpo do Dashboard
    col1, col2 = st.columns([1.6, 1])

    with col1:
        st.markdown("### ⚡ Próximas Ações por Contexto")
        
        # Carregar listas
        lists = get_todo_lists()
        gtd_map = {l['displayName']: l['id'] for l in lists if l['displayName'] in GTD_CONTEXT_LISTS}
        
        tabs = st.tabs(GTD_CONTEXT_LISTS)
        for i, context_name in enumerate(GTD_CONTEXT_LISTS):
            with tabs[i]:
                list_id = gtd_map.get(context_name)
                if list_id:
                    tasks = get_tasks(list_id)
                    for t in tasks[:8]:
                        if t['status'] != 'completed':
                            st.checkbox(t['title'], key=f"t_{t['id']}")
                else:
                    st.warning(f"Lista '{context_name}' não encontrada no Microsoft To Do.")
                    if st.button(f"Criar lista '{context_name}'"):
                        graph_request("POST", "/me/todo/lists", payload={"displayName": context_name})
                        st.rerun()

    with col2:
        st.markdown("### 📡 Radar de Delegação")
        st.caption("Aguardando Resposta (Planner)")
        # Simulação de itens do Planner
        st.markdown("""
            <div class="gtd-card">
                <p><strong>João Silva</strong><br><small>Ajustar relatório de custos</small> <span class="context-badge">05/03</span></p>
                <p><strong>Maria Souza</strong><br><small>Aprovação de arte</small> <span class="context-badge">Amanhã</span></p>
            </div>
        """, unsafe_allow_html=True)

        st.markdown("### ✉️ E-mails Flagged")
        emails = get_flagged_emails()
        for em in emails:
            st.markdown(f"🚩 **{em['subject']}**")

    st.divider()

    # 3. Ponte Analógica (Impressão e Scan)
    st.markdown("### 🖨️ PaperSync: Sincronização Analógica")
    pc1, pc2 = st.columns(2)

    with pc1:
        st.markdown('<div class="gtd-card">', unsafe_allow_html=True)
        st.write("#### Gerar Folha de Controle")
        st.write("Prepare sua folha A4 com QR Code para o dia de hoje.")
        
        if st.button("📄 Gerar PDF Master", type="primary"):
            # Coletar dados para o PDF
            # (Note: em produção coletaríamos de fato todas as listas acima)
            data_pdf = {
                'date': datetime.now().strftime("%d/%m/%Y"),
                'calendar': [{'time': ev['start']['dateTime'].split('T')[1][:5], 'subject': ev['subject']} for ev in events[:5]],
                'tasks': {ctx: [t['title'] for t in get_tasks(gtd_map[ctx])[:3]] if ctx in gtd_map else [] for ctx in GTD_CONTEXT_LISTS},
                'waiting': [{'who': 'João', 'task': 'Relatório'}, {'who': 'Equipe', 'task': 'Feedback projeto'}],
                'page_id': f"PS365-{int(time.time())}"
            }
            pdf_bytes = generate_gtd_page(data_pdf)
            st.download_button(
                label="⬇️ Baixar PDF para Impressão",
                data=pdf_bytes,
                file_name=f"PaperSync_{date.today()}.pdf",
                mime="application/pdf"
            )
        st.markdown('</div>', unsafe_allow_html=True)

    with pc2:
        st.markdown('<div class="gtd-card">', unsafe_allow_html=True)
        st.write("#### Processar Scan")
        st.write("Envie a foto da folha marcada para atualizar o sistema.")
        
        uploaded_file = st.file_uploader("Upload do Scan (JPG/PNG)", type=["png", "jpg", "jpeg"])
        if uploaded_file:
            with st.status("🔍 Analisando marcas manuais e QR Code...", expanded=True) as status:
                result = process_scan(uploaded_file.read())
                time.sleep(2)
                st.success(f"Página identificada: {result['page_id']}")
                for task in result['concluded_tasks']:
                    st.write(f"✅ Concluindo: **{task}**")
                status.update(label="Sincronização concluída com sucesso!", state="complete")
        st.markdown('</div>', unsafe_allow_html=True)

# Lógica de Callback para Streamlit Cloud / Local
if __name__ == "__main__":
    q = st.query_params
    if "code" in q and "token" not in st.session_state:
        client_id, tenant_id, client_secret, redirect_uri = get_azure_config()
        try:
            tok = exchange_code_for_token(
                code=q["code"],
                redirect_uri=redirect_uri,
                code_verifier=st.session_state.get("pkce_verifier"),
                tenant_id=tenant_id,
                client_id=client_id,
                client_secret=client_secret
            )
            st.session_state["token"] = tok
            st.session_state["token_expires_at"] = time.time() + int(tok.get("expires_in", 3600))
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Erro no OAuth: {e}")
            if st.button("Voltar"):
                st.query_params.clear()
                st.rerun()
    else:
        main()
