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
# 1. CONFIGURAÇÕES E BANCO DE DADOS SUPABASE (POSTGRESQL)
# ==============================================================================

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "nutricore2026").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
MERCADO_PAGO_TOKEN = os.getenv("MERCADO_PAGO_ACCESS_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_DATABASE_URL", "").strip()

def get_db_conn():
    if not SUPABASE_URL:
        raise Exception("Variável SUPABASE_DATABASE_URL não configurada no ambiente (Render).")
    return psycopg2.connect(SUPABASE_URL)

def init_db():
    if not SUPABASE_URL:
        print("[AVISO] SUPABASE_DATABASE_URL ausente. Banco não inicializado.")
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

        # Migrações seguras de colunas no Postgres
        try:
            c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_pro INTEGER DEFAULT 0;")
            c.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS budget_tier TEXT DEFAULT 'economico';")
            c.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS tmb REAL;")
            c.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS daily_calories REAL;")
            c.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS quiz_data_json TEXT;")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS qr_code TEXT;")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS qr_code_base64 TEXT;")
        except Exception:
            pass

        conn.commit()
        conn.close()
        print("[DB] Tabelas do Supabase inicializadas e validadas!")
    except Exception as e:
        print(f"[DB ERRO] Falha ao inicializar tabelas: {e}")

init_db()

# ==============================================================================
# 2. SEGURANÇA E AUTENTICAÇÃO
# ==============================================================================

def hash_password(password: str, salt: Optional[str] = None):
    if not salt:
        salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000).hex()
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
            FROM sessions s JOIN users u ON s.user_id = u.id WHERE s.token = %s
        ''', (token_clean,))
        row = c.fetchone()
        conn.close()
        if row:
            return {
                "id": row["id"], "name": row["name"], "email": row["email"],
                "subscription_status": row["subscription_status"], "plan_type": row["plan_type"],
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
    MASCULINO = "masculino"; FEMININO = "feminino"
class NivelAtividadeEnum(str, Enum):
    SEDENTARIO = "sedentario"; LEVE = "leve"; MODERADO = "moderado"; INTENSO = "intenso"
class ObjetivoEnum(str, Enum):
    PERDA_PESO = "perda_peso"; MANUTENCAO = "manutencao"; HIPERTROFIA = "hipertrofia"
class OrcamentoEnum(str, Enum):
    ECONOMICO = "economico"; MODERADO = "moderado"; PREMIUM = "premium"

class RegisterInput(BaseModel): name: str = Field(..., min_length=2); email: str = Field(..., min_length=3); password: str = Field(..., min_length=6)
class LoginInput(BaseModel): email: str; password: str
class AuthResponse(BaseModel): token: str; user: dict

class LeadCaptureInput(BaseModel):
    name: str; email: str; phone: str
    idade: Optional[int] = 28; sexo: Optional[str] = "masculino"; peso_kg: Optional[float] = 78.0
    altura_cm: Optional[float] = 178.0; peso_alvo_kg: Optional[float] = 70.0
    nivel_atividade: Optional[str] = "moderado"; objetivo: Optional[str] = "perda_peso"
    orcamento: Optional[str] = "economico"; obstaculo: Optional[str] = "falta_tempo"; estilo_culinario: Optional[str] = "caseiro_brasil"

class CreatePixPaymentInput(BaseModel):
    plan_type: Optional[str] = "anual"; amount: Optional[float] = None; email: Optional[str] = None; name: Optional[str] = None

class UserDataSyncInput(BaseModel):
    profile: Optional[dict] = None; diet: Optional[dict] = None; evolution: Optional[list] = None

class PerfilUsuarioInput(BaseModel):
    idade: int = 28; sexo: SexoEnum = SexoEnum.MASCULINO; peso_kg: float = 78.0; altura_cm: float = 178.0
    nivel_atividade: NivelAtividadeEnum = NivelAtividadeEnum.MODERADO; objetivo: ObjetivoEnum = ObjetivoEnum.PERDA_PESO
    orcamento: Optional[OrcamentoEnum] = OrcamentoEnum.ECONOMICO; ritmo_objetivo: Optional[str] = "moderado"
    preferencia: Optional[str] = "onivoro"; estilo_culinario: Optional[str] = "caseiro_brasil"
    alimentos_favoritos: Optional[str] = ""; alimentos_evitar: Optional[str] = ""; intolerancias_saude: Optional[List[str]] = []
    refeicoes_por_dia: int = 4; dias_plano: int = 7; gemini_api_key: Optional[str] = None

class Macronutrientes(BaseModel):
    proteinas_g: float; carboidratos_g: float; gorduras_g: float; calorias_totais: float

class RefeicaoIA(BaseModel):
    nome_refeicao: str; titulo_prato: str; horario_sugerido: str; calorias_alvo: float
    proteinas_refeicao_g: float; carboidratos_refeicao_g: float; gorduras_refeicao_g: float
    ingredientes: List[str]; modo_preparo: str; dica_chef: str

class DiaPlano(BaseModel):
    dia: int; titulo_dia: str; refeicoes: List[RefeicaoIA]

class TrocaAlimentoInput(BaseModel):
    refeicao_atual: RefeicaoIA; motivo_ou_substituto: str; orcamento: Optional[str] = "economico"
    preferencia: Optional[str] = "onivoro"; estilo_culinario: Optional[str] = "caseiro_brasil"
    intolerancias_saude: Optional[List[str]] = []; gemini_api_key: Optional[str] = None

class ConsultaFuncionalInput(BaseModel):
    objetivo_especifico: str; preferencia: Optional[str] = "onivoro"; gemini_api_key: Optional[str] = None

class TreinoInput(BaseModel):
    nivel: str = "intermediario"; foco: str = "hipertrofia"; equipamento: str = "academia"
    tempo_minutos: int = 45; gemini_api_key: Optional[str] = None

# ==============================================================================
# 4. MOTOR IA GEMINI (SDK GOOGLE GENAI - SEM FALLBACK ESTRUTURADO)
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
    
    inicio = texto.find("{")
    inicio_lista = texto.find("[")
    if inicio != -1 and (inicio_lista == -1 or inicio < inicio_lista):
        fim = texto.rfind("}")
        if fim != -1: texto = texto[inicio:fim+1]
    elif inicio_lista != -1:
        fim = texto.rfind("]")
        if fim != -1: texto = texto[inicio_lista:fim+1]
    return json.loads(texto)

def obter_chave(api_key_param: Optional[str]):
    key = api_key_param or GEMINI_API_KEY
    if not key or key.strip() == "":
        return None
    return key.strip()

def executar_chamada_ia(prompt: str, chave_api: Optional[str] = None) -> dict:
    key = obter_chave(chave_api)
    if not key:
        raise HTTPException(status_code=400, detail="Chave API do Gemini ausente nas configurações.")

    client = genai.Client(api_key=key.strip())
    erros = []
    
    for modelo in MODELOS_ATIVOS:
        try:
            response = client.models.generate_content(
                model=modelo,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.75
                )
            )
            if response and response.text:
                return extrair_json_seguro(response.text)
        except Exception as e:
            erros.append(f"{modelo}: {str(e)}")
            continue

    raise HTTPException(status_code=502, detail=f"Falha ao conectar na IA do Gemini: {'; '.join(erros)}")

# ==============================================================================
# 5. CÁLCULOS METABÓLICOS
# ==============================================================================

def calcular_metas(p: PerfilUsuarioInput):
    tmb = (10 * p.peso_kg) + (6.25 * p.altura_cm) - (5 * p.idade) + (5 if p.sexo == SexoEnum.MASCULINO else -161)
    fatores = {NivelAtividadeEnum.SEDENTARIO: 1.2, NivelAtividadeEnum.LEVE: 1.375, NivelAtividadeEnum.MODERADO: 1.55, NivelAtividadeEnum.INTENSO: 1.725}
    tdee = tmb * fatores.get(p.nivel_atividade, 1.55)

    if p.objetivo == ObjetivoEnum.PERDA_PESO:
        meta_calorica = tdee * (0.85 if p.ritmo_objetivo == "conservador" else (0.75 if p.ritmo_objetivo == "agressivo" else 0.80))
    elif p.objetivo == ObjetivoEnum.HIPERTROFIA:
        meta_calorica = tdee * (1.08 if p.ritmo_objetivo == "conservador" else (1.20 if p.ritmo_objetivo == "agressivo" else 1.15))
    else:
        meta_calorica = tdee

    fator_prot = 2.2 if p.objetivo == ObjetivoEnum.HIPERTROFIA else (2.0 if p.objetivo == ObjetivoEnum.PERDA_PESO else 1.8)
    proteinas_g = p.peso_kg * fator_prot
    cal_prot = proteinas_g * 4
    cal_gord = meta_calorica * 0.25
    gorduras_g = cal_gord / 9
    carboidratos_g = max((meta_calorica - (cal_prot + cal_gord)) / 4, 30.0)

    macros = Macronutrientes(
        proteinas_g=round(proteinas_g, 1), carboidratos_g=round(carboidratos_g, 1),
        gorduras_g=round(gorduras_g, 1), calorias_totais=round(meta_calorica, 0)
    )
    return round(tmb, 1), round(tdee, 1), round(meta_calorica, 1), macros

# ==============================================================================
# 6. ROTAS FASTAPI
# ==============================================================================

app = FastAPI(title="NutriCore Pro Engine Supabase", version="28.0.0")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

@app.get("/manifest.json")
def serve_manifest():
    if os.path.exists("manifest.json"): return FileResponse("manifest.json")
    return {"name": "NutriCore Pro", "short_name": "NutriCore", "start_url": "/", "display": "standalone", "background_color": "#0f172a", "theme_color": "#22c55e"}

@app.get("/")
def home():
    if os.path.exists("index.html"): return FileResponse("index.html")
    return HTMLResponse("<h2>NutriCore Pro Engine Online.</h2>")

@app.get("/quiz")
def quiz_page():
    if os.path.exists("quiz.html"): return FileResponse("quiz.html")
    return HTMLResponse("<h2>Quiz NutriCore Pro Online.</h2>")

@app.get("/health")
def health():
    return {
        "status": "online",
        "database": "Supabase PostgreSQL" if SUPABASE_URL else "ERRO: SEM BANCO",
        "gemini_configured": bool(GEMINI_API_KEY),
        "timestamp": datetime.utcnow().isoformat()
    }

# --- CAPTURA DE LEADS (QUIZ) SUPABASE ---

@app.post("/api/v1/lead/capture")
@app.post("/api/lead/capture")
def capturar_lead_quiz(lead: LeadCaptureInput):
    try:
        tmb = (10 * lead.peso_kg) + (6.25 * lead.altura_cm) - (5 * lead.idade) + (5 if str(lead.sexo).lower() in ["masculino", "homem", "m"] else -161)
        fatores = {"sedentario": 1.2, "leve": 1.375, "moderado": 1.55, "intenso": 1.725}
        tdee = tmb * fatores.get(str(lead.nivel_atividade).lower(), 1.55)

        if "perda" in str(lead.objetivo).lower() or "emagrecer" in str(lead.objetivo).lower():
            meta_calorica = tdee * 0.80
            semanas_estimadas = max(2, int(max(0.0, lead.peso_kg - lead.peso_alvo_kg) / 0.6))
        elif "hipertrofia" in str(lead.objetivo).lower() or "ganho" in str(lead.objetivo).lower():
            meta_calorica = tdee * 1.15
            semanas_estimadas = max(4, int(max(0.0, lead.peso_alvo_kg - lead.peso_kg) / 0.4))
        else:
            meta_calorica = tdee
            semanas_estimadas = 4

        imc = lead.peso_kg / ((lead.altura_cm / 100) ** 2)
        agora = datetime.utcnow().isoformat()
        orcamento_sel = lead.orcamento or "economico"

        clean_phone = re.sub(r'\D', '', str(lead.phone))
        if not clean_phone.startswith('55'): clean_phone = '55' + clean_phone
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
            "status": "success", "tmb": round(tmb, 0), "tdee": round(tdee, 0), "daily_calories": round(meta_calorica, 0),
            "meta_calorica": round(meta_calorica, 0), "imc": round(imc, 1), "budget_tier": orcamento_sel,
            "estimated_weeks": semanas_estimadas, "semanas_estimadas": semanas_estimadas, "recovery_whatsapp_url": wpp_url
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- AUTENTICAÇÃO SUPABASE BLINDADA ---

@app.post("/api/v1/auth/register", response_model=AuthResponse)
@app.post("/api/auth/register")
def cadastrar_usuario(dados: RegisterInput):
    try:
        email_clean = dados.email.lower().strip()
        conn = get_db_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        
        c.execute("SELECT id FROM users WHERE email = %s", (email_clean,))
        if c.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="Este e-mail já está cadastrado. Faça login.")

        pwd_hash, salt = hash_password(dados.password)
        agora = datetime.utcnow().isoformat()

        # Usando RETURNING id do Postgres de forma segura
        c.execute(
            "INSERT INTO users (name, email, password_hash, salt, subscription_status, plan_type, is_pro, created_at) VALUES (%s, %s, %s, %s, 'trial', 'free', 0, %s) RETURNING id",
            (dados.name.strip(), email_clean, pwd_hash, salt, agora)
        )
        row = c.fetchone()
        if not row:
            raise Exception("Falha ao criar ID do usuário no banco.")
        
        user_id = row["id"]
        token = secrets.token_urlsafe(32)
        c.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (%s, %s, %s)", (token, user_id, agora))
        conn.commit()
        conn.close()

        return AuthResponse(token=token, user={"id": user_id, "name": dados.name.strip(), "email": email_clean, "subscription_status": "trial", "plan_type": "free", "is_pro": False})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno de banco de dados (Supabase): {str(e)}")

@app.post("/api/v1/auth/login", response_model=AuthResponse)
@app.post("/api/auth/login")
def login_usuario(dados: LoginInput):
    try:
        email_clean = dados.email.lower().strip()
        conn = get_db_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT id, name, email, password_hash, salt, subscription_status, plan_type, subscription_end, is_pro FROM users WHERE email = %s", (email_clean,))
        user = c.fetchone()
        
        if not user or not verify_password(dados.password, user["salt"], user["password_hash"]):
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno no login (Supabase): {str(e)}")

@app.get("/api/v1/auth/me")
@app.get("/api/auth/me")
def obter_usuario_logado(authorization: Optional[str] = Header(None)):
    user = get_user_by_token(authorization)
    if not user: raise HTTPException(status_code=401, detail="Sessão expirada.")
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
        except Exception: pass
    return {"message": "Desconectado."}

# --- PAGAMENTOS PIX (MERCADO PAGO) SUPABASE ---

@app.post("/api/v1/payment/create-pix")
@app.post("/api/v1/pix/create")
@app.post("/api/payment/create-pix")
def criar_pagamento_pix(dados: CreatePixPaymentInput, authorization: Optional[str] = Header(None)):
    try:
        user = get_user_by_token(authorization)
        user_id = user["id"] if user else None
        user_email = (user["email"] if user else dados.email) or "cliente@nutricore.app"
        user_name = (user["name"] if user else dados.name) or "Cliente NutriCore"

        valor = float(dados.amount or (29.90 if dados.plan_type == "mensal" else 149.90))
        descricao = f"NutriCore Pro - Assinatura {str(dados.plan_type).capitalize()}"

        payment_id = None; qr_code = None; qr_code_base64 = None

        if MERCADO_PAGO_TOKEN and len(MERCADO_PAGO_TOKEN) > 15:
            try:
                headers = {"Authorization": f"Bearer {MERCADO_PAGO_TOKEN}", "Content-Type": "application/json", "X-Idempotency-Key": str(uuid.uuid4())}
                body = {"transaction_amount": valor, "description": descricao, "payment_method_id": "pix", "payer": {"email": user_email, "first_name": user_name.split()[0]}}
                resp = requests.post("[https://api.mercadopago.com/v1/payments](https://api.mercadopago.com/v1/payments)", headers=headers, json=body, timeout=8)
                if resp.status_code in [200, 201]:
                    data_mp = resp.json()
                    payment_id = str(data_mp.get("id"))
                    poi = data_mp.get("point_of_interaction", {}).get("transaction_data", {})
                    qr_code = poi.get("qr_code")
                    qr_code_base64 = poi.get("qr_code_base64")
            except Exception: pass

        if not payment_id:
            payment_id = f"demo_{secrets.token_hex(8)}"
            qr_code = f"00020126580014br.gov.bcb.pix0136nutricore-pix-{payment_id}520400005303986540{valor:.2f}5802BR5925NUTRICORE PRO SAAS6009SAO PAULO62070503***6304"
        
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
            "status": "success", "payment_id": payment_id, "id": payment_id, "amount": valor,
            "plan_type": dados.plan_type, "qr_code": qr_code, "qr_code_base64": qr_code_base64,
            "qr_code_url": qr_img_url, "copia_e_cola": qr_code
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/payment/check-status/{payment_id}")
def verificar_status_pagamento(payment_id: str):
    try:
        conn = get_db_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT status, plan_type, user_id FROM orders WHERE payment_id = %s", (payment_id,))
        order = c.fetchone()

        if not order:
            conn.close()
            return {"status": "pending", "is_approved": False}

        status_val, plan_type, order_user_id = order["status"], order["plan_type"], order["user_id"]

        if MERCADO_PAGO_TOKEN and not payment_id.startswith("demo_") and status_val == "pending":
            try:
                headers = {"Authorization": f"Bearer {MERCADO_PAGO_TOKEN}"}
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
            except Exception: pass

        conn.close()
        return {"status": status_val, "is_approved": status_val == "approved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.api_route("/api/v1/payment/simulate-approval/{payment_id}", methods=["GET", "POST"])
async def simular_aprovacao_com_id(payment_id: str, authorization: Optional[str] = Header(None)):
    try:
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
        return {"status": "success", "approved": True, "message": "Pagamento aprovado!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.api_route("/api/v1/payment/simulate", methods=["GET", "POST"])
async def simular_aprovacao_sem_id(authorization: Optional[str] = Header(None)):
    try:
        user = get_user_by_token(authorization)
        conn = get_db_conn()
        c = conn.cursor()
        sub_end = (datetime.utcnow() + timedelta(days=365)).isoformat()

        if user:
            c.execute("UPDATE users SET subscription_status = 'active', plan_type = 'anual', is_pro = 1, subscription_end = %s WHERE id = %s", (sub_end, user["id"]))
        else:
            c.execute("UPDATE users SET subscription_status = 'active', plan_type = 'anual', is_pro = 1, subscription_end = %s", (sub_end,))

        conn.commit()
        conn.close()
        return {"status": "success", "approved": True, "message": "Simulação PRO ativada!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- SINCRONIZAÇÃO NUVEM SUPABASE ---

@app.get("/api/v1/user/sync-data")
def obter_dados_usuario(authorization: Optional[str] = Header(None)):
    try:
        user = get_user_by_token(authorization)
        if not user: raise HTTPException(status_code=401, detail="Sessão expirada.")
        
        conn = get_db_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT profile_json, diet_json, evolution_json FROM user_data WHERE user_id = %s", (user["id"],))
        row = c.fetchone()
        conn.close()

        if not row: return {"profile": None, "diet": None, "evolution": None}
        return {
            "profile": json.loads(row["profile_json"]) if row["profile_json"] else None,
            "diet": json.loads(row["diet_json"]) if row["diet_json"] else None,
            "evolution": json.loads(row["evolution_json"]) if row["evolution_json"] else None
        }
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/user/sync-data")
def salvar_dados_usuario(dados: UserDataSyncInput, authorization: Optional[str] = Header(None)):
    try:
        user = get_user_by_token(authorization)
        if not user: raise HTTPException(status_code=401, detail="Sessão expirada.")

        conn = get_db_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT profile_json, diet_json, evolution_json FROM user_data WHERE user_id = %s", (user["id"],))
        row = c.fetchone()

        p_json = json.dumps(dados.profile) if dados.profile is not None else (row["profile_json"] if row else None)
        d_json = json.dumps(dados.diet) if dados.diet is not None else (row["diet_json"] if row else None)
        e_json = json.dumps(dados.evolution) if dados.evolution is not None else (row["evolution_json"] if row else None)

        c.execute('''
            INSERT INTO user_data (user_id, profile_json, diet_json, evolution_json) VALUES (%s, %s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET profile_json = EXCLUDED.profile_json, diet_json = EXCLUDED.diet_json, evolution_json = EXCLUDED.evolution_json
        ''', (user["id"], p_json, d_json, e_json))
        conn.commit()
        conn.close()
        return {"status": "ok"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- GERAÇÃO DE DIETA COM IA PURA ---

@app.post("/api/v1/diet/generate")
@app.post("/api/v1/plan/generate")
async def criar_plano(request: Request):
    try:
        raw_body = await request.json()
        orcamento_val = str(raw_body.get("orcamento") or "economico").lower()
        orcamento_enum = OrcamentoEnum.PREMIUM if "prem" in orcamento_val else (OrcamentoEnum.MODERADO if "mod" in orcamento_val else OrcamentoEnum.ECONOMICO)

        perfil = PerfilUsuarioInput(
            idade=int(raw_body.get("idade") or 28),
            sexo=SexoEnum.FEMININO if str(raw_body.get("sexo", "")).lower() in ["feminino", "f"] else SexoEnum.MASCULINO,
            peso_kg=float(raw_body.get("peso_kg") or 78.0),
            altura_cm=float(raw_body.get("altura_cm") or 178.0),
            nivel_atividade=NivelAtividadeEnum(raw_body.get("nivel_atividade", "moderado")),
            objetivo=ObjetivoEnum(raw_body.get("objetivo", "perda_peso")),
            orcamento=orcamento_enum,
            refeicoes_por_dia=max(3, min(6, int(raw_body.get("refeicoes_por_dia", 4)))),
            dias_plano=max(1, min(30, int(raw_body.get("dias_plano", 1)))),
            gemini_api_key=raw_body.get("gemini_api_key")
        )

        tmb, tdee, meta_calorica, macros = calcular_metas(perfil)
        
        diretrizes = {
            "economico": "ORÇAMENTO ECONÔMICO (R$12-18/dia). Ovos, frango, moela, sardinha, arroz, aveia, batata.",
            "moderado": "ORÇAMENTO MODERADO (R$22-35/dia). Patinho, tilápia, frango, arroz integral, batata doce, queijos magros.",
            "premium": "ORÇAMENTO GOURMET (R$45+/dia). Salmão, filé mignon, camarão, aspargos, mirtilos, castanhas."
        }

        prompt = f"""
        Como nutricionista clínico, gere plano em JSON puro para {perfil.dias_plano} dia(s).
        DADOS: {perfil.peso_kg}kg, {perfil.altura_cm}cm, Objetivo: {perfil.objetivo.value}.
        METAS DIÁRIAS: Calorias: {meta_calorica}kcal | Proteínas: {macros.proteinas_g}g | Carbos: {macros.carboidratos_g}g | Gorduras: {macros.gorduras_g}g.
        DIRETRIZ FINANCEIRA: {diretrizes.get(perfil.orcamento.value)}
        Refeições/dia: {perfil.refeicoes_por_dia}. Restrições: {raw_body.get('intolerancias_saude', [])}.
        
        Retorne OBRIGATORIAMENTE JSON seguindo este esquema para CADA UM DOS {perfil.dias_plano} DIAS:
        {{
          "dias": [
            {{
              "dia": 1, "titulo_dia": "Foco Metabólico",
              "refeicoes": [
                {{"nome_refeicao": "Café", "titulo_prato": "Prato", "horario_sugerido": "07:30", "calorias_alvo": 400, "proteinas_refeicao_g": 30, "carboidratos_refeicao_g": 40, "gorduras_refeicao_g": 10, "ingredientes": ["2 Ovos"], "modo_preparo": "Mexer", "dica_chef": "Rico em colina"}}
              ]
            }}
          ]
        }}
        """
        resultado_json = executar_chamada_ia(prompt, perfil.gemini_api_key)
        
        return {
            "status": "success", "gerado_por": "Gemini IA",
            "meta_calorica": meta_calorica, "macros": macros.dict(),
            "dias": resultado_json.get("dias", resultado_json) if isinstance(resultado_json, dict) else resultado_json
        }
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=f"Erro ao gerar dieta: {str(e)}")

# --- TROCA DE ALIMENTO ---

@app.post("/api/v1/diet/swap-food")
@app.post("/api/v1/food/swap")
def trocar_alimento_refeicao(dados: TrocaAlimentoInput):
    try:
        prompt = f"""
        Substitua rigorosamente e retorne OBRIGATORIAMENTE UM JSON PURO.
        ORIGINAL: {dados.refeicao_atual.nome_refeicao} - {dados.refeicao_atual.titulo_prato} ({dados.refeicao_atual.calorias_alvo} kcal)
        PEDIDO: "{dados.motivo_ou_substituto}"
        JSON FORMAT:
        {{
          "nome_refeicao": "{dados.refeicao_atual.nome_refeicao}", "titulo_prato": "Novo Titulo", "horario_sugerido": "{dados.refeicao_atual.horario_sugerido}",
          "calorias_alvo": {dados.refeicao_atual.calorias_alvo}, "proteinas_refeicao_g": {dados.refeicao_atual.proteinas_refeicao_g},
          "carboidratos_refeicao_g": {dados.refeicao_atual.carboidratos_refeicao_g}, "gorduras_refeicao_g": {dados.refeicao_atual.gorduras_refeicao_g},
          "ingredientes": ["Novo 1"], "modo_preparo": "Preparo", "dica_chef": "Motivo"
        }}
        """
        res = executar_chamada_ia(prompt, dados.gemini_api_key)
        nova = dados.refeicao_atual.dict()
        if isinstance(res, dict): nova.update(res)
        return nova
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=f"Erro ao substituir: {str(e)}")

# --- ANALISAR PROTOCOLO ---

@app.post("/api/v1/protocol/analyze")
async def analisar_protocolo(request: Request):
    try:
        body = await request.json()
        texto = body.get("protocol_text", "")
        if not texto: raise HTTPException(status_code=400, detail="Protocolo ausente")
        prompt = f"""Audite o protocolo: {texto}. Retorne JSON: {{"status_avaliacao": "Otimizado", "pontuacao_geral": 90, "resumo_executivo": "Parecer", "balanco_calorico_estimado": "Déficit", "distribuicao_macros": {{"proteinas": "ok", "carboidratos": "ok", "gorduras": "ok"}}, "pontos_fortes": ["1"], "pontos_de_atencao": ["1"], "recomendacoes_otimizacao": ["1"]}}"""
        res = executar_chamada_ia(prompt)
        res["status"] = "success"
        return res
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=f"Erro ao analisar: {str(e)}")

# --- CONSULTA & TREINO ---

@app.api_route("/api/v1/nutrition/consult", methods=["GET", "POST"])
def consultar_nutricao(dados: Optional[ConsultaFuncionalInput] = None):
    try:
        obj = dados.objetivo_especifico if dados else "Energia"
        prompt = f"""Gere estratégia funcional JSON para: "{obj}". Schema: {{"titulo_estrategia": "x", "explicacao_fisiologica": "x", "alimentos_chave": [{{"alimento": "x", "porcao_sugerida": "x", "por_que_funciona": "x", "como_consumir": "x"}}], "alimentos_evitar": ["x"], "receita_rapida": {{"titulo": "x", "tempo_preparo": "x", "ingredientes": ["x"], "modo_preparo": "x", "quando_tomar": "x"}}}}"""
        return executar_chamada_ia(prompt, dados.gemini_api_key if dados else None)
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/workout/generate")
def criar_treino(dados: TreinoInput):
    try:
        prompt = f"""Crie treino JSON. Nível: {dados.nivel} Foco: {dados.foco}. Schema: {{"titulo": "Treino", "foco_principal": "X", "aquecimento": [{{"nome": "X", "series": "2", "repeticoes": "10", "descanso": "30s", "dica_tecnica": "X"}}], "treino_principal": [], "finalizacao": []}}"""
        return executar_chamada_ia(prompt, dados.gemini_api_key)
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/ai/scan-plate")
def scan_plate():
    try:
        return executar_chamada_ia('Retorne um JSON puro para um scanner alimentar: {"status": "success", "prato_identificado": "Prato Tradicional", "calorias_estimadas": 580, "macros": {"proteina_g": 42, "carbo_g": 65, "gordura_g": 14}, "confianca_ia": "96%", "recomendacao": "Excelente."}')
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# --- ADMIN PANEL ---

@app.get("/admin/export/leads.csv")
def export_leads_csv(senha: str = ""):
    if senha.strip() != ADMIN_PASSWORD: raise HTTPException(status_code=403, detail="Senha incorreta.")
    try:
        conn = get_db_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM leads ORDER BY id DESC")
        leads = c.fetchall()
        conn.close()
        csv_content = "ID;Nome;Email;WhatsApp\n" + "".join([f"{l['id']};{l['name']};{l['email']};{l['phone']}\n" for l in leads])
        return Response(content=csv_content, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=leads.csv"})
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.api_route("/admin", methods=["GET", "POST"], response_class=HTMLResponse)
async def admin_portal(request: Request, senha: Optional[str] = None):
    body_senha = ""
    if request.method == "POST":
        try: body_senha = (await request.form()).get("senha", "")
        except: pass
    param_senha = (senha or request.query_params.get("senha") or body_senha or "").strip()

    if param_senha != ADMIN_PASSWORD:
        return "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Admin</title><style>body{background:#0b0f19;color:#fff;display:flex;align-items:center;justify-content:center;height:100vh;} .card{background:#111827;padding:2rem;border-radius:1rem;text-align:center;} input{width:100%;padding:0.75rem;margin:1rem 0;border-radius:0.5rem;} button{width:100%;padding:0.75rem;background:#10b981;border:none;border-radius:0.5rem;cursor:pointer;color:#fff;}</style></head><body><div class='card'><h2>⚡ Admin</h2><form method='post' action='/admin'><input type='password' name='senha' placeholder='Senha'><button type='submit'>Entrar</button></form></div></body></html>"

    try:
        conn = get_db_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM leads ORDER BY id DESC")
        leads = c.fetchall()
        conn.close()
        rows = "".join([f"<tr><td>#{l['id']}</td><td>{l['name']}</td><td>{l['email']}</td><td>{l['phone']}</td></tr>" for l in leads])
        return f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Dashboard</title><style>body{{background:#0b0f19;color:#fff;padding:2rem;}} table{{width:100%;border-collapse:collapse;}} th,td{{padding:10px;border-bottom:1px solid #1f2937;text-align:left;}}</style></head><body><h1>🚀 Leads</h1><table><thead><tr><th>ID</th><th>Nome</th><th>E-mail</th><th>WhatsApp</th></tr></thead><tbody>{rows}</tbody></table></body></html>"
    except Exception as e: return f"Erro no Admin: {str(e)}"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
