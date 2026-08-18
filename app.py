import os
import json
import re
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional
from enum import Enum
from google import genai
from google.genai import types

# --- 1. MODELOS ---
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

class PerfilUsuarioInput(BaseModel):
    idade: int = Field(28, ge=15, le=100)
    sexo: SexoEnum = SexoEnum.MASCULINO
    peso_kg: float = Field(78.0, gt=30, lt=300)
    altura_cm: float = Field(178.0, gt=100, lt=250)
    nivel_atividade: NivelAtividadeEnum = NivelAtividadeEnum.MODERADO
    objetivo: ObjetivoEnum = ObjetivoEnum.PERDA_PESO
    ritmo_objetivo: Optional[str] = "moderado"
    preferencia: Optional[str] = "onivoro"
    estilo_culinario: Optional[str] = "caseiro_brasil"
    alimentos_favoritos: Optional[str] = ""
    alimentos_evitar: Optional[str] = ""
    intolerancias_saude: Optional[List[str]] = []
    refeicoes_por_dia: int = Field(4, ge=3, le=6)
    gemini_api_key: Optional[str] = None

class TreinoInput(BaseModel):
    nivel: str = "intermediario"
    foco: str = "hipertrofia"
    equipamento: str = "academia"
    tempo_minutos: int = 45
    gemini_api_key: Optional[str] = None

# --- 2. UTILITÁRIOS ---
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

# --- 3. APP FASTAPI ---
app = FastAPI(title="NutriCore Pro Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": f"Erro no processamento: {str(exc)}"}
    )

@app.get("/manifest.json")
def serve_manifest():
    return FileResponse("manifest.json")

@app.get("/")
def home():
    return FileResponse("index.html")

# --- 4. ROTAS ---
@app.post("/api/v1/diet/generate")
def criar_plano(perfil: PerfilUsuarioInput):
    api_key = obter_chave(perfil.gemini_api_key)
    client = genai.Client(api_key=api_key)

    if perfil.sexo == SexoEnum.MASCULINO:
        tmb = (10 * perfil.peso_kg) + (6.25 * perfil.altura_cm) - (5 * perfil.idade) + 5
    else:
        tmb = (10 * perfil.peso_kg) + (6.25 * perfil.altura_cm) - (5 * perfil.idade) - 161

    fatores = {
        NivelAtividadeEnum.SEDENTARIO: 1.2,
        NivelAtividadeEnum.LEVE: 1.375,
        NivelAtividadeEnum.MODERADO: 1.55,
        NivelAtividadeEnum.INTENSO: 1.725
    }
    tdee = tmb * fatores.get(perfil.nivel_atividade, 1.55)
    meta_calorica = tdee * 0.8 if perfil.objetivo == ObjetivoEnum.PERDA_PESO else (tdee * 1.15 if perfil.objetivo == ObjetivoEnum.HIPERTROFIA else tdee)

    macros = {
        "proteinas_g": round(perfil.peso_kg * 2.0, 1),
        "carboidratos_g": round((meta_calorica * 0.45) / 4, 1),
        "gorduras_g": round((meta_calorica * 0.25) / 9, 1),
        "calorias_totais": round(meta_calorica, 0)
    }

    prompt = f"""
    Gere um cardápio JSON com {perfil.refeicoes_por_dia} refeições para {meta_calorica}kcal:
    [
      {{
        "nome_refeicao": "Café da Manhã",
        "titulo_prato": "Nome do prato",
        "horario_sugerido": "07:30",
        "calorias_alvo": 400,
        "proteinas_refeicao_g": 25,
        "carboidratos_refeicao_g": 30,
        "gorduras_refeicao_g": 12,
        "ingredientes": ["2 ovos", "1 fatia de pão"],
        "modo_preparo": "Instruções breves",
        "dica_chef": "Dica prática"
      }}
    ]
    Retorne APENAS o JSON.
    """

    response = client.models.generate_content(
        model='gemini-3.7-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    return {
        "tmb": round(tmb, 1),
        "tdee": round(tdee, 1),
        "meta_calorica": round(meta_calorica, 1),
        "macros": macros,
        "refeicoes": extrair_json_seguro(response.text)
    }

@app.post("/api/v1/workout/generate")
def criar_treino(dados: TreinoInput):
    api_key = obter_chave(dados.gemini_api_key)
    client = genai.Client(api_key=api_key)

    prompt = f"""
    Crie um treino em JSON para nível {dados.nivel}, foco {dados.foco}, equipamento {dados.equipamento}, tempo {dados.tempo_minutos}min:
    {{
      "titulo": "Treino Personalizado",
      "foco_principal": "{dados.foco}",
      "aquecimento": [{{"nome": "Polichinelo", "series": "2", "repeticoes": "30s", "descanso": "30s", "dica_tecnica": "Ritmo constante"}}],
      "treino_principal": [{{"nome": "Supino", "series": "4", "repeticoes": "10-12", "descanso": "60s", "dica_tecnica": "Execução controlada"}}],
      "finalizacao": [{{"nome": "Prancha", "series": "3", "repeticoes": "30s", "descanso": "30s", "dica_tecnica": "Mantenha o core firme"}}]
    }}
    Retorne APENAS o JSON.
    """

    response = client.models.generate_content(
        model='gemini-3.7-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    return extrair_json_seguro(response.text)
