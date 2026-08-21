import os
import re
import json
import math
import base64
import hashlib
import sqlite3
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

# ==========================================
# CONFIGURAÇÕES E VARIÁVEIS DE AMBIENTE
# ==========================================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "nutricore.db"

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "nutricore2026")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MERCADO_PAGO_TOKEN = os.getenv("MERCADO_PAGO_ACCESS_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

app = FastAPI(
    title="NutriCore Pro - Enterprise Nutrition Engine",
    description="Backend completo para SaaS de Nutrição, IA, Leads e Pagamentos",
    version="3.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# INICIALIZAÇÃO DO BANCO DE DADOS
# ==========================================
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    
    # 1. Usuários
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_pro INTEGER DEFAULT 0,
            role TEXT DEFAULT 'user',
            created_at TEXT NOT NULL,
            last_login TEXT
        )
    """)
    
    # 2. Perfis Corporais e Histórico de Peso
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            gender TEXT,
            age INTEGER,
            weight REAL,
            target_weight REAL,
            height REAL,
            activity_level TEXT,
            goal TEXT,
            diet_style TEXT,
            restrictions TEXT,
            tmb REAL,
            tdee REAL,
            daily_calories REAL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    
    # 3. Leads do Quiz (Funil de Vendas)
    c.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT NOT NULL,
            gender TEXT,
            age INTEGER,
            current_weight REAL,
            target_weight REAL,
            height REAL,
            goal TEXT,
            activity_level TEXT,
            diet_style TEXT,
            tmb REAL,
            daily_calories REAL,
            estimated_weeks INTEGER,
            quiz_data_json TEXT,
            recovered_via_whatsapp INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    
    # 4. Planos Alimentares
    c.execute("""
        CREATE TABLE IF NOT EXISTS diet_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_email TEXT,
            title TEXT,
            calories REAL,
            macros_json TEXT,
            meals_json TEXT,
            shopping_list_json TEXT,
            ai_tips TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    
    # 5. Planos de Treino Complementares
    c.execute("""
        CREATE TABLE IF NOT EXISTS workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            goal TEXT,
            level TEXT,
            workout_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    
    # 6. Cobranças e Pagamentos Pix
    c.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT UNIQUE NOT NULL,
            user_email TEXT NOT NULL,
            user_name TEXT,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            qr_code TEXT,
            qr_code_base64 TEXT,
            plan_type TEXT DEFAULT 'pro_annual',
            paid_at TEXT,
            created_at TEXT NOT NULL
        )
    """)
    
    conn.commit()
    conn.close()

init_db()

# ==========================================
# MODELOS PYDANTIC
# ==========================================
class UserRegisterInput(BaseModel):
    name: str = Field(..., min_length=2)
    email: EmailStr
    password: str = Field(..., min_length=6)

class UserLoginInput(BaseModel):
    email: EmailStr
    password: str

class ProfileUpdateInput(BaseModel):
    user_email: EmailStr
    gender: str
    age: int
    weight: float
    target_weight: float
    height: float
    activity_level: str
    goal: str
    diet_style: Optional[str] = "equilibrada"
    restrictions: Optional[List[str]] = []

class LeadCaptureInput(BaseModel):
    name: str
    email: EmailStr
    phone: str
    gender: Optional[str] = "masculino"
    age: Optional[int] = 28
    weight: Optional[float] = 75.0
    target_weight: Optional[float] = 70.0
    height: Optional[float] = 175.0
    goal: Optional[str] = "emagrecimento"
    activity_level: Optional[str] = "moderado"
    diet_style: Optional[str] = "flexivel"

class GeneratePlanInput(BaseModel):
    user_email: Optional[str] = ""
    gender: str
    age: int
    weight: float
    height: float
    activity_level: str
    goal: str
    diet_style: Optional[str] = "mediterranea"
    restrictions: Optional[List[str]] = []
    book_reference: Optional[str] = "Diretrizes Clínicas de Nutrição e Fisiologia Metabólica"

class ImageScanInput(BaseModel):
    image_base64: str
    notes: Optional[str] = ""

class WorkoutGenerateInput(BaseModel):
    goal: str
    days_per_week: int = 4
    location: str = "academia"  # academia | casa

class PixCreateInput(BaseModel):
    email: EmailStr
    name: str
    amount: float = 29.90
    plan_type: Optional[str] = "pro_annual"

# ==========================================
# UTILITÁRIOS CIENTÍFICOS & AUXILIARES
# ==========================================
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def calculate_metabolism(gender: str, weight: float, height: float, age: int, activity: str, goal: str):
    # Fórmula de Mifflin-St Jeor
    is_male = gender.lower() in ["homem", "male", "m", "masculino"]
    if is_male:
        tmb = (10.0 * weight) + (6.25 * height) - (5.0 * age) + 5.0
    else:
        tmb = (10.0 * weight) + (6.25 * height) - (5.0 * age) - 161.0

    factors = {
        "sedentario": 1.2,
        "leve": 1.375,
        "moderado": 1.55,
        "intenso": 1.725,
        "muito_intenso": 1.9
    }
    tdee = tmb * factors.get(activity.lower(), 1.4)

    # Ajuste por objetivo
    goal_lower = goal.lower()
    if any(k in goal_lower for k in ["perda", "emagrecer", "secar", "definir"]):
        target_calories = tdee - 500
    elif any(k in goal_lower for k in ["ganho", "hipertrofia", "massa"]):
        target_calories = tdee + 400
    else:
        target_calories = tdee

    return round(tmb, 1), round(tdee, 1), round(target_calories, 1)

def generate_deterministic_plan(weight: float, calories: float, goal: str, diet_style: str):
    # Distribuição de Macronutrientes
    prot_g = round(weight * 2.0)
    fat_g = round((calories * 0.25) / 9.0)
    carb_g = round(max(50, (calories - (prot_g * 4 + fat_g * 9)) / 4.0))

    water_liters = round((weight * 35) / 1000.0, 1)

    meals = [
        {
            "nome": "Café da Manhã Energético",
            "horario": "07:30",
            "calorias": round(calories * 0.25),
            "alimentos": [
                "3 Ovos inteiros mexidos com azeite de oliva",
                "2 Fatias de pão 100% integral",
                "1 Fruta média (Banana ou Maçã)",
                "Café preto sem açúcar"
            ],
            "dica_preparo": "Consuma proteína logo na primeira refeição para estabilizar a glicemia."
        },
        {
            "nome": "Almoço Anabólico & Equilibrado",
            "horario": "12:30",
            "calorias": round(calories * 0.35),
            "alimentos": [
                "150g de Peito de Frango ou Patinho grelhado",
                "120g de Arroz Integral ou Mandioca",
                "1 Concha de Feijão preto ou carioca (80g)",
                "Prato fundo de folhas verdes (Rúcula, Alface) e Tomate",
                "1 Fio de Azeite de Oliva Extra Virgem (5ml)"
            ],
            "dica_preparo": "Adicione limão espremido na salada para aumentar a absorção de ferro."
        },
        {
            "nome": "Lanche da Tarde Pré-Treino",
            "horario": "16:30",
            "calorias": round(calories * 0.15),
            "alimentos": [
                "1 Pote de Iogurte Natural Desnatado (170g)",
                "30g de Aveia em flocos finos",
                "1 Colher de sopa de sementes de Chia",
                "1 Porção de frutas vermelhas ou morangos"
            ],
            "dica_preparo": "Excelente combinação de carboidrato complexo e probióticos."
        },
        {
            "nome": "Jantar Regenerativo",
            "horario": "20:00",
            "calorias": round(calories * 0.25),
            "alimentos": [
                "140g de Peixe (Tilápia/Salmão) ou Sobrecoxa sem pele",
                "150g de Legumes ao vapor (Brócolis, Cenoura, Abobrinha)",
                "100g de Batata Doce cozida",
                "Chá de Camomila sem açúcar antes de dormir"
            ],
            "dica_preparo": "Evite excesso de sódio à noite para reduzir retenção hídrica."
        }
    ]

    shopping_list = {
        "Hortifrúti": ["Folhas verdes", "Tomates", "Brócolis", "Cenoura", "Bananas", "Morangos", "Limão"],
        "Proteínas & Ovos": ["Ovos caipiras (2 dúzias)", "Peito de Frango (1kg)", "Patinho moído (500g)", "Filé de Peixe"],
        "Mercearia & Grãos": ["Arroz integral", "Feijão", "Pão 100% integral", "Aveia em flocos", "Chia", "Azeite EV"],
        "Laticínios": ["Iogurte natural desnatado"]
    }

    return {
        "calorias_totais": calories,
        "macros": {
            "proteina_g": prot_g,
            "carbo_g": carb_g,
            "gordura_g": fat_g,
            "fibras_g": 30
        },
        "meta_hidratacao": f"{water_liters} Litros de água/dia",
        "estilo_aplicado": diet_style,
        "refeicoes": meals,
        "lista_compras": shopping_list,
        "diretrizes_metabolicas": [
            "Mantenha intervalos de 3 a 4 horas entre as principais refeições.",
            "Consuma 500ml de água logo ao acordar para ativar o trato gastrointestinal.",
            "Evite ultraprocessados, óleos vegetais refinados e refrigerantes açucarados."
        ]
    }

# ==========================================
# ROTAS DE FRONTEND & PWA
# ==========================================
@app.get("/", response_class=FileResponse)
def serve_index():
    path = BASE_DIR / "index.html"
    if path.exists():
        return FileResponse(path)
    return HTMLResponse("<h2>NutriCore Pro Online. Adicione index.html na raiz do projeto.</h2>")

@app.get("/quiz", response_class=FileResponse)
def serve_quiz():
    path = BASE_DIR / "quiz.html"
    if path.exists():
        return FileResponse(path)
    return HTMLResponse("<h2>Quiz NutriCore Pro Online. Adicione quiz.html na raiz do projeto.</h2>")

@app.get("/manifest.json")
def serve_manifest():
    return {
        "name": "NutriCore Pro",
        "short_name": "NutriCore",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": "#22c55e",
        "icons": [
            {
                "src": "https://cdn-icons-png.flaticon.com/512/2965/2965567.png",
                "sizes": "512x512",
                "type": "image/png"
            }
        ]
    }

@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "NutriCore Pro API",
        "timestamp": datetime.utcnow().isoformat(),
        "gemini_active": bool(GEMINI_API_KEY),
        "mercadopago_active": bool(MERCADO_PAGO_TOKEN)
    }

# ==========================================
# AUTENTICAÇÃO E PERFIL DO USUÁRIO
# ==========================================
@app.post("/api/v1/auth/register")
def register_user(data: UserRegisterInput):
    conn = get_db()
    c = conn.cursor()
    try:
        now = datetime.utcnow().isoformat()
        pwd_hash = hash_password(data.password)
        c.execute(
            "INSERT INTO users (name, email, password_hash, created_at, last_login) VALUES (?, ?, ?, ?, ?)",
            (data.name.strip(), data.email.lower().strip(), pwd_hash, now, now)
        )
        conn.commit()
        user_id = c.lastrowid
        conn.close()
        return {
            "status": "success",
            "message": "Usuário registrado com sucesso.",
            "user": {
                "id": user_id,
                "name": data.name.strip(),
                "email": data.email.lower().strip(),
                "is_pro": False
            }
        }
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Este e-mail já está cadastrado no sistema.")

@app.post("/api/v1/auth/login")
def login_user(data: UserLoginInput):
    conn = get_db()
    c = conn.cursor()
    pwd_hash = hash_password(data.password)
    c.execute(
        "SELECT id, name, email, is_pro FROM users WHERE email = ? AND password_hash = ?",
        (data.email.lower().strip(), pwd_hash)
    )
    user = c.fetchone()
    
    if not user:
        conn.close()
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")
        
    c.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.utcnow().isoformat(), user["id"]))
    conn.commit()
    conn.close()
    
    return {
        "status": "success",
        "user": {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
            "is_pro": bool(user["is_pro"])
        }
    }

@app.post("/api/v1/profile/sync")
def sync_profile(profile: ProfileUpdateInput):
    tmb, tdee, daily_cal = calculate_metabolism(
        profile.gender, profile.weight, profile.height, profile.age, profile.activity_level, profile.goal
    )
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email = ?", (profile.user_email.lower().strip(),))
    user = c.fetchone()
    user_id = user["id"] if user else None

    now = datetime.utcnow().isoformat()
    c.execute("""
        INSERT INTO user_profiles (user_id, gender, age, weight, target_weight, height, activity_level, goal, diet_style, restrictions, tmb, tdee, daily_calories, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            gender=excluded.gender,
            age=excluded.age,
            weight=excluded.weight,
            target_weight=excluded.target_weight,
            height=excluded.height,
            activity_level=excluded.activity_level,
            goal=excluded.goal,
            diet_style=excluded.diet_style,
            restrictions=excluded.restrictions,
            tmb=excluded.tmb,
            tdee=excluded.tdee,
            daily_calories=excluded.daily_calories,
            updated_at=excluded.updated_at
    """, (
        user_id, profile.gender, profile.age, profile.weight, profile.target_weight, profile.height,
        profile.activity_level, profile.goal, profile.diet_style, json.dumps(profile.restrictions),
        tmb, tdee, daily_cal, now
    ))
    conn.commit()
    conn.close()
    
    return {
        "status": "success",
        "tmb": tmb,
        "tdee": tdee,
        "daily_calories": daily_cal,
        "message": "Perfil atualizado e sincronizado na nuvem."
    }

# ==========================================
# FUNIL DE QUIZ & RECUPERAÇÃO DE LEADS
# ==========================================
@app.post("/api/v1/lead/capture")
def capture_quiz_lead(lead: LeadCaptureInput):
    tmb, tdee, daily_cal = calculate_metabolism(
        lead.gender, lead.weight, lead.height, lead.age, lead.activity_level, lead.goal
    )

    weight_diff = abs(lead.weight - lead.target_weight)
    weeks_estimate = max(2, math.ceil(weight_diff / 0.7))

    clean_phone = re.sub(r'\D', '', lead.phone)
    if not clean_phone.startswith('55'):
        clean_phone = '55' + clean_phone

    wpp_message = (
        f"Olá {lead.name}! Analisei suas respostas no NutriCore Pro.\n"
        f"Sua meta de {lead.target_weight}kg é totalmente atingível em aproximadamente {weeks_estimate} semanas.\n"
        f"Seu diagnóstico metabólico está pronto: https://nutricore-app-1.onrender.com"
    )
    whatsapp_recovery_url = f"https://wa.me/{clean_phone}?text={requests.utils.quote(wpp_message)}"

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO leads (name, email, phone, gender, age, current_weight, target_weight, height, goal, activity_level, diet_style, tmb, daily_calories, estimated_weeks, quiz_data_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        lead.name.strip(), lead.email.lower().strip(), lead.phone.strip(), lead.gender, lead.age,
        lead.weight, lead.target_weight, lead.height, lead.goal, lead.activity_level, lead.diet_style,
        tmb, daily_cal, weeks_estimate, json.dumps(lead.dict()), datetime.utcnow().isoformat()
    ))
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "tmb": tmb,
        "tdee": tdee,
        "daily_calories": daily_cal,
        "estimated_weeks": weeks_estimate,
        "recovery_whatsapp_url": whatsapp_recovery_url,
        "message": f"Diagnóstico gerado para {lead.name} com sucesso."
    }

# ==========================================
# MOTOR DE IA (GEMINI NUTRITION & SCANNER)
# ==========================================
@app.post("/api/v1/plan/generate")
def generate_plan(data: GeneratePlanInput):
    tmb, tdee, target_calories = calculate_metabolism(
        data.gender, data.weight, data.height, data.age, data.activity_level, data.goal
    )

    if GEMINI_API_KEY:
        try:
            from google import genai
            client = genai.Client(api_key=GEMINI_API_KEY)
            
            prompt = f"""
            Você é um nutricionista clínico de elite baseado estritamente na obra e diretrizes: "{data.book_reference}".
            Gere um plano alimentar de 1 dia perfeito, ultra detalhado e estruturado em JSON para o paciente abaixo:
            - Sexo: {data.gender}, Idade: {data.age}, Peso: {data.weight}kg, Altura: {data.height}cm
            - Objetivo: {data.goal}
            - Meta Calórica Alvo: {target_calories} kcal (TMB: {tmb} kcal, Gasto Total: {tdee} kcal)
            - Estilo de Alimentação: {data.diet_style}
            - Restrições: {', '.join(data.restrictions) if data.restrictions else 'Nenhuma'}

            Retorne OBRIGATORIAMENTE um JSON puro seguindo este schema exato:
            {{
              "calorias_totais": {target_calories},
              "macros": {{
                "proteina_g": 140,
                "carbo_g": 180,
                "gordura_g": 50,
                "fibras_g": 32
              }},
              "meta_hidratacao": "2.8 Litros/dia",
              "estilo_aplicado": "{data.diet_style}",
              "refeicoes": [
                {{
                  "nome": "Café da Manhã",
                  "horario": "07:30",
                  "calorias": 400,
                  "alimentos": ["3 ovos mexidos", "2 fatias de pão integral", "1 xícara de café preto"],
                  "dica_preparo": "Evite açúcar refinado."
                }},
                {{
                  "nome": "Almoço",
                  "horario": "12:30",
                  "calorias": 650,
                  "alimentos": ["150g de frango", "120g de arroz", "80g feijão", "Salada à vontade"],
                  "dica_preparo": "Tempere com azeite e ervas."
                }},
                {{
                  "nome": "Lanche da Tarde",
                  "horario": "16:30",
                  "calorias": 250,
                  "alimentos": ["1 pote de iogurte", "30g aveia", "1 maçã"],
                  "dica_preparo": "Rico em fibras."
                }},
                {{
                  "nome": "Jantar",
                  "horario": "20:00",
                  "calorias": 500,
                  "alimentos": ["140g de peixe grelhado", "150g batata doce", "Legumes cozidos"],
                  "dica_preparo": "Refeição leve para favorecer o sono."
                }}
              ],
              "lista_compras": {{
                "Hortifrúti": ["Maçã", "Legumes variados", "Folhas verdes"],
                "Proteínas & Ovos": ["Ovos", "Peito de Frango", "Filé de Tilápia"],
                "Mercearia & Grãos": ["Pão integral", "Arroz", "Feijão", "Aveia", "Azeite"],
                "Laticínios": ["Iogurte Natural"]
              }},
              "diretrizes_metabolicas": [
                "Mastigue devagar para favorecer a sinalização de saciedade via leptina.",
                "Mantenha a ingestão hídrica constante entre as refeições."
              ]
            }}
            """
            
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config={'response_mime_type': 'application/json'}
            )
            parsed_plan = json.loads(response.text)

            # Salva o plano no banco
            conn = get_db()
            c = conn.cursor()
            c.execute("""
                INSERT INTO diet_plans (user_email, title, calories, macros_json, meals_json, shopping_list_json, ai_tips, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.user_email, f"Plano {data.goal}", target_calories,
                json.dumps(parsed_plan.get("macros")), json.dumps(parsed_plan.get("refeicoes")),
                json.dumps(parsed_plan.get("lista_compras")), json.dumps(parsed_plan.get("diretrizes_metabolicas")),
                datetime.utcnow().isoformat()
            ))
            conn.commit()
            conn.close()

            return parsed_plan
        except Exception as e:
            pass

    # Fallback determinístico caso o Gemini não responda
    return generate_deterministic_plan(data.weight, target_calories, data.goal, data.diet_style)

@app.post("/api/v1/ai/scan-plate")
def scan_food_plate(data: ImageScanInput):
    if not GEMINI_API_KEY:
        return {
            "status": "mock",
            "prato_identificado": "Prato Tradicional Brasileiro (Arroz, Feijão, Frango e Salada)",
            "calorias_estimadas": 580,
            "macros": {"proteina_g": 42, "carbo_g": 65, "gordura_g": 14},
            "confianca_ia": "94%",
            "recomendacao": "Excelente equilíbrio entre proteínas magras e fibras."
        }

    try:
        from google import genai
        from google.genai import types
        
        client = genai.Client(api_key=GEMINI_API_KEY)
        img_bytes = base64.b64decode(data.image_base64.split(",")[-1])
        
        prompt = """
        Analise a imagem deste prato de comida e retorne um JSON com:
        {
          "prato_identificado": "Nome dos alimentos detectados",
          "calorias_estimadas": 550,
          "macros": {"proteina_g": 38, "carbo_g": 60, "gordura_g": 12},
          "confianca_ia": "95%",
          "alimentos_detectados": ["150g Frango Grelhado", "100g Arroz", "80g Feijão", "Salada"],
          "recomendacao": "Feedback nutricional sobre o prato."
        }
        """
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                types.Part.from_bytes(data=img_bytes, mime_type='image/jpeg'),
                prompt
            ],
            config={'response_mime_type': 'application/json'}
        )
        return json.loads(response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar imagem via IA: {str(e)}")

@app.post("/api/v1/workout/generate")
def generate_workout(data: WorkoutGenerateInput):
    # Prescrição de treino alinhada ao objetivo
    treinos = {
        "hipertrofia": [
            {"dia": "Segunda-feira", "foco": "Peito, Ombros e Tríceps", "exercicios": ["Supino Reto 4x10", "Desenvolvimento Halteres 3x12", "Tríceps Corda 4x12"]},
            {"dia": "Terça-feira", "foco": "Costas e Bíceps", "exercicios": ["Puxada Alta 4x10", "Remada Curvada 4x10", "Rosca Direta 3x12"]},
            {"dia": "Quinta-feira", "foco": "Pernas Completo", "exercicios": ["Agachamento Livre 4x8", "Leg Press 4x12", "Cadeira Extensora 3x15"]},
            {"dia": "Sexta-feira", "foco": "Ombros e Abdômen", "exercicios": ["Elevação Lateral 4x15", "Prancha 3x1min", "Abdominal Infra 3x20"]}
        ],
        "emagrecimento": [
            {"dia": "Segunda-feira", "foco": "Full Body Funcional + HIIT", "exercicios": ["Agachamento com Salto 4x15", "Flexão de Braço 4x12", "Burpees 3x45s"]},
            {"dia": "Quarta-feira", "foco": "Membros Inferiores & Cardio", "exercicios": ["Passada com Halteres 4x12", "Stiff 4x12", "20min Esteira Inclinada"]},
            {"dia": "Sexta-feira", "foco": "Tronco & Core Metabólico", "exercicios": ["Remada Unilateral 4x12", "Prancha Dinâmica 4x45s", "Mountain Climbers 4x40s"]}
        ]
    }
    return {
        "status": "success",
        "objetivo": data.goal,
        "divisao": treinos.get(data.goal.lower(), treinos["emagrecimento"])
    }

# ==========================================
# PAGAMENTOS PIX (MERCADO PAGO & WEBHOOK)
# ==========================================
@app.post("/api/v1/pix/create")
def create_pix(payment: PixCreateInput):
    now = datetime.utcnow().isoformat()
    
    if MERCADO_PAGO_TOKEN:
        try:
            headers = {
                "Authorization": f"Bearer {MERCADO_PAGO_TOKEN}",
                "Content-Type": "application/json"
            }
            body = {
                "transaction_amount": float(payment.amount),
                "description": f"NutriCore Pro - {payment.plan_type}",
                "payment_method_id": "pix",
                "payer": {
                    "email": payment.email,
                    "first_name": payment.name.split()[0] if payment.name else "Cliente"
                }
            }
            res = requests.post("https://api.mercadopago.com/v1/payments", headers=headers, json=body)
            if res.status_code in [200, 201]:
                res_data = res.json()
                tx_data = res_data.get("point_of_interaction", {}).get("transaction_data", {})
                
                pay_id = str(res_data.get("id"))
                qr_code = tx_data.get("qr_code")
                qr_base64 = tx_data.get("qr_code_base64")

                conn = get_db()
                c = conn.cursor()
                c.execute("""
                    INSERT INTO payments (payment_id, user_email, user_name, amount, status, qr_code, qr_code_base64, plan_type, created_at)
                    VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """, (pay_id, payment.email, payment.name, payment.amount, qr_code, qr_base64, payment.plan_type, now))
                conn.commit()
                conn.close()

                return {
                    "status": "success",
                    "payment_id": pay_id,
                    "qr_code": qr_code,
                    "qr_code_base64": qr_base64
                }
        except Exception:
            pass

    # Modo Sandbox / Demonstração
    fake_id = f"PIX-{int(datetime.utcnow().timestamp())}"
    fake_qr = "00020126580014br.gov.bcb.pix0136nutricore-pro-acesso-anual520400005303986540529.905802BR5913NutriCore Pro6009Sao Paulo62070503***6304E2CA"

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO payments (payment_id, user_email, user_name, amount, status, qr_code, qr_code_base64, plan_type, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?, '', ?, ?)
    """, (fake_id, payment.email, payment.name, payment.amount, fake_qr, payment.plan_type, now))
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "payment_id": fake_id,
        "qr_code": fake_qr,
        "qr_code_base64": ""
    }

@app.get("/api/v1/pix/status/{payment_id}")
def check_pix_status(payment_id: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT status, user_email FROM payments WHERE payment_id = ?", (payment_id,))
    pay = c.fetchone()
    conn.close()

    if not pay:
        return {"status": "not_found"}

    return {"status": pay["status"], "user_email": pay["user_email"]}

@app.post("/api/v1/webhooks/mercadopago")
async def mercadopago_webhook(request: Request):
    try:
        data = await request.json()
        if data.get("action") == "payment.updated" or data.get("type") == "payment":
            payment_id = data.get("data", {}).get("id")
            if payment_id and MERCADO_PAGO_TOKEN:
                headers = {"Authorization": f"Bearer {MERCADO_PAGO_TOKEN}"}
                res = requests.get(f"https://api.mercadopago.com/v1/payments/{payment_id}", headers=headers)
                if res.status_code == 200:
                    payment_data = res.json()
                    if payment_data.get("status") == "approved":
                        user_email = payment_data.get("payer", {}).get("email")
                        now = datetime.utcnow().isoformat()
                        
                        conn = get_db()
                        c = conn.cursor()
                        c.execute("UPDATE payments SET status = 'approved', paid_at = ? WHERE payment_id = ?", (now, str(payment_id)))
                        if user_email:
                            c.execute("UPDATE users SET is_pro = 1 WHERE email = ?", (user_email.lower().strip(),))
                        conn.commit()
                        conn.close()
        return {"status": "ok"}
    except Exception:
        return {"status": "error"}

# ==========================================
# PAINEL ADMINISTRATIVO & EXPORTAÇÃO CSV
# ==========================================
@app.get("/admin/export/leads.csv")
def export_leads_csv(senha: str = ""):
    if senha != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Senha incorreta.")
        
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, name, email, phone, goal, target_weight, current_weight, daily_calories, created_at FROM leads ORDER BY id DESC")
    leads = c.fetchall()
    conn.close()

    csv_content = "ID;Nome;Email;WhatsApp;Objetivo;Peso Atual;Meta Peso;Meta Calorica;Data Criacao\n"
    for l in leads:
        csv_content += f"{l['id']};{l['name']};{l['email']};{l['phone']};{l['goal']};{l['current_weight']};{l['target_weight']};{l['daily_calories']};{l['created_at']}\n"

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads_nutricore.csv"}
    )

@app.get("/admin", response_class=HTMLResponse)
def admin_portal(senha: str = ""):
    if senha != ADMIN_PASSWORD:
        return f"""
        <!DOCTYPE html>
        <html lang="pt-BR">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Admin - NutriCore Pro</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0b0f19; color: #f8fafc; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
                .card {{ background: #111827; padding: 2.5rem; border-radius: 1rem; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5); width: 100%; max-width: 380px; border: 1px solid #1f2937; text-align: center; }}
                h2 {{ margin-top: 0; color: #10b981; }}
                input {{ width: 100%; padding: 0.85rem; border-radius: 0.5rem; border: 1px solid #374151; background: #030712; color: white; margin: 1.2rem 0; box-sizing: border-box; font-size: 1rem; }}
                button {{ width: 100%; padding: 0.85rem; border-radius: 0.5rem; border: none; background: #10b981; color: white; font-weight: bold; cursor: pointer; font-size: 1rem; }}
            </style>
        </head>
        <body>
            <div class="card">
                <h2>⚡ NutriCore Admin</h2>
                <p style="color: #9ca3af; font-size: 0.9rem;">Insira sua chave mestra para acessar os dados.</p>
                <form method="get" action="/admin">
                    <input type="password" name="senha" placeholder="Senha Administrador" required autofocus>
                    <button type="submit">Entrar no Dashboard</button>
                </form>
            </div>
        </body>
        </html>
        """

    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT * FROM leads ORDER BY id DESC")
    leads = c.fetchall()

    c.execute("SELECT COUNT(*) as count FROM users")
    total_users = c.fetchone()["count"]

    c.execute("SELECT COUNT(*) as count FROM users WHERE is_pro = 1")
    total_pro = c.fetchone()["count"]

    c.execute("SELECT SUM(amount) as total FROM payments WHERE status = 'approved'")
    revenue_row = c.fetchone()
    total_revenue = revenue_row["total"] if revenue_row["total"] else 0.0

    conn.close()

    conversion_rate = round((len(leads) / max(1, total_users)) * 100, 1)

    rows = ""
    for l in leads:
        clean_phone = re.sub(r'\D', '', l['phone'])
        if not clean_phone.startswith('55'):
            clean_phone = '55' + clean_phone
        msg = f"Olá {l['name']}, tudo bem? Vi seu diagnóstico metabólico no NutriCore Pro. Gostaria de tirar alguma dúvida sobre o plano?"
        wpp_url = f"https://wa.me/{clean_phone}?text={requests.utils.quote(msg)}"

        rows += f"""
        <tr style="border-bottom: 1px solid #1f2937;">
            <td style="padding: 12px; color: #9ca3af;">#{l['id']}</td>
            <td style="padding: 12px; font-weight: 600;">{l['name']}</td>
            <td style="padding: 12px; color: #cbd5e1;">{l['email']}</td>
            <td style="padding: 12px;">
                <a href="{wpp_url}" target="_blank" style="background: #064e3b; color: #34d399; padding: 6px 12px; border-radius: 6px; text-decoration: none; font-size: 0.85rem; font-weight: bold; display: inline-flex; align-items: center; gap: 4px;">
                    💬 {l['phone']}
                </a>
            </td>
            <td style="padding: 12px;"><span style="background: #1e293b; padding: 4px 8px; border-radius: 4px; font-size: 0.85rem;">{l['goal'] or 'Geral'}</span></td>
            <td style="padding: 12px; color: #38bdf8; font-weight: bold;">{l['daily_calories'] or '-'} kcal</td>
            <td style="padding: 12px; color: #9ca3af; font-size: 0.85rem;">{l['created_at'][:16].replace('T', ' ')}</td>
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
                    <p style="color: #9ca3af; margin: 5px 0 0 0; font-size: 0.9rem;">Visão em tempo real de conversão de leads e faturamento.</p>
                </div>
                <div style="display: flex; gap: 12px; align-items: center;">
                    <a href="/admin/export/leads.csv?senha={senha}" class="btn-csv">📥 Baixar Planilha CSV</a>
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
                            <th>Objetivo</th>
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
