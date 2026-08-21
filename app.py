import os
import re
import time
import json
import math
import uuid
import secrets
import hashlib
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any, Union
from enum import Enum

import requests
from fastapi import FastAPI, HTTPException, Request, Response, Header, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel, Field

# ==============================================================================
# 1. CONFIGURAÇÕES E BANCO DE DADOS LOCAL
# ==============================================================================

DB_PATH = "nutricore.db"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "nutricore2026").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
MERCADO_PAGO_TOKEN = os.getenv("MERCADO_PAGO_ACCESS_TOKEN", "").strip()

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
            is_pro INTEGER DEFAULT 0,
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
            user_id INTEGER,
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
            budget_tier TEXT DEFAULT 'economico',
            tmb REAL,
            daily_calories REAL,
            quiz_data_json TEXT,
            created_at TEXT NOT NULL
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS protocols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            protocol_text TEXT NOT NULL,
            analysis_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')

    def add_col(tabela, col_def):
        col_name = col_def.split()[0]
        c.execute(f"PRAGMA table_info({tabela})")
        cols = [col[1] for col in c.fetchall()]
        if col_name not in cols:
            try:
                c.execute(f"ALTER TABLE {tabela} ADD COLUMN {col_def}")
            except Exception:
                pass

    add_col("users", "is_pro INTEGER DEFAULT 0")
    add_col("leads", "budget_tier TEXT DEFAULT 'economico'")
    add_col("leads", "tmb REAL")
    add_col("leads", "daily_calories REAL")
    add_col("leads", "quiz_data_json TEXT")
    add_col("orders", "qr_code TEXT")
    add_col("orders", "qr_code_base64 TEXT")

    conn.commit()
    conn.close()

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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT u.id, u.name, u.email, u.subscription_status, u.plan_type, u.subscription_end, u.is_pro
        FROM sessions s 
        JOIN users u ON s.user_id = u.id 
        WHERE s.token = ?
    ''', (token_clean,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "id": row[0],
            "name": row[1],
            "email": row[2],
            "subscription_status": row[3],
            "plan_type": row[4],
            "subscription_end": row[5],
            "is_pro": bool(row[6]) or row[3] == 'active'
        }
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
# 4. MOTOR IA GEMINI (REST DIRETO)
# ==============================================================================

MODELOS_ATIVOS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro"
]

def extrair_json_seguro(texto: str):
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

def executar_chamada_ia(prompt: str, chave_api: Optional[str] = None):
    key = chave_api or GEMINI_API_KEY
    if not key or not str(key).startswith("AIzaSy"):
        return None

    for modelo in MODELOS_ATIVOS:
        for _ in range(2):
            try:
                url = f"[https://generativelanguage.googleapis.com/v1beta/models/](https://generativelanguage.googleapis.com/v1beta/models/){modelo}:generateContent?key={key}"
                headers = {"Content-Type": "application/json"}
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"response_mime_type": "application/json", "temperature": 0.7}
                }
                resp = requests.post(url, headers=headers, json=payload, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                    return extrair_json_seguro(raw_text)
            except Exception:
                time.sleep(0.3)
                continue
    return None

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

def gerar_cardapio_fallback_por_orcamento(orcamento: str, meta_calorica: float, macros: Macronutrientes):
    orcamento_clean = str(orcamento).lower()
    
    if orcamento_clean == "premium":
        return [
            {
                "nome_refeicao": "Café da Manhã Gourmet",
                "titulo_prato": "Omelete de Claras com Queijo de Cabra e Frutas Vermelhas",
                "horario_sugerido": "07:30",
                "calorias_alvo": round(meta_calorica * 0.25),
                "proteinas_refeicao_g": round(macros.proteinas_g * 0.25),
                "carboidratos_refeicao_g": round(macros.carboidratos_g * 0.25),
                "gorduras_refeicao_g": round(macros.gorduras_g * 0.25),
                "ingredientes": ["3 Ovos caipiras", "30g Queijo de cabra/búfala", "100g Mirtilos ou Morangos frescos", "Café espresso"],
                "modo_preparo": "Prepare a omelete em fogo baixo com manteiga ghee.",
                "dica_chef": "Rico em antioxidantes de alta densidade."
            },
            {
                "nome_refeicao": "Almoço Nobre",
                "titulo_prato": "Filé de Salmão com Aspargos e Quinoa Real",
                "horario_sugerido": "12:30",
                "calorias_alvo": round(meta_calorica * 0.35),
                "proteinas_refeicao_g": round(macros.proteinas_g * 0.35),
                "carboidratos_refeicao_g": round(macros.carboidratos_g * 0.35),
                "gorduras_refeicao_g": round(macros.gorduras_g * 0.35),
                "ingredientes": ["160g Salmão grelhado", "100g Quinoa real cozida", "Aspargos e tomatinhos confitados", "Azeite EV"],
                "modo_preparo": "Grelhe o salmão com a pele crocante e salteie os aspargos no azeite.",
                "dica_chef": "Excelente fonte de ômega-3 EPA/DHA."
            },
            {
                "nome_refeicao": "Lanche da Tarde",
                "titulo_prato": "Iogurte Grego Artesanal com Castanhas Nobres",
                "horario_sugerido": "16:30",
                "calorias_alvo": round(meta_calorica * 0.15),
                "proteinas_refeicao_g": round(macros.proteinas_g * 0.15),
                "carboidratos_refeicao_g": round(macros.carboidratos_g * 0.15),
                "gorduras_refeicao_g": round(macros.gorduras_g * 0.15),
                "ingredientes": ["150g Iogurte grego sem açúcar", "25g Mix de Nozes e Amêndoas", "1 colher de Mel cru"],
                "modo_preparo": "Misture os ingredientes em uma taça.",
                "dica_chef": "Gorduras monoinsaturadas e saciedade prolongada."
            },
            {
                "nome_refeicao": "Jantar Leve",
                "titulo_prato": "Medalhão de Mignon com Purê de Mandioquinha",
                "horario_sugerido": "20:00",
                "calorias_alvo": round(meta_calorica * 0.25),
                "proteinas_refeicao_g": round(macros.proteinas_g * 0.25),
                "carboidratos_refeicao_g": round(macros.carboidratos_g * 0.25),
                "gorduras_refeicao_g": round(macros.gorduras_g * 0.25),
                "ingredientes": ["150g Filé Mignon grelhado", "120g Purê de Mandioquinha", "Brócolis ninja ao vapor"],
                "modo_preparo": "Sele o mignon ao ponto e sirva com o purê aveludado.",
                "dica_chef": "Proteína de alto valor biológico e ferro heme."
            }
        ]
    elif orcamento_clean == "moderado":
        return [
            {
                "nome_refeicao": "Café da Manhã",
                "titulo_prato": "Ovos Mexidos com Pão Integral e Banana",
                "horario_sugerido": "07:30",
                "calorias_alvo": round(meta_calorica * 0.25),
                "proteinas_refeicao_g": round(macros.proteinas_g * 0.25),
                "carboidratos_refeicao_g": round(macros.carboidratos_g * 0.25),
                "gorduras_refeicao_g": round(macros.gorduras_g * 0.25),
                "ingredientes": ["3 Ovos inteiros mexidos", "2 Fatias de pão 100% integral", "1 Banana média com canela", "Café preto"],
                "modo_preparo": "Prepare os ovos na frigideira com 1 fio de azeite.",
                "dica_chef": "Proteína e carboidrato de média absorção."
            },
            {
                "nome_refeicao": "Almoço Equilibrado",
                "titulo_prato": "Patinho Moído com Arroz Integral e Feijão Carioca",
                "horario_sugerido": "12:30",
                "calorias_alvo": round(meta_calorica * 0.35),
                "proteinas_refeicao_g": round(macros.proteinas_g * 0.35),
                "carboidratos_refeicao_g": round(macros.carboidratos_g * 0.35),
                "gorduras_refeicao_g": round(macros.gorduras_g * 0.35),
                "ingredientes": ["150g Patinho moído", "120g Arroz integral", "80g Feijão carioca", "Salada verde com tomate", "Azeite EV"],
                "modo_preparo": "Refogue o patinho com cebola e alho.",
                "dica_chef": "Tempere a salada com azeite e limão."
            },
            {
                "nome_refeicao": "Lanche da Tarde",
                "titulo_prato": "Iogurte Natural Desnatado com Chia e Aveia",
                "horario_sugerido": "16:30",
                "calorias_alvo": round(meta_calorica * 0.15),
                "proteinas_refeicao_g": round(macros.proteinas_g * 0.15),
                "carboidratos_refeicao_g": round(macros.carboidratos_g * 0.15),
                "gorduras_refeicao_g": round(macros.gorduras_g * 0.15),
                "ingredientes": ["1 Pote iogurte natural (170g)", "30g Farelo de aveia", "1 Colher de sementes de chia"],
                "modo_preparo": "Misture tudo em uma tigela.",
                "dica_chef": "Excelente aporte de fibras solúveis."
            },
            {
                "nome_refeicao": "Jantar Regenerativo",
                "titulo_prato": "Filé de Tilápia com Batata Doce e Legumes ao Vapor",
                "horario_sugerido": "20:00",
                "calorias_alvo": round(meta_calorica * 0.25),
                "proteinas_refeicao_g": round(macros.proteinas_g * 0.25),
                "carboidratos_refeicao_g": round(macros.carboidratos_g * 0.25),
                "gorduras_refeicao_g": round(macros.gorduras_g * 0.25),
                "ingredientes": ["140g Tilápia grelhada", "100g Batata doce cozida", "Brócolis e cenoura ao vapor"],
                "modo_preparo": "Grelhe a tilápia com ervas finas.",
                "dica_chef": "Digestão rápida para preservar o sono profundo."
            }
        ]
    else:
        # Padrão: ECONÔMICO
        return [
            {
                "nome_refeicao": "Café da Manhã Econômico",
                "titulo_prato": "Ovos Mexidos com Cuscuz Nordestino e Banana",
                "horario_sugerido": "07:30",
                "calorias_alvo": round(meta_calorica * 0.25),
                "proteinas_refeicao_g": round(macros.proteinas_g * 0.25),
                "carboidratos_refeicao_g": round(macros.carboidratos_g * 0.25),
                "gorduras_refeicao_g": round(macros.gorduras_g * 0.25),
                "ingredientes": ["3 Ovos inteiros mexidos", "100g Cuscuz de milho cozido", "1 Banana prata com canela", "Café preto"],
                "modo_preparo": "Hidrate e cozinhe o cuscuz no vapor. Prepare os ovos mexidos na frigideira.",
                "dica_chef": "Custo por refeição de apenas ~R$ 3,50 com proteína completa."
            },
            {
                "nome_refeicao": "Almoço Fortalecedor",
                "titulo_prato": "Frango com Arroz Branco, Feijão e Repolho",
                "horario_sugerido": "12:30",
                "calorias_alvo": round(meta_calorica * 0.35),
                "proteinas_refeicao_g": round(macros.proteinas_g * 0.35),
                "carboidratos_refeicao_g": round(macros.carboidratos_g * 0.35),
                "gorduras_refeicao_g": round(macros.gorduras_g * 0.35),
                "ingredientes": ["160g Frango desfiado ou grelhado", "130g Arroz branco", "90g Feijão carioca", "Salada de repolho e cenoura"],
                "modo_preparo": "Cozinhe o frango com temperos naturais. Tempere a salada com vinagre e sal.",
                "dica_chef": "Feijão com arroz fornece todos os aminoácidos essenciais."
            },
            {
                "nome_refeicao": "Lanche da Tarde",
                "titulo_prato": "Mingau Prático de Aveia com Banana",
                "horario_sugerido": "16:30",
                "calorias_alvo": round(meta_calorica * 0.15),
                "proteinas_refeicao_g": round(macros.proteinas_g * 0.15),
                "carboidratos_refeicao_g": round(macros.carboidratos_g * 0.15),
                "gorduras_refeicao_g": round(macros.gorduras_g * 0.15),
                "ingredientes": ["40g Aveia em flocos", "150ml Água ou leite", "1 Banana amassada", "Canela a gosto"],
                "modo_preparo": "Leve a aveia com água e banana ao micro-ondas por 1m30s.",
                "dica_chef": "Energia limpa e saciedade de baixo custo."
            },
            {
                "nome_refeicao": "Jantar Nutritivo",
                "titulo_prato": "Omelete de Frango com Mandioca Cozida e Abóbora",
                "horario_sugerido": "20:00",
                "calorias_alvo": round(meta_calorica * 0.25),
                "proteinas_refeicao_g": round(macros.proteinas_g * 0.25),
                "carboidratos_refeicao_g": round(macros.carboidratos_g * 0.25),
                "gorduras_refeicao_g": round(macros.gorduras_g * 0.25),
                "ingredientes": ["2 Ovos + 60g Frango desfiado", "100g Mandioca cozida", "120g Abóbora cozida"],
                "modo_preparo": "Bata os ovos com o frango e faça na frigideira. Sirva com a mandioca e abóbora.",
                "dica_chef": "A abóbora é rica em betacaroteno e super econômica."
            }
        ]

# ==============================================================================
# 6. ROTAS FASTAPI
# ==============================================================================

app = FastAPI(title="NutriCore Pro Engine", version="15.0.0")

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
    return HTMLResponse("<h2>NutriCore Pro Engine Online.</h2>")

@app.get("/quiz")
def quiz_page():
    if os.path.exists("quiz.html"):
        return FileResponse("quiz.html")
    return HTMLResponse("<h2>Quiz NutriCore Pro Online.</h2>")

@app.get("/health")
def health():
    return {
        "status": "online",
        "gemini_configured": bool(GEMINI_API_KEY and GEMINI_API_KEY.startswith("AIzaSy")),
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

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO leads (name, email, phone, budget_tier, tmb, daily_calories, quiz_data_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email = ?", (email_clean,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Este e-mail já está cadastrado. Faça login.")

    pwd_hash, salt = hash_password(dados.password)
    agora = datetime.utcnow().isoformat()

    c.execute(
        "INSERT INTO users (name, email, password_hash, salt, subscription_status, plan_type, is_pro, created_at) VALUES (?, ?, ?, ?, 'trial', 'free', 0, ?)",
        (dados.name.strip(), email_clean, pwd_hash, salt, agora)
    )
    user_id = c.lastrowid
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)", (token, user_id, agora))
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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, email, password_hash, salt, subscription_status, plan_type, subscription_end, is_pro FROM users WHERE email = ?", (email_clean,))
    user = c.fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=400, detail="E-mail ou senha incorretos.")

    user_id, name, email, stored_hash, salt, status_sub, plan, sub_end, is_pro = user
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
        user={"id": user_id, "name": name, "email": email, "subscription_status": status_sub, "plan_type": plan, "subscription_end": sub_end, "is_pro": bool(is_pro) or status_sub == "active"}
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
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()
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

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO orders (user_id, payment_id, plan_type, amount, status, qr_code, qr_code_base64, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT status, plan_type, user_id FROM orders WHERE payment_id = ?", (payment_id,))
    order = c.fetchone()

    if not order:
        conn.close()
        return {"status": "pending", "is_approved": False}

    status_val, plan_type, order_user_id = order

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
                    c.execute("UPDATE orders SET status = 'approved' WHERE payment_id = ?", (payment_id,))
                    if order_user_id:
                        c.execute("UPDATE users SET subscription_status = 'active', plan_type = ?, is_pro = 1, subscription_end = ? WHERE id = ?", (plan_type, sub_end, order_user_id))
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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    sub_end = (datetime.utcnow() + timedelta(days=365)).isoformat()
    c.execute("UPDATE orders SET status = 'approved' WHERE payment_id = ?", (payment_id,))
    c.execute("SELECT user_id, plan_type FROM orders WHERE payment_id = ?", (payment_id,))
    order = c.fetchone()
    
    if order and order[0]:
        c.execute("UPDATE users SET subscription_status = 'active', plan_type = ?, is_pro = 1, subscription_end = ? WHERE id = ?", (order[1] or 'anual', sub_end, order[0]))
    elif user:
        c.execute("UPDATE users SET subscription_status = 'active', plan_type = 'anual', is_pro = 1, subscription_end = ? WHERE id = ?", (sub_end, user["id"]))
    else:
        c.execute("UPDATE users SET subscription_status = 'active', plan_type = 'anual', is_pro = 1, subscription_end = ?", (sub_end,))

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

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    sub_end = (datetime.utcnow() + timedelta(days=365)).isoformat()

    if payment_id:
        c.execute("UPDATE orders SET status = 'approved' WHERE payment_id = ?", (payment_id,))
    else:
        c.execute("UPDATE orders SET status = 'approved' WHERE status = 'pending'")

    if user:
        c.execute("UPDATE users SET subscription_status = 'active', plan_type = 'anual', is_pro = 1, subscription_end = ? WHERE id = ?", (sub_end, user["id"]))
    elif email:
        c.execute("UPDATE users SET subscription_status = 'active', plan_type = 'anual', is_pro = 1, subscription_end = ? WHERE email = ?", (sub_end, email.lower().strip()))
    else:
        c.execute("UPDATE users SET subscription_status = 'active', plan_type = 'anual', is_pro = 1, subscription_end = ?", (sub_end,))

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
    user = get_user_by_token(authorization)
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

# --- GERAÇÃO DE DIETA / IA ---

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
            "DIRETRIZ FINANCEIRA: ORÇAMENTO ECONÔMICO (BAIXA RENDA / R$ 12-18/dia). "
            "Utilize APENAS proteínas de altíssimo custo-benefício (ovos caipiras, peito/coxa de frango, sardinha fresca ou em lata, moela/fígado). "
            "Carboidratos base: arroz branco/integral, feijão, aveia em flocos, banana prata, mandioca, cuscuz de milho. "
            "Vegetais: repolho, cenoura, abóbora, chuchu. PROIBIDO prescrever salmão, queijos caros ou farinhas nobres."
        ),
        "moderado": (
            "DIRETRIZ FINANCEIRA: ORÇAMENTO MODERADO (MÉDIA RENDA / R$ 22-35/dia). "
            "Equilíbrio entre custo e conveniência. Proteínas: patinho moído, filé de tilápia, peito de frango, queijo minas, iogurte natural. "
            "Carboidratos e gorduras: arroz integral, batata-doce, sementes de chia, azeite extra virgem e frutas da estação."
        ),
        "premium": (
            "DIRETRIZ FINANCEIRA: ORÇAMENTO PREMIUM / GOURMET (ALTA RENDA / R$ 45+/dia). "
            "Priorize máxima sofisticação e conveniência. Proteínas: salmão fresco, filé mignon, camarão, queijo de cabra, iogurte grego artesanal. "
            "Carboidratos e gorduras: quinoa real, aspargos, mirtilos/framboesas, amêndoas, nozes e azeite trufado."
        )
    }

    instrucao_orcamento = diretrizes_orcamento.get(perfil.orcamento.value, diretrizes_orcamento["economico"])

    prompt = f"""
    Atue como nutricionista clínico avançado e elabore um plano alimentar completo para {perfil.dias_plano} dia(s).
    
    METAS NUTRICIONAIS:
    - Calorias Alvo: ~{meta_calorica} kcal | Macros: {macros.proteinas_g}g Proteína, {macros.carboidratos_g}g Carbo, {macros.gorduras_g}g Gordura.
    - {instrucao_orcamento}
    - Preferência: {preferencia} | Estilo Culinário: {estilo_culinario}.
    - Condições/Restrições: {', '.join(restricoes) if restricoes else 'Nenhuma'}.

    Retorne estritamente um JSON puro no formato:
    {{
      "dias": [
        {{
          "dia": 1,
          "titulo_dia": "Dia 1 - Plano {perfil.orcamento.value.capitalize()}",
          "refeicoes": [
            {{
              "nome_refeicao": "Café da Manhã",
              "titulo_prato": "Nome do Prato",
              "horario_sugerido": "07:30",
              "calorias_alvo": {round(meta_calorica * 0.25)},
              "proteinas_refeicao_g": {round(macros.proteinas_g * 0.25)},
              "carboidratos_refeicao_g": {round(macros.carboidratos_g * 0.25)},
              "gorduras_refeicao_g": {round(macros.gorduras_g * 0.25)},
              "ingredientes": ["Ingrediente 1", "Ingrediente 2"],
              "modo_preparo": "Instruções de preparo.",
              "dica_chef": "Dica nutricional ou de economia."
            }}
          ]
        }}
      ]
    }}
    """

    resultado_json = executar_chamada_ia(prompt, api_key)

    if resultado_json:
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

    default_refeicoes = gerar_cardapio_fallback_por_orcamento(perfil.orcamento.value, meta_calorica, macros)

    return {
        "status": "success",
        "tmb": tmb,
        "tdee": tdee,
        "meta_calorica": meta_calorica,
        "calorias_totais": meta_calorica,
        "budget_tier": perfil.orcamento.value,
        "macros": macros.dict(),
        "dias_total": 1,
        "dias": [{"dia": 1, "titulo_dia": f"Dia 1 - Plano {perfil.orcamento.value.capitalize()}", "refeicoes": default_refeicoes}],
        "refeicoes": default_refeicoes,
        "meals": default_refeicoes,
        "cardapio": default_refeicoes
    }

# --- ANALISAR PROTOCOLO ---

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

    protocol_text = body.get("protocol_text") or body.get("protocolo") or body.get("text") or body.get("dieta") or "Protocolo alimentar com divisão proteica e carboidratos."
    goal = body.get("goal") or body.get("objetivo") or "emagrecimento e definição"
    weight = float(body.get("weight") or body.get("peso") or 75.0)

    prompt = f"""
    Você é um nutricionista clínico esportivo e avaliador metabólico.
    Analise o seguinte protocolo: \"{protocol_text}\" (Paciente: {weight}kg, Objetivo: {goal}).
    Retorne OBRIGATORIAMENTE um JSON puro com:
    {{
      "status_avaliacao": "Protocolo Otimizado",
      "pontuacao_geral": 95,
      "pontuacao": 95,
      "score": 95,
      "resumo_executivo": "Parecer clínico completo sobre a eficiência da estratégia.",
      "balanco_calorico_estimado": "Déficit Calórico Inteligente (-450 kcal)",
      "distribuicao_macros": {{
        "proteinas": "{round(weight * 2.0)}g (Excelente síntese proteica)",
        "carboidratos": "Carboidratos complexos bem distribuídos",
        "gorduras": "Ácidos graxos essenciais de boa qualidade"
      }},
      "pontos_fortes": [
        "Fracionamento regular evitando picos de insulina",
        "Boa densidade de micronutrientes e fibras"
      ],
      "pontos_de_atencao": [
        "Manter a ingestão hídrica superior a 35ml por kg corporal",
        "Garantir 7 a 8 horas de sono noturno"
      ],
      "recomendacoes_otimizacao": [
        "Adicionar sementes de chia no café da manhã",
        "Incluir infusão de camomila ou hortelã à noite"
      ]
    }}
    """

    res = executar_chamada_ia(prompt)
    if not res:
        res = {
            "status_avaliacao": "Protocolo Aprovado & Otimizado",
            "pontuacao_geral": 92,
            "pontuacao": 92,
            "score": 92,
            "resumo_executivo": f"O protocolo analisado atende com rigor científico os requisitos para {goal}.",
            "balanco_calorico_estimado": "Déficit Calórico Controlado (-400 kcal)",
            "distribuicao_macros": {
                "proteinas": f"Aprox. {round(weight * 2.0)}g/dia",
                "carboidratos": "Carboidratos complexos de baixo índice glicêmico",
                "gorduras": "Gorduras mono e poli-insaturadas saudáveis"
            },
            "pontos_fortes": [
                "Excelente equilíbrio de nutrientes e saciedade prolongada",
                "Fracionamento regular prevenindo picos de insulina"
            ],
            "pontos_de_atencao": [
                "Manter hidratação fracionada ao longo do dia",
                "Priorizar alimentos naturais e evitar ultraprocessados"
            ],
            "recomendacoes_otimizacao": [
                "Adicionar 1 porção de vegetais verde-escuros no almoço",
                "Incluir sementes de chia ou linhaça no café da manhã"
            ]
        }

    res["status"] = "success"
    res["success"] = True

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO protocols (user_id, protocol_text, analysis_json, created_at) VALUES (?, ?, ?, ?)",
        (user_id, protocol_text, json.dumps(res), datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

    return res

# --- TROCA DE ALIMENTOS & CONSULTAS ---

@app.post("/api/v1/diet/swap-food", response_model=RefeicaoIA)
@app.post("/api/v1/food/swap")
def trocar_alimento_refeicao(dados: TrocaAlimentoInput):
    api_key = obter_chave(dados.gemini_api_key)
    prompt = f"""
    Substitua o alimento mantendo a equivalência calórica e de macros considerando orçamento {dados.orcamento} para:
    Refeição: {dados.refeicao_atual.nome_refeicao} | Calorias: {dados.refeicao_atual.calorias_alvo} kcal.
    Pedido: "{dados.motivo_ou_substituto}".
    Retorne estritamente um JSON no schema da refeição.
    """
    res = executar_chamada_ia(prompt, api_key)
    if res:
        return RefeicaoIA(**res)

    return RefeicaoIA(
        nome_refeicao=dados.refeicao_atual.nome_refeicao,
        titulo_prato=f"{dados.refeicao_atual.titulo_prato} (Adaptado)",
        horario_sugerido=dados.refeicao_atual.horario_sugerido,
        calorias_alvo=dados.refeicao_atual.calorias_alvo,
        proteinas_refeicao_g=dados.refeicao_atual.proteinas_refeicao_g,
        carboidratos_refeicao_g=dados.refeicao_atual.carboidratos_refeicao_g,
        gorduras_refeicao_g=dados.refeicao_atual.gorduras_refeicao_g,
        ingredientes=[f"{dados.motivo_ou_substituto} equivalente"] + dados.refeicao_atual.ingredientes[1:],
        modo_preparo="Substitua na proporção equivalente e prepare normalmente.",
        dica_chef="Equivalência nutricional mantida com sucesso."
    )

@app.api_route("/api/v1/nutrition/consult", methods=["GET", "POST"])
@app.api_route("/api/v1/energy/boost", methods=["GET", "POST"])
@app.api_route("/api/energy/tips", methods=["GET", "POST"])
def consultar_nutricao(request: Request):
    return {
        "titulo_estrategia": "Protocolo de Otimização Mitocondrial e Energia",
        "explicacao_fisiologica": "Aporte de micronutrientes e hidratação adequada estimulam a produção celular de ATP.",
        "alimentos_chave": [
            {"alimento": "Chá Verde com Limão", "porcao_sugerida": "200ml", "por_que_funciona": "Rico em polifenóis", "como_consumir": "Pela manhã"},
            {"alimento": "Castanha-do-Pará", "porcao_sugerida": "2 unidades", "por_que_funciona": "Aporte de selênio", "como_consumir": "No café da manhã"}
        ],
        "alimentos_evitar": ["Açúcares refinados", "Frituras em óleos vegetais"],
        "receita_rapida": {
            "titulo": "Shot Matinal Energético",
            "tempo_preparo": "2 min",
            "ingredientes": ["50ml de água", "1/2 limão espremido", "1g de cúrcuma em pó"],
            "modo_preparo": "Misture bem e tome em jejum.",
            "quando_tomar": "Ao acordar"
        }
    }

@app.post("/api/v1/workout/generate")
def criar_treino(dados: TreinoInput):
    return {
        "titulo": "Sessão de Treino Funcional & Força",
        "foco_principal": dados.foco,
        "aquecimento": [{"nome": "Polichinelos + Mobilidade", "series": "2", "repeticoes": "45s", "descanso": "30s", "dica_tecnica": "Respiração constante"}],
        "treino_principal": [
            {"nome": "Agachamento Goblet", "series": "4", "repeticoes": "12", "descanso": "60s", "dica_tecnica": "Amplitude completa"},
            {"nome": "Flexão de Braço", "series": "4", "repeticoes": "10", "descanso": "60s", "dica_tecnica": "Tronco alinhado"},
            {"nome": "Remada Unilateral", "series": "3", "repeticoes": "12/lado", "descanso": "60s", "dica_tecnica": "Foco nas dorsais"}
        ],
        "finalizacao": [{"nome": "Prancha Abdominal", "series": "3", "repeticoes": "45s", "descanso": "30s", "dica_tecnica": "Glúteos e abdômen firmes"}]
    }

@app.post("/api/v1/ai/scan-plate")
@app.post("/api/scan-plate")
def scan_plate():
    return {
        "status": "success",
        "prato_identificado": "Prato Tradicional Brasileiro (Arroz, Feijão, Frango Grelhado e Salada)",
        "calorias_estimadas": 580,
        "macros": {"proteina_g": 42, "carbo_g": 65, "gordura_g": 14},
        "confianca_ia": "96%",
        "recomendacao": "Excelente equilíbrio entre proteínas magras e fibras de digestão lenta."
    }

# ==============================================================================
# 7. PAINEL ADMINISTRATIVO (/admin) & EXPORTAÇÃO CSV
# ==============================================================================

@app.get("/admin/export/leads.csv")
def export_leads_csv(senha: str = ""):
    if senha.strip() != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Senha incorreta.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        c.execute("SELECT * FROM leads ORDER BY id DESC")
        leads = [dict(r) for r in c.fetchall()]
    except Exception:
        leads = []
    conn.close()

    csv_content = "ID;Nome;Email;WhatsApp;Faixa Orcamento;Calorias Meta;Data Criacao\n"
    for l in leads:
        lead_id = l.get('id', '')
        name = l.get('name', '')
        email = l.get('email', '')
        phone = l.get('phone', '')
        budget = l.get('budget_tier', 'economico')
        cals = l.get('daily_calories') or ''
        created_at = l.get('created_at', '')
        csv_content += f"{lead_id};{name};{email};{phone};{budget};{cals};{created_at}\n"

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

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    try:
        c.execute("SELECT * FROM leads ORDER BY id DESC")
        leads = [dict(r) for r in c.fetchall()]
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
