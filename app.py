import os
import re
import json
import math
import base64
import hashlib
import sqlite3
import urllib.parse
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

import requests
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ==========================================
# CONFIGURAÇÕES E VARIÁVEIS DE AMBIENTE
# ==========================================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "nutricore.db"

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "nutricore2026")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MERCADO_PAGO_TOKEN = os.getenv("MERCADO_PAGO_ACCESS_TOKEN", "")

app = FastAPI(
    title="NutriCore Pro - Enterprise Nutrition Engine",
    description="Backend completo para SaaS de Nutrição, IA, Leads e Pagamentos",
    version="3.0.1"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# BANCO DE DADOS LOCAL (SQLITE)
# ==========================================
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    
    # Usuários
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
    
    # Perfis de Usuário
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
    
    # Leads do Quiz (Funil)
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
            created_at TEXT NOT NULL
        )
    """)
    
    # Planos Alimentares
    c.execute("""
        CREATE TABLE IF NOT EXISTS diet_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT,
            title TEXT,
            calories REAL,
            macros_json TEXT,
            meals_json TEXT,
            shopping_list_json TEXT,
            ai_tips TEXT,
            created_at TEXT NOT NULL
        )
    """)
    
    # Pagamentos Pix
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
# SCHEMAS PYDANTIC (COMPATÍVEIS SEM DEPENDÊNCIAS EXTRAS)
# ==========================================
class UserRegisterInput(BaseModel):
    name: str
    email: str
    password: str

class UserLoginInput(BaseModel):
    email: str
    password: str

class ProfileUpdateInput(BaseModel):
    user_email: str
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
    email: str
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
    location: str = "academia"

class PixCreateInput(BaseModel):
    email: str
    name: str
    amount: float = 29.90
    plan_type: Optional[str] = "pro_annual"

# ==========================================
# CÁLCULOS NUTRICIONAIS
# ==========================================
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def calculate_metabolism(gender: str, weight: float, height: float, age: int, activity: str, goal: str):
    is_male = str(gender).lower() in ["homem", "male", "m", "masculino"]
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
    tdee = tmb * factors.get(str(activity).lower(), 1.4)

    goal_lower = str(goal).lower()
    if any(k in goal_lower for k in ["perda", "emagrecer", "secar", "definir"]):
        target_calories = tdee - 500
    elif any(k in goal_lower for k in ["ganho", "hipertrofia", "massa"]):
        target_calories = tdee + 400
    else:
        target_calories = tdee

    return round(tmb, 1), round(tdee, 1), round(target_calories, 1)

def generate_fallback_plan(weight: float, calories: float, goal: str, diet_style: str):
    prot_g = round(weight * 2.0)
    fat_g = round((calories * 0.25) / 9.0)
    carb_g = round(max(50, (calories - (prot_g * 4 + fat_g * 9)) / 4.0))
    water_liters = round((weight * 35) / 1000.0, 1)

    return {
        "calorias_totais": calories,
        "macros": {
            "proteina_g": prot_g,
            "carbo_g": carb_g,
            "gordura_g": fat_g,
            "fibras_g": 30
        },
        "meta_hidratacao": f"{water_liters} Litros/dia",
        "estilo_aplicado": diet_style,
        "refeicoes": [
            {
                "nome": "Café da Manhã",
                "horario": "07:30",
                "calorias": round(calories * 0.25),
                "alimentos": ["3 Ovos mexidos", "2 Fatias de pão 100% integral", "1 Banana", "Café preto sem açúcar"],
                "dica_preparo": "Consuma proteína logo pela manhã para manter a saciedade."
            },
            {
                "nome": "Almoço Equilibrado",
                "horario": "12:30",
                "calorias": round(calories * 0.35),
                "alimentos": ["150g de Peito de Frango grelhado", "120g de Arroz Integral", "80g de Feijão", "Salada verde à vontade", "1 Fio de azeite"],
                "dica_preparo": "Adicione limão à salada para favorecer a digestão."
            },
            {
                "nome": "Lanche da Tarde",
                "horario": "16:30",
                "calorias": round(calories * 0.15),
                "alimentos": ["1 Pote de Iogurte Natural (170g)", "30g de Aveia em flocos", "Morangos picados"],
                "dica_preparo": "Opção rica em fibras e carboidratos complexos."
            },
            {
                "nome": "Jantar Leve",
                "horario": "20:00",
                "calorias": round(calories * 0.25),
                "alimentos": ["140g de Peixe ou Patinho moído", "150g de Legumes ao vapor (Brócolis/Cenoura)", "100g de Batata Doce"],
                "dica_preparo": "Evite excesso de sal e gorduras à noite."
            }
        ],
        "lista_compras": {
            "Hortifrúti": ["Folhas verdes", "Tomates", "Brócolis", "Cenoura", "Bananas", "Morangos"],
            "Proteínas": ["Ovos (2 dúzias)", "Peito de Frango (1kg)", "Patinho moído (500g)", "Tilápia"],
            "Mercearia": ["Arroz integral", "Feijão", "Pão integral", "Aveia", "Azeite Extra Virgem"],
            "Laticínios": ["Iogurte Natural"]
        },
        "diretrizes_metabolicas": [
            "Beba água fracionada ao longo do dia.",
            "Priorize alimentos integrais e reduza açúcares refinados."
        ]
    }

# ==========================================
# ROTAS DE PÁGINAS (FRONTEND)
# ==========================================
@app.get("/", response_class=FileResponse)
def serve_index():
    index_file = BASE_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("<h2>NutriCore Pro Online. Certifique-se de que index.html está no repositório.</h2>")

@app.get("/quiz", response_class=FileResponse)
def serve_quiz():
    quiz_file = BASE_DIR / "quiz.html"
    if quiz_file.exists():
        return FileResponse(quiz_file)
    return HTMLResponse("<h2>Quiz NutriCore Pro Online. Certifique-se de que quiz.html está no repositório.</h2>")

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
        "status": "online",
        "service": "NutriCore Pro",
        "timestamp": datetime.utcnow().isoformat()
    }

# ==========================================
# AUTENTICAÇÃO E PERFIL
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
            "message": "Usuário cadastrado com sucesso.",
            "user": {
                "id": user_id,
                "name": data.name.strip(),
                "email": data.email.lower().strip(),
                "is_pro": False
            }
        }
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Este e-mail já está cadastrado.")

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

# ==========================================
# CAPTURA DE LEADS (QUIZ)
# ==========================================
@app.post("/api/v1/lead/capture")
def capture_lead(lead: LeadCaptureInput):
    tmb, tdee, daily_cal = calculate_metabolism(
        lead.gender, lead.weight, lead.height, lead.age, lead.activity_level, lead.goal
    )

    weight_diff = abs(lead.weight - lead.target_weight)
    weeks_estimate = max(2, math.ceil(weight_diff / 0.7))

    clean_phone = re.sub(r'\D', '', lead.phone)
    if not clean_phone.startswith('55'):
        clean_phone = '55' + clean_phone

    wpp_message = (
        f"Olá {lead.name}! Seu diagnóstico do NutriCore Pro está concluído.\n"
        f"Sua meta de {lead.target_weight}kg pode ser atingida em cerca de {weeks_estimate} semanas.\n"
        f"Acesse seu plano: https://nutricore-app-1.onrender.com"
    )
    whatsapp_url = f"https://wa.me/{clean_phone}?text={urllib.parse.quote(wpp_message)}"

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
        "recovery_whatsapp_url": whatsapp_url
    }

# ==========================================
# GERAÇÃO DE DIETA COM IA (GEMINI OU FALLBACK)
# ==========================================
@app.post("/api/v1/plan/generate")
def generate_diet(data: GeneratePlanInput):
    tmb, tdee, target_calories = calculate_metabolism(
        data.gender, data.weight, data.height, data.age, data.activity_level, data.goal
    )

    if GEMINI_API_KEY:
        try:
            from google import genai
            client = genai.Client(api_key=GEMINI_API_KEY)
            
            prompt = f"""
            Gere um plano alimentar de 1 dia estruturado em JSON para um paciente:
            - Sexo: {data.gender}, Idade: {data.age}, Peso: {data.weight}kg, Altura: {data.height}cm
            - Objetivo: {data.goal}, Meta Calórica Diária: {target_calories} kcal
            - Estilo: {data.diet_style}
            - Restrições: {', '.join(data.restrictions) if data.restrictions else 'Nenhuma'}

            Retorne estritamente um JSON no formato:
            {{
              "calorias_totais": {target_calories},
              "macros": {{"proteina_g": 140, "carbo_g": 180, "gordura_g": 50, "fibras_g": 30}},
              "meta_hidratacao": "2.5 Litros/dia",
              "estilo_aplicado": "{data.diet_style}",
              "refeicoes": [
                {{"nome": "Café da Manhã", "horario": "07:30", "calorias": 400, "alimentos": ["3 Ovos mexidos", "Pão integral"], "dica_preparo": "Sem açúcar"}},
                {{"nome": "Almoço", "horario": "12:30", "calorias": 650, "alimentos": ["Frango grelhado", "Arroz", "Feijão", "Salada"], "dica_preparo": "Azeite extra virgem"}},
                {{"nome": "Lanche da Tarde", "horario": "16:30", "calorias": 250, "alimentos": ["Iogurte com aveia e frutas"], "dica_preparo": "Rico em fibras"}},
                {{"nome": "Jantar", "horario": "20:00", "calorias": 500, "alimentos": ["Tilápia grelhada com legumes"], "dica_preparo": "Refeição leve"}}
              ],
              "lista_compras": {{
                "Hortifrúti": ["Folhas verdes", "Tomates", "Frutas"],
                "Proteínas": ["Ovos", "Frango", "Peixe"],
                "Mercearia": ["Arroz integral", "Feijão", "Aveia", "Azeite"]
              }},
              "diretrizes_metabolicas": ["Mastigue devagar", "Hidrate-se constantemente"]
            }}
            """
            
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config={'response_mime_type': 'application/json'}
            )
            return json.loads(response.text)
        except Exception:
            pass

    return generate_fallback_plan(data.weight, target_calories, data.goal, data.diet_style)

# ==========================================
# PAGAMENTO PIX
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

    # Modo Sandbox
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
    rev = c.fetchone()["total"]
    total_revenue = rev if rev else 0.0
    conn.close()

    rows = ""
    for l in leads:
        clean_phone = re.sub(r'\D', '', l['phone'])
        if not clean_phone.startswith('55'):
            clean_phone = '55' + clean_phone
        msg = f"Olá {l['name']}, tudo bem? Vi seu diagnóstico metabólico no NutriCore Pro. Gostaria de tirar alguma dúvida sobre o plano?"
        wpp_url = f"https://wa.me/{clean_phone}?text={urllib.parse.quote(msg)}"

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
