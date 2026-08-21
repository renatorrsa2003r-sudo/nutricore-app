import os
import re
import time
import json
import math
import uuid
import secrets
import hashlib
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Union
from enum import Enum

import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from fastapi import FastAPI, HTTPException, Request, Response, Header, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# ==============================================================================
# 1. CONFIGURAÇÕES GERAIS
# ==============================================================================

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "nutricore2026").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
MERCADO_PAGO_TOKEN = os.getenv("MERCADO_PAGO_ACCESS_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_DATABASE_URL", "").strip()

def get_db_conn():
    if not SUPABASE_URL:
        raise Exception("ERRO FATAL: Variável SUPABASE_DATABASE_URL não configurada no ambiente.")
    return psycopg2.connect(SUPABASE_URL)

def init_db():
    if not SUPABASE_URL:
        print("[AVISO] SUPABASE_DATABASE_URL ausente. Pulo da inicialização do banco.")
        return
        
    try:
        conn = get_db_conn()
        c = conn.cursor()
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                subscription_status TEXT DEFAULT 'trial',
                plan_type TEXT DEFAULT 'free',
                subscription_end TEXT,
                is_pro INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        ''')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            )
        ''')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS user_data (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                profile_json TEXT,
                diet_json TEXT,
                evolution_json TEXT
            )
        ''')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                payment_id TEXT UNIQUE,
                plan_type TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                qr_code TEXT,
                qr_code_base64 TEXT,
                created_at TEXT NOT NULL
            )
        ''')
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                budget_tier TEXT DEFAULT 'economico',
                tmb REAL,
                daily_calories REAL,
                quiz_data_json TEXT,
                created_at TEXT NOT NULL
            )
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS protocols (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                protocol_text TEXT NOT NULL,
                analysis_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        ''')

        # Migrações seguras (Postgres aceita IF NOT EXISTS no ADD COLUMN a partir da versão 11+)
        try:
            c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_pro INTEGER DEFAULT 0;")
            c.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS budget_tier TEXT DEFAULT 'economico';")
            c.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS tmb REAL;")
            c.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS daily_calories REAL;")
            c.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS quiz_data_json TEXT;")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS qr_code TEXT;")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS qr_code_base64 TEXT;")
        except Exception as mig_err:
            pass

        conn.commit()
        conn.close()
        print("[DB] Banco Supabase inicializado com sucesso!")
    except Exception as e:
        print(f"[DB ERRO] Falha ao inicializar o banco Supabase: {e}")

init_db()

# ==============================================================================
# 2. SEGURANÇA E AUTENTICAÇÃO
# ==============================================================================

def hash_password(password: str, salt: Optional[str] = None):
    if not salt:
        salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    ).hex()
    return pwd_hash, salt

def verify_password(password: str, salt: str, stored_hash: str) -> bool:
    pwd_hash, _ = hash_password(password, salt)
    return pwd_hash == stored_hash

def get_user_by_token(token: Optional[str]):
    if not token:
        return None
    token_clean = token.replace("Bearer ", "").strip()
    try:
        conn = get_db_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute('''
            SELECT u.id, u.name, u.email, u.subscription_status, u.plan_type, u.subscription_end, u.is_pro
            FROM sessions s 
            JOIN users u ON s.user_id = u.id 
            WHERE s.token = %s
        ''', (token_clean,))
        row = c.fetchone()
        conn.close()
        if row:
            return {
                "id": row["id"],
                "name": row["name"],
                "email": row["email"],
                "subscription_status": row["subscription_status"],
                "plan_type": row["plan_type"],
                "subscription_end": row["subscription_end"],
                "is_pro": bool(row["is_pro"]) or row["subscription_status"] == 'active'
            }
    except Exception:
        pass
    return None

# ==============================================================================
# 3. MODELOS PYDANTIC
# ==============================================================================

class SexoEnum(str, Enum):
    MASCULINO = "masculino"
    FEMININO = "feminino"

class NivelAtividadeEnum(str, Enum):
    SEDENTARIO = "sedentario"
    LEVE = "leve"
    MODERADO = "moderado"
    INTENSO = "intenso"

class ObjetivoEnum(str, Enum):
    PERDA_PESO = "perda_peso"
    MANUTENCAO = "manutencao"
    HIPERTROFIA = "hipertrofia"

class OrcamentoEnum(str, Enum):
    ECONOMICO = "economico"
    MODERADO = "moderado"
    PREMIUM = "premium"

class RegisterInput(BaseModel):
    name: str = Field(..., min_length=2)
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=6)

class LoginInput(BaseModel):
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=6)

class AuthResponse(BaseModel):
    token: str
    user: dict

class LeadCaptureInput(BaseModel):
    name: str
    email: str
    phone: str
    idade: Optional[int] = 28
    sexo: Optional[str] = "masculino"
    peso_kg: Optional[float] = 78.0
    altura_cm: Optional[float] = 178.0
    peso_alvo_kg: Optional[float] = 70.0
    nivel_atividade: Optional[str] = "moderado"
    objetivo: Optional[str] = "perda_peso"
    orcamento: Optional[str] = "economico"
    obstaculo: Optional[str] = "falta_tempo"
    estilo_culinario: Optional[str] = "caseiro_brasil"

class CreatePixPaymentInput(BaseModel):
    plan_type: Optional[str] = "anual"
    amount: Optional[float] = None
    email: Optional[str] = None
    name: Optional[str] = None

class UserDataSyncInput(BaseModel):
    profile: Optional[dict] = None
    diet: Optional[dict] = None
    evolution: Optional[list] = None

class PerfilUsuarioInput(BaseModel):
    idade: int = Field(28, ge=12, le=120)
    sexo: SexoEnum = SexoEnum.MASCULINO
    peso_kg: float = Field(78.0, gt=30, lt=350)
    altura_cm: float = Field(178.0, gt=90, lt=260)
    nivel_atividade: NivelAtividadeEnum = NivelAtividadeEnum.MODERADO
    objetivo: ObjetivoEnum = ObjetivoEnum.PERDA_PESO
    orcamento: Optional[OrcamentoEnum] = OrcamentoEnum.ECONOMICO
    ritmo_objetivo: Optional[str] = "moderado"
    preferencia: Optional[str] = "onivoro"
    estilo_culinario: Optional[str] = "caseiro_brasil"
    alimentos_favoritos: Optional[str] = ""
    alimentos_evitar: Optional[str] = ""
    intolerancias_saude: Optional[List[str]] = []
    refeicoes_por_dia: int = Field(4, ge=3, le=6)
    dias_plano: int = Field(7, ge=1, le=30)
    gemini_api_key: Optional[str] = None

class Macronutrientes(BaseModel):
    proteinas_g: float
    carboidratos_g: float
    gorduras_g: float
    calorias_totais: float

class RefeicaoIA(BaseModel):
    nome_refeicao: str
    titulo_prato: str
    horario_sugerido: str
    calorias_alvo: float
    proteinas_refeicao_g: float
    carboidratos_refeicao_g: float
    gorduras_refeicao_g: float
    ingredientes: List[str]
    modo_preparo: str
    dica_chef: str

class DiaPlano(BaseModel):
    dia: int
    titulo_dia: str
    refeicoes: List[RefeicaoIA]

class TrocaAlimentoInput(BaseModel):
    refeicao_atual: RefeicaoIA
    motivo_ou_substituto: str = Field(..., min_length=2)
    orcamento: Optional[str] = "economico"
    preferencia: Optional[str] = "onivoro"
    estilo_culinario: Optional[str] = "caseiro_brasil"
    intolerancias_saude: Optional[List[str]] = []
    gemini_api_key: Optional[str] = None

class ConsultaFuncionalInput(BaseModel):
    objetivo_especifico: str = Field(..., min_length=3)
    preferencia: Optional[str] = "onivoro"
    gemini_api_key: Optional[str] = None

class TreinoInput(BaseModel):
    nivel: str = "intermediario"
    foco: str = "hipertrofia"
    equipamento: str = "academia"
    tempo_minutos: int = 45
    gemini_api_key: Optional[str] = None

# ==============================================================================
# 4. MOTOR IA GEMINI (SDK OFICIAL GOOGLE-GENAI - ZERO FALLBACK ESTÁTICO)
# ==============================================================================

MODELOS_ATIVOS = [
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash"
]

def extrair_json_seguro(texto: str) -> dict:
    texto = texto.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-zA-Z]*\n?", "", texto)
        texto = re.sub(r"\n?```$", "", texto)
    return json.loads(texto.strip())

def obter_chave(api_key_param: Optional[str]):
    key = api_key_param or GEMINI_API_KEY
    if not key or key.strip() == "":
        return None
    return key.strip()

def executar_chamada_ia(prompt: str, chave_api: Optional[str] = None) -> dict:
    key = obter_chave(chave_api)
    if not key:
        raise HTTPException(
            status_code=400,
            detail="Chave API do Gemini não configurada (GEMINI_API_KEY)."
        )

    client = genai.Client(api_key=key.strip())
    erros = []

    for modelo in MODELOS_ATIVOS:
        try:
            response = client.models.generate_content(
                model=modelo,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.7
                )
            )
            if response and response.text:
                return extrair_json_seguro(response.text)
        except Exception as e:
            erros.append(f"{modelo}: {str(e)}")
            continue

    raise HTTPException(
        status_code=502,
        detail=f"Erro ao comunicar com a IA do Gemini: {'; '.join(erros)}"
    )

# ==============================================================================
# 5. CÁLCULOS METABÓLICOS
# ==============================================================================

def calcular_metas(p: PerfilUsuarioInput):
    if p.sexo == SexoEnum.MASCULINO:
        tmb = (10 * p.peso_kg) + (6.25 * p.altura_cm) - (5 * p.idade) + 5
    else:
        tmb = (10 * p.peso_kg) + (6.25 * p.altura_cm) - (5 * p.idade) - 161

    fatores = {
        NivelAtividadeEnum.SEDENTARIO: 1.2,
        NivelAtividadeEnum.LEVE: 1.375,
        NivelAtividadeEnum.MODERADO: 1.55,
        NivelAtividadeEnum.INTENSO: 1.725
    }
    tdee = tmb * fatores.get(p.nivel_atividade, 1.55)

    if p.objetivo == ObjetivoEnum.PERDA_PESO:
        deficit = 0.85 if p.ritmo_objetivo == "conservador" else (0.75 if p.ritmo_objetivo == "agressivo" else 0.80)
        meta_calorica = tdee * deficit
    elif p.objetivo == ObjetivoEnum.HIPERTROFIA:
        superavit = 1.08 if p.ritmo_objetivo == "conservador" else (1.20 if p.ritmo_objetivo == "agressivo" else 1.15)
        meta_calorica = tdee * superavit
    else:
        meta_calorica = tdee

    fator_prot = 2.2 if p.objetivo == ObjetivoEnum.HIPERTROFIA else (2.0 if p.objetivo == ObjetivoEnum.PERDA_PESO else 1.8)
    proteinas_g = p.peso_kg * fator_prot
    cal_prot = proteinas_g * 4
    cal_gord = meta_calorica * 0.25
    gorduras_g = cal_gord / 9
    carboidratos_g = max((meta_calorica - (cal_prot + cal_gord)) / 4, 30.0)

    macros = Macronutrientes(
        proteinas_g=round(proteinas_g, 1),
        carboidratos_g=round(carboidratos_g, 1),
        gorduras_g=round(gorduras_g, 1),
        calorias_totais=round(meta_calorica, 0)
    )
    return round(tmb, 1), round(tdee, 1), round(meta_calorica, 1), macros

# ==============================================================================
# 6. ROTAS FASTAPI
# ==============================================================================

app = FastAPI(title="NutriCore Pro Engine Supabase", version="24.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/manifest.json")
def serve_manifest():
    if os.path.exists("manifest.json"):
        return FileResponse("manifest.json")
    return {
        "name": "NutriCore Pro",
        "short_name": "NutriCore",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": "#22c55e"
    }

@app.get("/")
def home():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return HTMLResponse("<h2>NutriCore Pro Engine Online (Conectado ao Supabase).</h2>")

@app.get("/quiz")
def quiz_page():
    if os.path.exists("quiz.html"):
        return FileResponse("quiz.html")
    return HTMLResponse("<h2>Quiz NutriCore Pro Online.</h2>")

@app.get("/health")
def health():
    return {
        "status": "online",
        "database": "Supabase PostgreSQL" if SUPABASE_URL else "NENHUM BANCO CONFIGURADO",
        "gemini_configured": bool(GEMINI_API_KEY),
        "mercadopago_configured": bool(MERCADO_PAGO_TOKEN),
        "timestamp": datetime.utcnow().isoformat()
    }

# --- CAPTURA DE LEADS (QUIZ) ---

@app.post("/api/v1/lead/capture")
@app.post("/api/lead/capture")
def capturar_lead_quiz(lead: LeadCaptureInput):
    if str(lead.sexo).lower() in ["masculino", "homem", "m"]:
        tmb = (10 * lead.peso_kg) + (6.25 * lead.altura_cm) - (5 * lead.idade) + 5
    else:
        tmb = (10 * lead.peso_kg) + (6.25 * lead.altura_cm) - (5 * lead.idade) - 161

    fatores = {"sedentario": 1.2, "leve": 1.375, "moderado": 1.55, "intenso": 1.725}
    tdee = tmb * fatores.get(str(lead.nivel_atividade).lower(), 1.55)

    if "perda" in str(lead.objetivo).lower() or "emagrecer" in str(lead.objetivo).lower():
        meta_calorica = tdee * 0.80
        dif_peso = max(0.0, lead.peso_kg - lead.peso_alvo_kg)
        semanas_estimadas = max(2, int(dif_peso / 0.6))
    elif "hipertrofia" in str(lead.objetivo).lower() or "ganho" in str(lead.objetivo).lower():
        meta_calorica = tdee * 1.15
        dif_peso = max(0.0, lead.peso_alvo_kg - lead.peso_kg)
        semanas_estimadas = max(4, int(dif_peso / 0.4))
    else:
        meta_calorica = tdee
        semanas_estimadas = 4

    imc = lead.peso_kg / ((lead.altura_cm / 100) ** 2)
    agora = datetime.utcnow().isoformat()
    orcamento_sel = lead.orcamento or "economico"

    clean_phone = re.sub(r'\D', '', str(lead.phone))
    if not clean_phone.startswith('55'):
        clean_phone = '55' + clean_phone
    msg = f"Olá {lead.name}! Seu diagnóstico no NutriCore Pro está pronto."
    wpp_url = f"[https://wa.me/](https://wa.me/){clean_phone}?text={urllib.parse.quote(msg)}"

    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO leads (name, email, phone, budget_tier, tmb, daily_calories, quiz_data_json, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (lead.name.strip(), lead.email.lower().strip(), lead.phone.strip(), orcamento_sel, round(tmb, 1), round(meta_calorica, 1), json.dumps(lead.dict()), agora)
    )
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "tmb": round(tmb, 0),
        "tdee": round(tdee, 0),
        "daily_calories": round(meta_calorica, 0),
        "meta_calorica": round(meta_calorica, 0),
        "imc": round(imc, 1),
        "budget_tier": orcamento_sel,
        "estimated_weeks": semanas_estimadas,
        "semanas_estimadas": semanas_estimadas,
        "recovery_whatsapp_url": wpp_url
    }

# --- AUTENTICAÇÃO ---

@app.post("/api/v1/auth/register", response_model=AuthResponse)
@app.post("/api/auth/register")
def cadastrar_usuario(dados: RegisterInput):
    email_clean = dados.email.lower().strip()
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email = %s", (email_clean,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Este e-mail já está cadastrado. Faça login.")

    pwd_hash, salt = hash_password(dados.password)
    agora = datetime.utcnow().isoformat()

    c.execute(
        "INSERT INTO users (name, email, password_hash, salt, subscription_status, plan_type, is_pro, created_at) VALUES (%s, %s, %s, %s, 'trial', 'free', 0, %s) RETURNING id",
        (dados.name.strip(), email_clean, pwd_hash, salt, agora)
    )
    user_id = c.fetchone()[0]
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (%s, %s, %s)", (token, user_id, agora))
    conn.commit()
    conn.close()

    return AuthResponse(
        token=token,
        user={"id": user_id, "name": dados.name.strip(), "email": email_clean, "subscription_status": "trial", "plan_type": "free", "is_pro": False}
    )

@app.post("/api/v1/auth/login", response_model=AuthResponse)
@app.post("/api/auth/login")
def login_usuario(dados: LoginInput):
    email_clean = dados.email.lower().strip()
    conn = get_db_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT id, name, email, password_hash, salt, subscription_status, plan_type, subscription_end, is_pro FROM users WHERE email = %s", (email_clean,))
    user = c.fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=400, detail="E-mail ou senha incorretos.")

    if not verify_password(dados.password, user["salt"], user["password_hash"]):
        conn.close()
        raise HTTPException(status_code=400, detail="E-mail ou senha incorretos.")

    token = secrets.token_urlsafe(32)
    agora = datetime.utcnow().isoformat()
    c.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (%s, %s, %s)", (token, user["id"], agora))
    conn.commit()
    conn.close()

    return AuthResponse(
        token=token,
        user={"id": user["id"], "name": user["name"], "email": user["email"], "subscription_status": user["subscription_status"], "plan_type": user["plan_type"], "subscription_end": user["subscription_end"], "is_pro": bool(user["is_pro"]) or user["subscription_status"] == 'active'}
    )

@app.get("/api/v1/auth/me")
@app.get("/api/auth/me")
def obter_usuario_logado(authorization: Optional[str] = Header(None)):
    user = get_user_by_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Sessão expirada. Faça login novamente.")
    return user

@app.post("/api/v1/auth/logout")
@app.post("/api/auth/logout")
def logout_usuario(authorization: Optional[str] = Header(None)):
    token = authorization.replace("Bearer ", "").strip() if authorization else None
    if token:
        try:
            conn = get_db_conn()
            c = conn.cursor()
            c.execute("DELETE FROM sessions WHERE token = %s", (token,))
            conn.commit()
            conn.close()
        except Exception:
            pass
    return {"message": "Desconectado com sucesso."}

# --- PAGAMENTOS PIX ---

@app.post("/api/v1/payment/create-pix")
@app.post("/api/v1/pix/create")
@app.post("/api/payment/create-pix")
@app.post("/api/pix/create")
@app.post("/create-pix")
def criar_pagamento_pix(dados: CreatePixPaymentInput, authorization: Optional[str] = Header(None)):
    user = get_user_by_token(authorization)
    user_id = user["id"] if user else None
    user_email = (user["email"] if user else dados.email) or "cliente@nutricore.app"
    user_name = (user["name"] if user else dados.name) or "Cliente NutriCore"

    valor = float(dados.amount or (29.90 if dados.plan_type == "mensal" else 149.90))
    descricao = f"NutriCore Pro - Assinatura {str(dados.plan_type).capitalize()}"

    mp_access_token = os.getenv("MERCADO_PAGO_ACCESS_TOKEN")
    payment_id = None
    qr_code = None
    qr_code_base64 = None

    if mp_access_token and len(mp_access_token) > 15:
        try:
            headers = {
                "Authorization": f"Bearer {mp_access_token}",
                "Content-Type": "application/json",
                "X-Idempotency-Key": str(uuid.uuid4())
            }
            body = {
                "transaction_amount": valor,
                "description": descricao,
                "payment_method_id": "pix",
                "payer": {
                    "email": user_email,
                    "first_name": user_name.split()[0] if user_name else "Cliente"
                }
            }
            resp = requests.post("[https://api.mercadopago.com/v1/payments](https://api.mercadopago.com/v1/payments)", headers=headers, json=body, timeout=8)
            if resp.status_code in [200, 201]:
                data_mp = resp.json()
                payment_id = str(data_mp.get("id"))
                poi = data_mp.get("point_of_interaction", {}).get("transaction_data", {})
                qr_code = poi.get("qr_code")
                qr_code_base64 = poi.get("qr_code_base64")
        except Exception:
            pass

    if not payment_id:
        payment_id = f"demo_{secrets.token_hex(8)}"
        qr_code = f"00020126580014br.gov.bcb.pix0136nutricore-pix-{payment_id}520400005303986540{valor:.2f}5802BR5925NUTRICORE PRO SAAS6009SAO PAULO62070503***6304"
        qr_code_base64 = None

    qr_img_url = f"[https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=](https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=){urllib.parse.quote(qr_code)}"
    agora = datetime.utcnow().isoformat()

    conn = get_db_conn()
    c = conn.cursor()
    c.execute('''
        INSERT INTO orders (user_id, payment_id, plan_type, amount, status, qr_code, qr_code_base64, created_at)
        VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s)
    ''', (user_id, payment_id, dados.plan_type or 'anual', valor, qr_code, qr_code_base64, agora))
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "payment_id": payment_id,
        "id": payment_id,
        "amount": valor,
        "plan_type": dados.plan_type,
        "qr_code": qr_code,
        "qr_code_base64": qr_code_base64,
        "qr_code_url": qr_img_url,
        "ticket_url": qr_img_url,
        "copia_e_cola": qr_code,
        "pix_code": qr_code
    }

@app.get("/api/v1/payment/check-status/{payment_id}")
@app.get("/api/v1/pix/status/{payment_id}")
@app.get("/api/payment/status/{payment_id}")
def verificar_status_pagamento(payment_id: str):
    conn = get_db_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT status, plan_type, user_id FROM orders WHERE payment_id = %s", (payment_id,))
    order = c.fetchone()

    if not order:
        conn.close()
        return {"status": "pending", "is_approved": False}

    status_val, plan_type, order_user_id = order["status"], order["plan_type"], order["user_id"]

    mp_access_token = os.getenv("MERCADO_PAGO_ACCESS_TOKEN")
    if mp_access_token and not payment_id.startswith("demo_") and status_val == "pending":
        try:
            headers = {"Authorization": f"Bearer {mp_access_token}"}
            resp = requests.get(f"[https://api.mercadopago.com/v1/payments/](https://api.mercadopago.com/v1/payments/){payment_id}", headers=headers, timeout=6)
            if resp.status_code == 200:
                status_mp = resp.json().get("status")
                if status_mp == "approved":
                    status_val = "approved"
                    dias_add = 30 if plan_type == "mensal" else 365
                    sub_end = (datetime.utcnow() + timedelta(days=dias_add)).isoformat()
                    c.execute("UPDATE orders SET status = 'approved' WHERE payment_id = %s", (payment_id,))
                    if order_user_id:
                        c.execute("UPDATE users SET subscription_status = 'active', plan_type = %s, is_pro = 1, subscription_end = %s WHERE id = %s", (plan_type, sub_end, order_user_id))
                    conn.commit()
        except Exception:
            pass

    conn.close()
    return {"status": status_val, "is_approved": status_val == "approved"}

# --- SIMULAÇÃO DE TESTE / MODO PRO ---

@app.api_route("/api/v1/payment/simulate-approval/{payment_id}", methods=["GET", "POST"])
@app.api_route("/api/v1/payment/simulate-approve/{payment_id}", methods=["GET", "POST"])
@app.api_route("/api/v1/pix/simulate-approve/{payment_id}", methods=["GET", "POST"])
@app.api_route("/api/payment/simulate-approve/{payment_id}", methods=["GET", "POST"])
@app.api_route("/api/payment/simulate-approval/{payment_id}", methods=["GET", "POST"])
@app.api_route("/api/simulate-approve/{payment_id}", methods=["GET", "POST"])
async def simular_aprovacao_com_id(payment_id: str, authorization: Optional[str] = Header(None)):
    user = get_user_by_token(authorization)
    conn = get_db_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    
    sub_end = (datetime.utcnow() + timedelta(days=365)).isoformat()
    c.execute("UPDATE orders SET status = 'approved' WHERE payment_id = %s", (payment_id,))
    c.execute("SELECT user_id, plan_type FROM orders WHERE payment_id = %s", (payment_id,))
    order = c.fetchone()
    
    if order and order["user_id"]:
        c.execute("UPDATE users SET subscription_status = 'active', plan_type = %s, is_pro = 1, subscription_end = %s WHERE id = %s", (order["plan_type"] or 'anual', sub_end, order["user_id"]))
    elif user:
        c.execute("UPDATE users SET subscription_status = 'active', plan_type = 'anual', is_pro = 1, subscription_end = %s WHERE id = %s", (sub_end, user["id"]))
    else:
        c.execute("UPDATE users SET subscription_status = 'active', plan_type = 'anual', is_pro = 1, subscription_end = %s", (sub_end,))

    conn.commit()
    conn.close()

    return {
        "status": "success",
        "approved": True,
        "is_approved": True,
        "is_pro": True,
        "subscription_status": "active",
        "plan_type": "anual",
        "message": f"Pagamento {payment_id} aprovado com sucesso! Acesso PRO liberado."
    }

@app.api_route("/api/v1/payment/simulate", methods=["GET", "POST"])
@app.api_route("/api/v1/payment/simulate-approval", methods=["GET", "POST"])
@app.api_route("/api/v1/payment/simulate-approve", methods=["GET", "POST"])
@app.api_route("/api/v1/pix/simulate", methods=["GET", "POST"])
@app.api_route("/api/payment/simulate", methods=["GET", "POST"])
@app.api_route("/api/payment/simulate-approval", methods=["GET", "POST"])
@app.api_route("/api/simulate-payment", methods=["GET", "POST"])
@app.api_route("/api/simulate-pro", methods=["GET", "POST"])
@app.api_route("/api/simulate-test", methods=["GET", "POST"])
@app.api_route("/api/test-mode", methods=["GET", "POST"])
@app.api_route("/simulate-payment", methods=["GET", "POST"])
@app.api_route("/simulate-pro", methods=["GET", "POST"])
async def simular_aprovacao_sem_id(request: Request, authorization: Optional[str] = Header(None)):
    user = get_user_by_token(authorization)
    
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    
    payment_id = body.get("payment_id") or request.query_params.get("payment_id")
    email = body.get("email") or request.query_params.get("email")

    conn = get_db_conn()
    c = conn.cursor()
    sub_end = (datetime.utcnow() + timedelta(days=365)).isoformat()

    if payment_id:
        c.execute("UPDATE orders SET status = 'approved' WHERE payment_id = %s", (payment_id,))
    else:
        c.execute("UPDATE orders SET status = 'approved' WHERE status = 'pending'")

    if user:
        c.execute("UPDATE users SET subscription_status = 'active', plan_type = 'anual', is_pro = 1, subscription_end = %s WHERE id = %s", (sub_end, user["id"]))
    elif email:
        c.execute("UPDATE users SET subscription_status = 'active', plan_type = 'anual', is_pro = 1, subscription_end = %s WHERE email = %s", (sub_end, email.lower().strip()))
    else:
        c.execute("UPDATE users SET subscription_status = 'active', plan_type = 'anual', is_pro = 1, subscription_end = %s", (sub_end,))

    conn.commit()
    conn.close()

    return {
        "status": "success",
        "approved": True,
        "is_approved": True,
        "is_pro": True,
        "subscription_status": "active",
        "plan_type": "anual",
        "message": "Simulação de teste concluída com sucesso! Acesso PRO liberado."
    }

# --- SINCRONIZAÇÃO NUVEM ---

@app.get("/api/v1/user/sync-data")
def obter_dados_usuario(authorization: Optional[str] = Header(None)):
    user = get_user_by_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Sessão expirada.")
    
    conn = get_db_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT profile_json, diet_json, evolution_json FROM user_data WHERE user_id = %s", (user["id"],))
    row = c.fetchone()
    conn.close()

    if not row:
        return {"profile": None, "diet": None, "evolution": None}
    
    return {
        "profile": json.loads(row["profile_json"]) if row["profile_json"] else None,
        "diet": json.loads(row["diet_json"]) if row["diet_json"] else None,
        "evolution": json.loads(row["evolution_json"]) if row["evolution_json"] else None
    }

@app.post("/api/v1/user/sync-data")
def salvar_dados_usuario(dados: UserDataSyncInput, authorization: Optional[str] = Header(None)):
    user = get_user_by_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Sessão expirada.")

    conn = get_db_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT profile_json, diet_json, evolution_json FROM user_data WHERE user_id = %s", (user["id"],))
    row = c.fetchone()

    p_json = json.dumps(dados.profile) if dados.profile is not None else (row["profile_json"] if row else None)
    d_json = json.dumps(dados.diet) if dados.diet is not None else (row["diet_json"] if row else None)
    e_json = json.dumps(dados.evolution) if dados.evolution is not None else (row["evolution_json"] if row else None)

    c.execute('''
        INSERT INTO user_data (user_id, profile_json, diet_json, evolution_json)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT(user_id) DO UPDATE SET
            profile_json = EXCLUDED.profile_json,
            diet_json = EXCLUDED.diet_json,
            evolution_json = EXCLUDED.evolution_json
    ''', (user["id"], p_json, d_json, e_json))

    conn.commit()
    conn.close()
    return {"status": "ok"}

# --- GERAÇÃO DE DIETA COM IA PURA (SEM FALLBACK) ---

@app.post("/api/v1/diet/generate")
@app.post("/api/v1/plan/generate")
@app.post("/api/plan/generate")
@app.post("/api/generate-plan")
async def criar_plano(request: Request):
    try:
        raw_body = await request.json()
    except Exception:
        raw_body = {}

    idade = int(raw_body.get("idade") or raw_body.get("age") or 28)
    sexo = str(raw_body.get("sexo") or raw_body.get("gender") or "masculino").lower()
    peso_kg = float(raw_body.get("peso_kg") or raw_body.get("peso") or raw_body.get("weight") or 78.0)
    altura_cm = float(raw_body.get("altura_cm") or raw_body.get("altura") or raw_body.get("height") or 178.0)
    atividade = str(raw_body.get("nivel_atividade") or raw_body.get("activity_level") or "moderado")
    objetivo = str(raw_body.get("objetivo") or raw_body.get("goal") or "perda_peso")
    orcamento = str(raw_body.get("orcamento") or raw_body.get("budget_tier") or "economico").lower()
    dias_plano = int(raw_body.get("dias_plano") or raw_body.get("days") or 1)
    refeicoes_por_dia = int(raw_body.get("refeicoes_por_dia") or raw_body.get("meals_count") or 4)
    estilo_culinario = str(raw_body.get("estilo_culinario") or raw_body.get("diet_style") or "caseiro_brasil")
    preferencia = str(raw_body.get("preferencia") or "onivoro")
    restricoes = raw_body.get("intolerancias_saude") or raw_body.get("restrictions") or []
    favs = raw_body.get("alimentos_favoritos") or ""
    evitar = raw_body.get("alimentos_evitar") or ""

    orcamento_enum = OrcamentoEnum.PREMIUM if "prem" in orcamento or "gourmet" in orcamento else (
        OrcamentoEnum.MODERADO if "mod" in orcamento or "equil" in orcamento else OrcamentoEnum.ECONOMICO
    )

    perfil = PerfilUsuarioInput(
        idade=idade,
        sexo=SexoEnum.FEMININO if sexo in ["feminino", "f", "female"] else SexoEnum.MASCULINO,
        peso_kg=peso_kg,
        altura_cm=altura_cm,
        nivel_atividade=NivelAtividadeEnum.SEDENTARIO if "sedent" in atividade else (NivelAtividadeEnum.INTENSO if "inten" in atividade else NivelAtividadeEnum.MODERADO),
        objetivo=ObjetivoEnum.HIPERTROFIA if "hiper" in objetivo or "ganho" in objetivo else (ObjetivoEnum.MANUTENCAO if "manut" in objetivo else ObjetivoEnum.PERDA_PESO),
        orcamento=orcamento_enum,
        refeicoes_por_dia=max(3, min(6, refeicoes_por_dia)),
        dias_plano=max(1, min(30, dias_plano)),
        gemini_api_key=raw_body.get("gemini_api_key")
    )

    tmb, tdee, meta_calorica, macros = calcular_metas(perfil)
    api_key = obter_chave(perfil.gemini_api_key)

    diretrizes_orcamento = {
        "economico": (
            "DIRETRIZ FINANCEIRA RIGOROSA: ORÇAMENTO ECONÔMICO (R$ 12 a R$ 18/dia). "
            "Pesquise e selecione apenas alimentos de altíssimo rendimento calórico/proteico: ovos, peito/coxa de frango, moela, fígado, sardinha fresca ou em lata. "
            "Carboidratos: arroz, feijão, aveia, banana, cuscuz, mandioca, batata inglesa. Vegetais baratos: repolho, abóbora, cenoura, chuchu. "
            "PROIBIDO prescrever salmão, queijos finos ou suplementos importados."
        ),
        "moderado": (
            "DIRETRIZ FINANCEIRA: ORÇAMENTO EQUILIBRADO (R$ 22 a R$ 35/dia). "
            "Proteínas: patinho moído, filé de tilápia, peito de frango, queijo minas frescal, iogurte natural desnatado. "
            "Carboidratos e gorduras: arroz integral, batata doce, azeite de oliva extra virgem, chia e frutas da estação."
        ),
        "premium": (
            "DIRETRIZ FINANCEIRA: ORÇAMENTO GOURMET / LIVRE (R$ 45+/dia). "
            "Priorize sofisticação e máxima densidade de micronutrientes: salmão fresco, filé mignon, camarão, queijo de cabra, iogurte grego artesanal, quinoa real, aspargos, mirtilos e castanhas nobres."
        )
    }

    instrucao_orcamento = diretrizes_orcamento.get(perfil.orcamento.value, diretrizes_orcamento["economico"])

    prompt = f"""
    Você é um nutricionista clínico de elite e pesquisador metabólico.
    Elabore em TEMPO REAL um plano alimentar original, detalhado e cientificamente balanceado para exatamente {perfil.dias_plano} dia(s).

    DADOS DO PACIENTE:
    - Sexo: {perfil.sexo.value}, Idade: {perfil.idade} anos, Peso: {perfil.peso_kg}kg, Altura: {perfil.altura_cm}cm
    - Nível de Atividade: {perfil.nivel_atividade.value}, Objetivo: {perfil.objetivo.value}
    - METAS: Calorias: {meta_calorica} kcal/dia | Proteínas: {macros.proteinas_g}g | Carbos: {macros.carboidratos_g}g | Gorduras: {macros.gorduras_g}g
    - Quantidade de Refeições por Dia: exatamente {perfil.refeicoes_por_dia}
    - {instrucao_orcamento}
    - Preferência Alimentar: {preferencia} | Culinária: {estilo_culinario}
    - Alimentos Favoritos: {favs if favs else 'Sem restrição'}
    - Alimentos a Evitar: {evitar if evitar else 'Nenhum'}
    - Restrições/Condições de Saúde: {', '.join(restricoes) if restricoes else 'Nenhuma'}

    REGRAS DE FORMATAÇÃO:
    - Retorne OBRIGATORIAMENTE um JSON puro válido.
    - Crie exatamente {perfil.dias_plano} objeto(s) no array "dias".
    - Cada dia deve conter exatamente {perfil.refeicoes_por_dia} refeições personalizadas com modo de preparo e dicas úteis.

    ESTRUTURA JSON EXATA:
    {{
      "dias": [
        {{
          "dia": 1,
          "titulo_dia": "Dia 1 - Estratégia de Adaptação Metabólica",
          "refeicoes": [
            {{
              "nome_refeicao": "Café da Manhã",
              "titulo_prato": "Nome do Prato",
              "horario_sugerido": "07:30",
              "calorias_alvo": {round(meta_calorica * 0.25)},
              "proteinas_refeicao_g": {round(macros.proteinas_g * 0.25)},
              "carboidratos_refeicao_g": {round(macros.carboidratos_g * 0.25)},
              "gorduras_refeicao_g": {round(macros.gorduras_g * 0.25)},
              "ingredientes": ["Quantidade e ingrediente 1", "Quantidade e ingrediente 2"],
              "modo_preparo": "Instruções claras de preparo culinário.",
              "dica_chef": "Dica funcional e metabólica."
            }}
          ]
        }}
      ]
    }}
    """

    resultado_json = executar_chamada_ia(prompt, api_key)

    if isinstance(resultado_json, list):
        lista_dias_raw = resultado_json
    elif isinstance(resultado_json, dict) and "dias" in resultado_json:
        lista_dias_raw = resultado_json["dias"]
    else:
        lista_dias_raw = [{"dia": 1, "titulo_dia": f"Dia 1 - Plano {perfil.orcamento.value.capitalize()}", "refeicoes": resultado_json.get("refeicoes", [])}]

    dias_objs = []
    for item in lista_dias_raw:
        if "refeicoes" in item:
            refeicoes = [RefeicaoIA(**r) for r in item["refeicoes"]]
            dias_objs.append(DiaPlano(dia=item.get("dia", len(dias_objs)+1), titulo_dia=item.get("titulo_dia", f"Dia {len(dias_objs)+1}"), refeicoes=refeicoes))

    primeiro_dia_refeicoes = dias_objs[0].refeicoes if dias_objs else []

    return {
        "status": "success",
        "gerado_por": "Gemini IA (Tempo Real)",
        "tmb": tmb,
        "tdee": tdee,
        "meta_calorica": meta_calorica,
        "calorias_totais": meta_calorica,
        "budget_tier": perfil.orcamento.value,
        "macros": macros.dict(),
        "dias_total": len(dias_objs),
        "dias": [d.dict() for d in dias_objs],
        "refeicoes": [r.dict() for r in primeiro_dia_refeicoes],
        "meals": [r.dict() for r in primeiro_dia_refeicoes],
        "cardapio": [r.dict() for r in primeiro_dia_refeicoes]
    }

# --- ANALISAR PROTOCOLO COM IA PURA (SEM FALLBACK) ---

@app.post("/api/v1/protocol/analyze")
@app.post("/api/v1/protocolo/analisar")
@app.post("/api/protocol/analyze")
@app.post("/api/analisar-protocolo")
async def analisar_protocolo(request: Request, authorization: Optional[str] = Header(None)):
    try:
        body = await request.json()
    except Exception:
        body = {}

    user = get_user_by_token(authorization)
    user_id = user["id"] if user else None

    protocol_text = body.get("protocol_text") or body.get("protocolo") or body.get("text") or body.get("dieta")
    if not protocol_text or not protocol_text.strip():
        raise HTTPException(status_code=400, detail="Por favor, forneça o texto do protocolo a ser analisado.")

    goal = body.get("goal") or body.get("objetivo") or "emagrecimento e definição"
    weight = float(body.get("weight") or body.get("peso") or 75.0)

    prompt = f"""
    Você é um nutricionista clínico de alta precisão e avaliador metabólico.
    Execute uma auditoria crítica e aprofundada em tempo real sobre o seguinte protocolo alimentar:
    \"{protocol_text}\"
    
    Perfil do Paciente: Peso {weight}kg | Objetivo: {goal}.

    Retorne OBRIGATORIAMENTE um JSON puro seguindo este schema:
    {{
      "status_avaliacao": "Parecer Clínico Curto (ex: Protocolo Otimizado / Déficit Excessivo / Ajuste Necessário)",
      "pontuacao_geral": 92,
      "pontuacao": 92,
      "score": 92,
      "resumo_executivo": "Parecer clínico completo e personalizado avaliando calorias, timing de nutrientes e consistência fisiológica.",
      "balanco_calorico_estimado": "Estimativa calórica e balanço energético em relação ao gasto metabólico.",
      "distribuicao_macros": {{
        "proteinas": "Avaliação detalhada da ingestão proteica por kg de peso corporal.",
        "carboidratos": "Avaliação do tipo e distribuição de carboidratos ao longo do dia.",
        "gorduras": "Avaliação da qualidade dos ácidos graxos essenciais."
      }},
      "pontos_fortes": [
        "Ponto forte 1 identificado no protocolo",
        "Ponto forte 2 identificado no protocolo"
      ],
      "pontos_de_atencao": [
        "Risco ou falha 1 identificado",
        "Risco ou falha 2 identificado"
      ],
      "recomendacoes_otimizacao": [
        "Ajuste prático 1 recomendado",
        "Ajuste prático 2 recomendado"
      ]
    }}
    """

    res = executar_chamada_ia(prompt)
    res["status"] = "success"
    res["gerado_por"] = "Gemini IA (Tempo Real)"

    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO protocols (user_id, protocol_text, analysis_json, created_at) VALUES (%s, %s, %s, %s)",
        (user_id, protocol_text, json.dumps(res), datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

    return res

# --- TROCA DE ALIMENTOS COM IA PURA ---

@app.post("/api/v1/diet/swap-food", response_model=RefeicaoIA)
@app.post("/api/v1/food/swap")
def trocar_alimento_refeicao(dados: TrocaAlimentoInput):
    api_key = obter_chave(dados.gemini_api_key)
    prompt = f"""
    Você é um nutricionista clínico. Substitua em tempo real um alimento ou prato mantendo rigorosa equivalência nutricional.

    REFEIÇÃO ORIGINAL:
    - Nome: {dados.refeicao_atual.nome_refeicao} | Prato: {dados.refeicao_atual.titulo_prato}
    - Calorias: ~{dados.refeicao_atual.calorias_alvo} kcal | P: {dados.refeicao_atual.proteinas_refeicao_g}g | C: {dados.refeicao_atual.carboidratos_refeicao_g}g | G: {dados.refeicao_atual.gorduras_refeicao_g}g
    - Ingredientes Atuais: {', '.join(dados.refeicao_atual.ingredientes)}

    PEDIDO DE SUBSTITUIÇÃO DO PACIENTE: \"{dados.motivo_ou_substituto}\"
    Faixa Orçamentária: {dados.orcamento} | Padrão: {dados.preferencia} | Culinária: {dados.estilo_culinario}
    Restrições de Saúde: {', '.join(dados.intolerancias_saude) if dados.intolerancias_saude else 'Nenhuma'}

    Retorne OBRIGATORIAMENTE um JSON puro com o prato substituto equivalente:
    {{
      "nome_refeicao": "{dados.refeicao_atual.nome_refeicao}",
      "titulo_prato": "Novo Título do Prato",
      "horario_sugerido": "{dados.refeicao_atual.horario_sugerido}",
      "calorias_alvo": {dados.refeicao_atual.calorias_alvo},
      "proteinas_refeicao_g": {dados.refeicao_atual.proteinas_refeicao_g},
      "carboidratos_refeicao_g": {dados.refeicao_atual.carboidratos_refeicao_g},
      "gorduras_refeicao_g": {dados.refeicao_atual.gorduras_refeicao_g},
      "ingredientes": ["Ingrediente 1 com porção", "Ingrediente 2 com porção"],
      "modo_preparo": "Instruções práticas de preparo.",
      "dica_chef": "Por que esta substituição atende a meta com perfeição."
    }}
    """
    res = executar_chamada_ia(prompt, api_key)
    return RefeicaoIA(**res)

# --- CONSULTA FUNCIONAL COM IA PURA ---

@app.api_route("/api/v1/nutrition/consult", methods=["GET", "POST"])
@app.api_route("/api/v1/energy/boost", methods=["GET", "POST"])
@app.api_route("/api/energy/tips", methods=["GET", "POST"])
def consultar_nutricao(dados: Optional[ConsultaFuncionalInput] = None):
    obj = dados.objetivo_especifico if dados else "Aumentar a energia mitocondrial e disposição metabólica diária"
    pref = dados.preferencia if dados else "onivoro"
    api_key = obter_chave(dados.gemini_api_key if dados else None)

    prompt = f"""
    Você é um fitoterapeuta e nutricionista funcional.
    Gere um protocolo terapêutico baseado em evidências científicas para a seguinte queixa: \"{obj}\".
    Padrão alimentar: {pref}.

    Retorne OBRIGATORIAMENTE um JSON puro no schema:
    {{
      "titulo_estrategia": "Título da Estratégia Terapêutica",
      "explicacao_fisiologica": "Explicação científica clara sobre como estes compostos atuam nas vias metabólicas.",
      "alimentos_chave": [
        {{"alimento": "Nome do Alimento/Erva", "porcao_sugerida": "Dose diária", "por_que_funciona": "Mecanismo biológico de ação", "como_consumir": "Melhor momento do dia"}},
        {{"alimento": "Nome do Alimento 2", "porcao_sugerida": "Dose diária", "por_que_funciona": "Mecanismo biológico de ação", "como_consumir": "Melhor momento do dia"}}
      ],
      "alimentos_evitar": ["Alimento pró-inflamatório 1", "Alimento prejudicial 2"],
      "receita_rapida": {{
        "titulo": "Nome do Shot ou Infusão Terapêutica",
        "tempo_preparo": "3 min",
        "ingredientes": ["Ingrediente 1", "Ingrediente 2"],
        "modo_preparo": "Instruções de preparo.",
        "quando_tomar": "Horário ideal de ingestão."
      }}
    }}
    """
    return executar_chamada_ia(prompt, api_key)

# --- PRESCRIÇÃO DE TREINOS COM IA PURA ---

@app.post("/api/v1/workout/generate")
def criar_treino(dados: TreinoInput):
    api_key = obter_chave(dados.gemini_api_key)
    prompt = f"""
    Você é um treinador de força e fisiologista do exercício.
    Crie uma sessão de treino personalizada em tempo real para:
    - Nível: {dados.nivel} | Foco: {dados.foco} | Equipamento: {dados.equipamento} | Duração: {dados.tempo_minutos} minutos.

    Retorne OBRIGATORIAMENTE um JSON puro:
    {{
      "titulo": "Nome da Sessão de Treino",
      "foco_principal": "{dados.foco}",
      "aquecimento": [
        {{"nome": "Exercício de Aquecimento/Mobilidade", "series": "2", "repeticoes": "45s", "descanso": "30s", "dica_tecnica": "Instrução biomecânica"}}
      ],
      "treino_principal": [
        {{"nome": "Exercício 1", "series": "4", "repeticoes": "10-12", "descanso": "60s", "dica_tecnica": "Instrução biomecânica"}},
        {{"nome": "Exercício 2", "series": "3", "repeticoes": "12", "descanso": "60s", "dica_tecnica": "Instrução biomecânica"}}
      ],
      "finalizacao": [
        {{"nome": "Core ou Alongamento", "series": "3", "repeticoes": "45s", "descanso": "30s", "dica_tecnica": "Instrução"}}
      ]
    }}
    """
    return executar_chamada_ia(prompt, api_key)

# --- SCANNER DE PRATOS COM IA ---

@app.post("/api/v1/ai/scan-plate")
@app.post("/api/scan-plate")
def scan_plate():
    prompt = """
    Atue como um sistema de visão computacional nutricional.
    Identifique um prato de refeição saudável comum e estime os macronutrientes.
    Retorne OBRIGATORIAMENTE um JSON puro:
    {
      "status": "success",
      "prato_identificado": "Descrição detalhada dos alimentos identificados",
      "calorias_estimadas": 580,
      "macros": {"proteina_g": 42, "carbo_g": 65, "gordura_g": 14},
      "confianca_ia": "96%",
      "recomendacao": "Parecer sobre equilíbrio e saciedade."
    }
    """
    return executar_chamada_ia(prompt)

# ==============================================================================
# 7. PAINEL ADMINISTRATIVO (/admin) & EXPORTAÇÃO CSV
# ==============================================================================

@app.get("/admin/export/leads.csv")
def export_leads_csv(senha: str = ""):
    if senha.strip() != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Senha incorreta.")
    conn = get_db_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    try:
        c.execute("SELECT * FROM leads ORDER BY id DESC")
        leads = c.fetchall()
    except Exception:
        leads = []
    conn.close()

    csv_content = "ID;Nome;Email;WhatsApp;Faixa Orcamento;Calorias Meta;Data Criacao\n"
    for l in leads:
        csv_content += f"{l['id']};{l['name']};{l['email']};{l['phone']};{l['budget_tier']};{l['daily_calories']};{l['created_at']}\n"

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads_nutricore.csv"}
    )

@app.api_route("/admin", methods=["GET", "POST"], response_class=HTMLResponse)
async def admin_portal(request: Request, senha: Optional[str] = None):
    body_senha = ""
    if request.method == "POST":
        try:
            form = await request.form()
            body_senha = form.get("senha", "")
        except Exception:
            pass

    param_senha = senha or request.query_params.get("senha") or body_senha or ""
    param_senha = param_senha.strip()

    if param_senha != ADMIN_PASSWORD:
        return f"""
        <!DOCTYPE html>
        <html lang="pt-BR">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Admin - NutriCore Pro</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0b0f19; color: #f8fafc; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
                .card {{ background: #111827; padding: 2.5rem; border-radius: 1rem; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5); width: 100%; max-width: 360px; border: 1px solid #1f2937; text-align: center; }}
                h2 {{ margin-top: 0; color: #10b981; }}
                input {{ width: 100%; padding: 0.85rem; border-radius: 0.5rem; border: 1px solid #374151; background: #030712; color: white; margin: 1.2rem 0; box-sizing: border-box; font-size: 1rem; }}
                button {{ width: 100%; padding: 0.85rem; border-radius: 0.5rem; border: none; background: #10b981; color: white; font-weight: bold; cursor: pointer; font-size: 1rem; }}
            </style>
        </head>
        <body>
            <div class="card">
                <h2>⚡ Admin NutriCore</h2>
                <p style="color: #9ca3af; font-size: 0.9rem;">Insira a sua senha de administrador.</p>
                <form method="get" action="/admin">
                    <input type="password" name="senha" placeholder="Senha Administrador" required autofocus>
                    <button type="submit">Entrar no Dashboard</button>
                </form>
            </div>
        </body>
        </html>
        """

    conn = get_db_conn()
    c = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        c.execute("SELECT * FROM leads ORDER BY id DESC")
        leads = c.fetchall()
    except Exception:
        leads = []

    try:
        c.execute("SELECT COUNT(*) as count FROM users")
        total_users = c.fetchone()["count"]
    except Exception:
        total_users = 0

    try:
        c.execute("SELECT COUNT(*) as count FROM users WHERE is_pro = 1 OR subscription_status = 'active'")
        total_pro = c.fetchone()["count"]
    except Exception:
        total_pro = 0

    try:
        c.execute("SELECT SUM(amount) as total FROM orders WHERE status = 'approved'")
        rev_row = c.fetchone()
        total_revenue = rev_row["total"] if rev_row and rev_row["total"] else 0.0
    except Exception:
        total_revenue = 0.0

    conn.close()

    rows = ""
    for l in leads:
        clean_phone = re.sub(r'\D', '', str(l.get('phone', '')))
        if clean_phone and not clean_phone.startswith('55'):
            clean_phone = '55' + clean_phone
        
        lead_name = l.get('name', 'Lead')
        budget_name = str(l.get('budget_tier', 'economico')).capitalize()
        msg = f"Olá {lead_name}, tudo bem? Vi seu diagnóstico metabólico no NutriCore Pro (Perfil {budget_name}). Gostaria de tirar alguma dúvida sobre o plano?"
        wpp_url = f"[https://wa.me/](https://wa.me/){clean_phone}?text={urllib.parse.quote(msg)}" if clean_phone else "#"
        cals = l.get('daily_calories') or '-'
        created_at_val = str(l.get('created_at', ''))[:16].replace('T', ' ')

        rows += f"""
        <tr style="border-bottom: 1px solid #1f2937;">
            <td style="padding: 12px; color: #9ca3af;">#{l.get('id', '-')}</td>
            <td style="padding: 12px; font-weight: 600;">{lead_name}</td>
            <td style="padding: 12px; color: #cbd5e1;">{l.get('email', '-')}</td>
            <td style="padding: 12px;">
                <a href="{wpp_url}" target="_blank" style="background: #064e3b; color: #34d399; padding: 6px 12px; border-radius: 6px; text-decoration: none; font-size: 0.85rem; font-weight: bold; display: inline-flex; align-items: center; gap: 4px;">
                    💬 {l.get('phone', '-')}
                </a>
            </td>
            <td style="padding: 12px;"><span style="background: #1e293b; color: #34d399; padding: 4px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: 600;">{budget_name}</span></td>
            <td style="padding: 12px; color: #38bdf8; font-weight: bold;">{cals} kcal</td>
            <td style="padding: 12px; color: #9ca3af; font-size: 0.85rem;">{created_at_val}</td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Painel Executivo - NutriCore Pro</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0b0f19; color: #f8fafc; margin: 0; padding: 2.5rem; }}
            .container {{ max-width: 1300px; margin: 0 auto; }}
            .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 2rem; }}
            .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1.25rem; margin-bottom: 2.5rem; }}
            .stat-card {{ background: #111827; padding: 1.5rem; border-radius: 0.75rem; border: 1px solid #1f2937; }}
            .stat-title {{ color: #9ca3af; font-size: 0.85rem; text-transform: uppercase; font-weight: 600; }}
            .stat-val {{ font-size: 2rem; font-weight: 800; color: #10b981; margin-top: 0.5rem; }}
            .btn-csv {{ background: #2563eb; color: white; padding: 10px 18px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 0.9rem; }}
            .table-wrap {{ background: #111827; border-radius: 0.75rem; border: 1px solid #1f2937; overflow-x: auto; }}
            table {{ width: 100%; border-collapse: collapse; text-align: left; }}
            th {{ background: #1f2937; padding: 14px 12px; color: #9ca3af; font-size: 0.8rem; text-transform: uppercase; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div>
                    <h1 style="margin: 0; font-size: 1.8rem;">🚀 Painel Executivo de Vendas</h1>
                    <p style="color: #9ca3af; margin: 5px 0 0 0; font-size: 0.9rem;">Leads em tempo real com filtro de orçamento e WhatsApp.</p>
                </div>
                <div style="display: flex; gap: 12px; align-items: center;">
                    <a href="/admin/export/leads.csv?senha={param_senha}" class="btn-csv">📥 Baixar Planilha CSV</a>
                    <a href="/admin" style="color: #ef4444; text-decoration: none; font-weight: bold; font-size: 0.9rem;">Sair</a>
                </div>
            </div>

            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-title">Leads Capturados</div>
                    <div class="stat-val">{len(leads)}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-title">Total de Usuários</div>
                    <div class="stat-val" style="color: #38bdf8;">{total_users}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-title">Assinantes PRO</div>
                    <div class="stat-val" style="color: #f59e0b;">{total_pro}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-title">Faturamento Aprovado</div>
                    <div class="stat-val" style="color: #10b981;">R$ {total_revenue:,.2f}</div>
                </div>
            </div>

            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Nome</th>
                            <th>E-mail</th>
                            <th>Recuperação WhatsApp</th>
                            <th>Orçamento</th>
                            <th>Meta Calórica</th>
                            <th>Data/Hora</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows if rows else '<tr><td colspan="7" style="padding: 30px; text-align: center; color: #9ca3af;">Nenhum lead registrado no banco.</td></tr>'}
                    </tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
