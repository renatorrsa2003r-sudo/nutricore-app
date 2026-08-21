import os
import json
import re
import time
import sqlite3
import secrets
import hashlib
import requests
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional
from enum import Enum
from google import genai
from google.genai import types

# ==========================================
# 1. BANCO DE DADOS & SEGURANÇA
# ==========================================

DB_PATH = "nutricore.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            subscription_status TEXT DEFAULT 'trial',
            plan_type TEXT DEFAULT 'free',
            subscription_end TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_data (
            user_id INTEGER PRIMARY KEY,
            profile_json TEXT,
            diet_json TEXT,
            evolution_json TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            payment_id TEXT UNIQUE,
            plan_type TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            qr_code TEXT,
            qr_code_base64 TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT NOT NULL,
            quiz_data_json TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

init_db()

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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT u.id, u.name, u.email, u.subscription_status, u.plan_type, u.subscription_end
        FROM sessions s 
        JOIN users u ON s.user_id = u.id 
        WHERE s.token = ?
    ''', (token,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "id": row[0],
            "name": row[1],
            "email": row[2],
            "subscription_status": row[3],
            "plan_type": row[4],
            "subscription_end": row[5]
        }
    return None

# ==========================================
# 2. MODELOS DE DADOS E PAGAMENTO
# ==========================================

class RegisterInput(BaseModel):
    name: str = Field(..., min_length=2)
    email: str = Field(..., min_length=5)
    password: str = Field(..., min_length=6)

class LoginInput(BaseModel):
    email: str = Field(..., min_length=5)
    password: str = Field(..., min_length=6)

class AuthResponse(BaseModel):
    token: str
    user: dict

class LeadCaptureInput(BaseModel):
    name: str
    email: str
    phone: str
    idade: int = 28
    sexo: str = "masculino"
    peso_kg: float = 78.0
    altura_cm: float = 178.0
    peso_alvo_kg: float = 70.0
    nivel_atividade: str = "moderado"
    objetivo: str = "perda_peso"
    obstaculo: Optional[str] = "falta_tempo"
    estilo_culinario: Optional[str] = "caseiro_brasil"

class CreatePixPaymentInput(BaseModel):
    plan_type: str = Field(..., pattern="^(mensal|anual)$")

class UserDataSyncInput(BaseModel):
    profile: Optional[dict] = None
    diet: Optional[dict] = None
    evolution: Optional[list] = None

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

class PreferenciaAlimentarEnum(str, Enum):
    ONIVORO = "onivoro"
    VEGETARIANO = "vegetariano"
    VEGANO = "vegano"
    LOW_CARB = "low_carb"

class EstiloCulinarioEnum(str, Enum):
    CASEIRO = "caseiro_brasil"
    PRATICO = "pratico_rapido"
    MEDITERRANEO = "mediterraneo"
    ECONOMICO = "economico"

class PerfilUsuarioInput(BaseModel):
    idade: int = Field(28, ge=15, le=100)
    sexo: SexoEnum = SexoEnum.MASCULINO
    peso_kg: float = Field(78.0, gt=30, lt=300)
    altura_cm: float = Field(178.0, gt=100, lt=250)
    nivel_atividade: NivelAtividadeEnum = NivelAtividadeEnum.MODERADO
    objetivo: ObjetivoEnum = ObjetivoEnum.PERDA_PESO
    ritmo_objetivo: Optional[str] = "moderado"
    preferencia: PreferenciaAlimentarEnum = PreferenciaAlimentarEnum.ONIVORO
    estilo_culinario: EstiloCulinarioEnum = EstiloCulinarioEnum.CASEIRO
    alimentos_favoritos: Optional[str] = ""
    alimentos_evitar: Optional[str] = ""
    intolerancias_saude: Optional[List[str]] = []
    horario_acordar: Optional[str] = "07:00"
    horario_dormir: Optional[str] = "23:00"
    horario_treino: Optional[str] = "nenhum"
    habilidade_culinaria: Optional[str] = "pratico"
    orcamento: Optional[str] = "medio"
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

class PlanoAlimentarResponse(BaseModel):
    tmb: float
    tdee: float
    meta_calorica: float
    macros: Macronutrientes
    dias_total: int
    dias: List[DiaPlano]

class TrocaAlimentoInput(BaseModel):
    refeicao_atual: RefeicaoIA
    motivo_ou_substituto: str = Field(..., min_length=2)
    preferencia: Optional[str] = "onivoro"
    estilo_culinario: Optional[str] = "caseiro_brasil"
    intolerancias_saude: Optional[List[str]] = []
    gemini_api_key: Optional[str] = None

class ConsultaFuncionalInput(BaseModel):
    objetivo_especifico: str = Field(..., min_length=3)
    preferencia: Optional[str] = "onivoro"
    gemini_api_key: Optional[str] = None

class AlimentoRecomendado(BaseModel):
    alimento: str
    porcao_sugerida: str
    por_que_funciona: str
    como_consumir: str

class ReceitaTerapeutica(BaseModel):
    titulo: str
    tempo_preparo: str
    ingredientes: List[str]
    modo_preparo: str
    quando_tomar: str

class ConsultaFuncionalResponse(BaseModel):
    titulo_estrategia: str
    explicacao_fisiologica: str
    alimentos_chave: List[AlimentoRecomendado]
    alimentos_evitar: List[str]
    receita_rapida: ReceitaTerapeutica

class TreinoInput(BaseModel):
    nivel: str = "intermediario"
    foco: str = "hipertrofia"
    equipamento: str = "academia"
    tempo_minutos: int = 45
    gemini_api_key: Optional[str] = None

class Exercicio(BaseModel):
    nome: str
    series: str
    repeticoes: str
    descanso: str
    dica_tecnica: str

class TreinoResponse(BaseModel):
    titulo: str
    foco_principal: str
    aquecimento: List[Exercicio]
    treino_principal: List[Exercicio]
    finalizacao: List[Exercicio]

# ==========================================
# 3. MODELOS ATIVOS E EXECUTOR IA
# ==========================================

MODELOS_ATIVOS = [
    "gemini-3.5-flash-lite",
    "gemini-3.7-flash",
    "gemini-3.5-flash"
]

def extrair_json_seguro(texto: str):
    texto = texto.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-zA-Z]*\n?", "", texto)
        texto = re.sub(r"\n?```$", "", texto)
    return json.loads(texto.strip())

def obter_chave(api_key_param: Optional[str]):
    key = api_key_param or os.getenv("GEMINI_API_KEY")
    if not key or key.strip() == "":
        raise HTTPException(
            status_code=400,
            detail="Chave API do Gemini ausente. Configure sua chave na aba Configurações."
        )
    return key.strip()

def executar_chamada_ia(client: genai.Client, prompt: str):
    ultimo_erro = None
    for modelo in MODELOS_ATIVOS:
        for tentativa in range(2):
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
                erro_str = str(e)
                ultimo_erro = erro_str
                if "404" in erro_str or "NOT_FOUND" in erro_str:
                    break
                time.sleep(1.0)

    raise HTTPException(
        status_code=503,
        detail=f"Erro ao comunicar com a IA. Detalhes: {ultimo_erro}"
    )

# ==========================================
# 4. LÓGICA NUTRICIONAL
# ==========================================

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

    fator_prot = 2.2 if p.objetivo == ObjetivoEnum.HIPERTROFIA else (1.8 if p.objetivo == ObjetivoEnum.PERDA_PESO else 1.6)
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

# ==========================================
# 5. APP FASTAPI E ROTAS
# ==========================================

app = FastAPI(title="NutriCore Pro Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": f"Erro interno: {str(exc)}"})

@app.get("/manifest.json")
def serve_manifest():
    return FileResponse("manifest.json")

@app.get("/")
def home():
    return FileResponse("index.html")

@app.get("/quiz")
def quiz_page():
    return FileResponse("quiz.html")

# --- ROTAS DE CAPTURA DE LEADS (QUIZ) ---

@app.post("/api/v1/lead/capture")
def capturar_lead_quiz(lead: LeadCaptureInput):
    # Cálculo rápido de diagnóstico
    if lead.sexo == "masculino":
        tmb = (10 * lead.peso_kg) + (6.25 * lead.altura_cm) - (5 * lead.idade) + 5
    else:
        tmb = (10 * lead.peso_kg) + (6.25 * lead.altura_cm) - (5 * lead.idade) - 161

    fatores = {"sedentario": 1.2, "leve": 1.375, "moderado": 1.55, "intenso": 1.725}
    tdee = tmb * fatores.get(lead.nivel_atividade, 1.55)

    if lead.objetivo == "perda_peso":
        meta_calorica = tdee * 0.80
        dif_peso = max(0.0, lead.peso_kg - lead.peso_alvo_kg)
        semanas_estimadas = max(2, int(dif_peso / 0.6))
    elif lead.objetivo == "hipertrofia":
        meta_calorica = tdee * 1.15
        dif_peso = max(0.0, lead.peso_alvo_kg - lead.peso_kg)
        semanas_estimadas = max(4, int(dif_peso / 0.4))
    else:
        meta_calorica = tdee
        semanas_estimadas = 4

    imc = lead.peso_kg / ((lead.altura_cm / 100) ** 2)

    agora = datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO leads (name, email, phone, quiz_data_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (lead.name.strip(), lead.email.lower().strip(), lead.phone.strip(), json.dumps(lead.dict()), agora)
    )
    conn.commit()
    conn.close()

    return {
        "tmb": round(tmb, 0),
        "tdee": round(tdee, 0),
        "meta_calorica": round(meta_calorica, 0),
        "imc": round(imc, 1),
        "semanas_estimadas": semanas_estimadas,
        "mensagem_personalizada": f"Com base na sua rotina e metabolismo, identificamos um potencial de transformação corporal consistente em {semanas_estimadas} semanas sem cortes bruscos."
    }

# --- ROTAS DE AUTENTICAÇÃO ---

@app.post("/api/v1/auth/register", response_model=AuthResponse)
def cadastrar_usuario(dados: RegisterInput):
    email_clean = dados.email.lower().strip()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email = ?", (email_clean,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Este e-mail já está cadastrado. Faça login.")

    pwd_hash, salt = hash_password(dados.password)
    agora = datetime.utcnow().isoformat()

    c.execute(
        "INSERT INTO users (name, email, password_hash, salt, subscription_status, plan_type, created_at) VALUES (?, ?, ?, ?, 'trial', 'free', ?)",
        (dados.name.strip(), email_clean, pwd_hash, salt, agora)
    )
    user_id = c.lastrowid
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)", (token, user_id, agora))
    conn.commit()
    conn.close()

    return AuthResponse(
        token=token,
        user={
            "id": user_id,
            "name": dados.name.strip(),
            "email": email_clean,
            "subscription_status": "trial",
            "plan_type": "free"
        }
    )

@app.post("/api/v1/auth/login", response_model=AuthResponse)
def login_usuario(dados: LoginInput):
    email_clean = dados.email.lower().strip()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, email, password_hash, salt, subscription_status, plan_type, subscription_end FROM users WHERE email = ?", (email_clean,))
    user = c.fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=400, detail="E-mail ou senha incorretos.")

    user_id, name, email, stored_hash, salt, status, plan, sub_end = user
    if not verify_password(dados.password, salt, stored_hash):
        conn.close()
        raise HTTPException(status_code=400, detail="E-mail ou senha incorretos.")

    token = secrets.token_urlsafe(32)
    agora = datetime.utcnow().isoformat()
    c.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)", (token, user_id, agora))
    conn.commit()
    conn.close()

    return AuthResponse(
        token=token,
        user={
            "id": user_id,
            "name": name,
            "email": email,
            "subscription_status": status,
            "plan_type": plan,
            "subscription_end": sub_end
        }
    )

@app.get("/api/v1/auth/me")
def obter_usuario_logado(authorization: Optional[str] = Header(None)):
    token = authorization.replace("Bearer ", "") if authorization else None
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Sessão expirada. Faça login novamente.")
    return user

@app.post("/api/v1/auth/logout")
def logout_usuario(authorization: Optional[str] = Header(None)):
    token = authorization.replace("Bearer ", "") if authorization else None
    if token:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()
    return {"message": "Desconectado com sucesso."}

# --- ROTAS DE PAGAMENTO PIX ---

@app.post("/api/v1/payment/create-pix")
def criar_pagamento_pix(dados: CreatePixPaymentInput, authorization: Optional[str] = Header(None)):
    token = authorization.replace("Bearer ", "") if authorization else None
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para assinar.")

    valor = 29.90 if dados.plan_type == "mensal" else 149.90
    descricao = f"NutriCore Pro - Assinatura {dados.plan_type.capitalize()}"
    mp_access_token = os.getenv("MERCADO_PAGO_ACCESS_TOKEN")

    if mp_access_token:
        headers = {
            "Authorization": f"Bearer {mp_access_token}",
            "Content-Type": "application/json"
        }
        body = {
            "transaction_amount": valor,
            "description": descricao,
            "payment_method_id": "pix",
            "payer": {
                "email": user["email"],
                "first_name": user["name"]
            }
        }
        resp = requests.post("[https://api.mercadopago.com/v1/payments](https://api.mercadopago.com/v1/payments)", headers=headers, json=body)
        if resp.status_code in [200, 201]:
            data_mp = resp.json()
            payment_id = str(data_mp.get("id"))
            poi = data_mp.get("point_of_interaction", {}).get("transaction_data", {})
            qr_code = poi.get("qr_code")
            qr_code_base64 = poi.get("qr_code_base64")
        else:
            raise HTTPException(status_code=500, detail="Erro ao gerar cobrança no gateway de pagamento.")
    else:
        payment_id = f"demo_{secrets.token_hex(8)}"
        qr_code = f"00020126580014br.gov.bcb.pix0136nutricore-pix-{payment_id}520400005303986540{valor:.2f}5802BR5925NUTRICORE PRO SAAS6009SAO PAULO62070503***6304"
        qr_code_base64 = None

    agora = datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO orders (user_id, payment_id, plan_type, amount, status, qr_code, qr_code_base64, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
    ''', (user["id"], payment_id, dados.plan_type, valor, qr_code, qr_code_base64, agora))
    conn.commit()
    conn.close()

    return {
        "payment_id": payment_id,
        "amount": valor,
        "plan_type": dados.plan_type,
        "qr_code": qr_code,
        "qr_code_base64": qr_code_base64
    }

@app.get("/api/v1/payment/check-status/{payment_id}")
def verificar_status_pagamento(payment_id: str, authorization: Optional[str] = Header(None)):
    token = authorization.replace("Bearer ", "") if authorization else None
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Sessão expirada.")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT status, plan_type, user_id FROM orders WHERE payment_id = ?", (payment_id,))
    order = c.fetchone()

    if not order:
        conn.close()
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")

    status, plan_type, order_user_id = order

    mp_access_token = os.getenv("MERCADO_PAGO_ACCESS_TOKEN")
    if mp_access_token and not payment_id.startswith("demo_") and status == "pending":
        headers = {"Authorization": f"Bearer {mp_access_token}"}
        resp = requests.get(f"[https://api.mercadopago.com/v1/payments/](https://api.mercadopago.com/v1/payments/){payment_id}", headers=headers)
        if resp.status_code == 200:
            status_mp = resp.json().get("status")
            if status_mp == "approved":
                status = "approved"
                dias_add = 30 if plan_type == "mensal" else 365
                sub_end = (datetime.utcnow() + timedelta(days=dias_add)).isoformat()
                c.execute("UPDATE orders SET status = 'approved' WHERE payment_id = ?", (payment_id,))
                c.execute("UPDATE users SET subscription_status = 'active', plan_type = ?, subscription_end = ? WHERE id = ?", (plan_type, sub_end, order_user_id))
                conn.commit()

    conn.close()
    return {"status": status}

@app.post("/api/v1/payment/simulate-approval/{payment_id}")
def simular_aprovacao(payment_id: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT plan_type, user_id FROM orders WHERE payment_id = ?", (payment_id,))
    order = c.fetchone()
    if not order:
        conn.close()
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")

    plan_type, user_id = order
    dias_add = 30 if plan_type == "mensal" else 365
    sub_end = (datetime.utcnow() + timedelta(days=dias_add)).isoformat()

    c.execute("UPDATE orders SET status = 'approved' WHERE payment_id = ?", (payment_id,))
    c.execute("UPDATE users SET subscription_status = 'active', plan_type = ?, subscription_end = ? WHERE id = ?", (plan_type, sub_end, user_id))
    conn.commit()
    conn.close()
    return {"message": "Pagamento aprovado e plano liberado com sucesso!"}

# --- ROTAS DE SINCRONIZAÇÃO NUVEM ---

@app.get("/api/v1/user/sync-data")
def obter_dados_usuario(authorization: Optional[str] = Header(None)):
    token = authorization.replace("Bearer ", "") if authorization else None
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Sessão expirada.")
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT profile_json, diet_json, evolution_json FROM user_data WHERE user_id = ?", (user["id"],))
    row = c.fetchone()
    conn.close()

    if not row:
        return {"profile": None, "diet": None, "evolution": None}
    
    return {
        "profile": json.loads(row[0]) if row[0] else None,
        "diet": json.loads(row[1]) if row[1] else None,
        "evolution": json.loads(row[2]) if row[2] else None
    }

@app.post("/api/v1/user/sync-data")
def salvar_dados_usuario(dados: UserDataSyncInput, authorization: Optional[str] = Header(None)):
    token = authorization.replace("Bearer ", "") if authorization else None
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Sessão expirada.")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT profile_json, diet_json, evolution_json FROM user_data WHERE user_id = ?", (user["id"],))
    row = c.fetchone()

    p_json = json.dumps(dados.profile) if dados.profile is not None else (row[0] if row else None)
    d_json = json.dumps(dados.diet) if dados.diet is not None else (row[1] if row else None)
    e_json = json.dumps(dados.evolution) if dados.evolution is not None else (row[2] if row else None)

    c.execute('''
        INSERT INTO user_data (user_id, profile_json, diet_json, evolution_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            profile_json = excluded.profile_json,
            diet_json = excluded.diet_json,
            evolution_json = excluded.evolution_json
    ''', (user["id"], p_json, d_json, e_json))

    conn.commit()
    conn.close()
    return {"status": "ok"}

# --- ROTAS NUTRICIONAIS E TREINOS ---

@app.post("/api/v1/diet/generate", response_model=PlanoAlimentarResponse)
def criar_plano(perfil: PerfilUsuarioInput):
    tmb, tdee, meta_calorica, macros = calcular_metas(perfil)
    api_key = obter_chave(perfil.gemini_api_key)
    client = genai.Client(api_key=api_key)

    prompt = f"""
    Atue como nutricionista clínico avançado e elabore um plano alimentar completo para exatamente {perfil.dias_plano} dia(s).
    
    ESTRUTURA JSON OBRIGATÓRIA:
    Retorne um objeto JSON contendo o campo "dias", onde cada elemento representa um dia com exatamente {perfil.refeicoes_por_dia} refeições.
    Exemplo:
    {{
      "dias": [
        {{
          "dia": 1,
          "titulo_dia": "Dia 1 - Foco em Energia & Saciedade",
          "refeicoes": [
            {{
              "nome_refeicao": "Café da Manhã",
              "titulo_prato": "Ovos Mexidos com Aveia e Fruta",
              "horario_sugerido": "07:30",
              "calorias_alvo": 420.0,
              "proteinas_refeicao_g": 28.0,
              "carboidratos_refeicao_g": 35.0,
              "gorduras_refeicao_g": 14.0,
              "ingredientes": ["3 ovos", "30g de farelo de aveia", "1 banana prata"],
              "modo_preparo": "Bata os ovos e prepare na frigideira. Sirva com banana e aveia.",
              "dica_chef": "Adicione canela para saciedade."
            }}
          ]
        }}
      ]
    }}

    Diretrizes para o período ({perfil.dias_plano} dias):
    - Gere {perfil.dias_plano} dia(s) com variedade inteligente, respeitando o estilo {perfil.estilo_culinario.value}.
    - Calorias Alvo por Dia: ~{meta_calorica} kcal | Macros: {macros.proteinas_g}g Proteína, {macros.carboidratos_g}g Carbo, {macros.gorduras_g}g Gordura.
    - Preferência: {perfil.preferencia.value}.
    - Alimentos favoritos: {perfil.alimentos_favoritos or 'Nenhum'}.
    - Alimentos a evitar: {perfil.alimentos_evitar or 'Nenhum'}.
    - Condições clínicas: {', '.join(perfil.intolerancias_saude) if perfil.intolerancias_saude else 'Nenhuma'}.

    Retorne APENAS o JSON puro.
    """

    resultado_json = executar_chamada_ia(client, prompt)
    
    if isinstance(resultado_json, list):
        lista_dias_raw = resultado_json
    elif isinstance(resultado_json, dict) and "dias" in resultado_json:
        lista_dias_raw = resultado_json["dias"]
    else:
        lista_dias_raw = [{"dia": 1, "titulo_dia": "Dia 1 - Plano Principal", "refeicoes": resultado_json}]

    dias_objs = []
    for item in lista_dias_raw:
        if "refeicoes" in item:
            refeicoes = [RefeicaoIA(**r) for r in item["refeicoes"]]
            dias_objs.append(DiaPlano(dia=item.get("dia", len(dias_objs)+1), titulo_dia=item.get("titulo_dia", f"Dia {len(dias_objs)+1}"), refeicoes=refeicoes))

    return PlanoAlimentarResponse(
        tmb=tmb,
        tdee=tdee,
        meta_calorica=meta_calorica,
        macros=macros,
        dias_total=len(dias_objs),
        dias=dias_objs
    )

@app.post("/api/v1/diet/swap-food", response_model=RefeicaoIA)
def trocar_alimento_refeicao(dados: TrocaAlimentoInput):
    api_key = obter_chave(dados.gemini_api_key)
    client = genai.Client(api_key=api_key)

    prompt = f"""
    Atue como nutricionista clínico avançado. O paciente deseja substituir um alimento ou alterar uma refeição específica mantendo a equivalência nutricional.

    REFEIÇÃO ATUAL:
    - Nome: {dados.refeicao_atual.nome_refeicao}
    - Prato atual: {dados.refeicao_atual.titulo_prato}
    - Calorias Alvo: ~{dados.refeicao_atual.calorias_alvo} kcal
    - Proteínas: ~{dados.refeicao_atual.proteinas_refeicao_g}g | Carbos: ~{dados.refeicao_atual.carboidratos_refeicao_g}g | Gorduras: ~{dados.refeicao_atual.gorduras_refeicao_g}g
    - Ingredientes atuais: {dados.refeicao_atual.ingredientes}

    PEDIDO DO PACIENTE: "{dados.motivo_ou_substituto}"
    Preferência: {dados.preferencia} | Estilo: {dados.estilo_culinario}
    Restrições Clínicas: {', '.join(dados.intolerancias_saude) if dados.intolerancias_saude else 'Nenhuma'}

    REGRAS OBRIGATÓRIAS:
    1. Atenda à solicitação (remova o que não gosta ou substitua pelo solicitado).
    2. Preserve as calorias e macronutrientes aproximados.
    3. Retorne estritamente o JSON:
    {{
      "nome_refeicao": "{dados.refeicao_atual.nome_refeicao}",
      "titulo_prato": "Novo Título do Prato",
      "horario_sugerido": "{dados.refeicao_atual.horario_sugerido}",
      "calorias_alvo": {dados.refeicao_atual.calorias_alvo},
      "proteinas_refeicao_g": {dados.refeicao_atual.proteinas_refeicao_g},
      "carboidratos_refeicao_g": {dados.refeicao_atual.carboidratos_refeicao_g},
      "gorduras_refeicao_g": {dados.refeicao_atual.gorduras_refeicao_g},
      "ingredientes": ["Ingrediente 1", "Ingrediente 2"],
      "modo_preparo": "Instruções práticas",
      "dica_chef": "Dica nutricional da nova combinação"
    }}
    Retorne APENAS o JSON puro.
    """

    resultado_json = executar_chamada_ia(client, prompt)
    return RefeicaoIA(**resultado_json)

@app.post("/api/v1/nutrition/consult", response_model=ConsultaFuncionalResponse)
def consultar_nutricao(dados: ConsultaFuncionalInput):
    api_key = obter_chave(dados.gemini_api_key)
    client = genai.Client(api_key=api_key)

    prompt = f"""
    Atue como nutricionista funcional e especialista em fitoterapia.
    Gere um protocolo terapêutico em JSON para: "{dados.objetivo_especifico}".
    Padrão: {dados.preferencia}.

    Estrutura JSON:
    {{
      "titulo_estrategia": "Título da estratégia",
      "explicacao_fisiologica": "Explicação científica clara",
      "alimentos_chave": [
        {{"alimento": "Nome", "porcao_sugerida": "Quantidade", "por_que_funciona": "Motivo", "como_consumir": "Uso"}}
      ],
      "alimentos_evitar": ["Item 1", "Item 2"],
      "receita_rapida": {{
        "titulo": "Nome do shot ou receita",
        "tempo_preparo": "3 min",
        "ingredientes": ["Item 1", "Item 2"],
        "modo_preparo": "Instruções",
        "quando_tomar": "Horário ideal"
      }}
    }}
    Retorne APENAS o JSON puro.
    """

    dados_funcionais = executar_chamada_ia(client, prompt)
    return ConsultaFuncionalResponse(**dados_funcionais)

@app.post("/api/v1/workout/generate", response_model=TreinoResponse)
def criar_treino(dados: TreinoInput):
    api_key = obter_chave(dados.gemini_api_key)
    client = genai.Client(api_key=api_key)

    prompt = f"""
    Crie uma sessão de treino em JSON.
    Nível: {dados.nivel} | Foco: {dados.foco} | Equipamento: {dados.equipamento} | Duração: {dados.tempo_minutos}min.

    Estrutura JSON:
    {{
      "titulo": "Título do Treino",
      "foco_principal": "{dados.foco}",
      "aquecimento": [{{"nome": "Aquecimento", "series": "2", "repeticoes": "45s", "descanso": "30s", "dica_tecnica": "Instrução"}}],
      "treino_principal": [{{"nome": "Exercício Principal", "series": "4", "repeticoes": "10-12", "descanso": "60s", "dica_tecnica": "Instrução"}}],
      "finalizacao": [{{"nome": "Core / Alongamento", "series": "3", "repeticoes": "45s", "descanso": "45s", "dica_tecnica": "Instrução"}}]
    }}
    Retorne APENAS o JSON puro.
    """

    dados_treino = executar_chamada_ia(client, prompt)
    return TreinoResponse(**dados_treino)
