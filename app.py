import os
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import List, Optional
from enum import Enum
from google import genai
from google.genai import types

# --- 1. MODELOS DE DADOS ---
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
    idade: int = Field(..., ge=15, le=100)
    sexo: SexoEnum
    peso_kg: float = Field(..., gt=30, lt=300)
    altura_cm: float = Field(..., gt=100, lt=250)
    nivel_atividade: NivelAtividadeEnum
    objetivo: ObjetivoEnum
    ritmo_objetivo: Optional[str] = "moderado"
    preferencia: PreferenciaAlimentarEnum
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

class PlanoAlimentarResponse(BaseModel):
    tmb: float
    tdee: float
    meta_calorica: float
    macros: Macronutrientes
    refeicoes: List[RefeicaoIA]

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

# --- 2. LÓGICA E IA ---
def calcular_metas(p: PerfilUsuarioInput):
    if p.sexo == SexoEnum.MASCULINO:
        tmb = (10 * p.peso_kg) + (6.25 * p.altura_cm) - (5 * p.idade) + 5
    else:
        tmb = (10 * p.peso_kg) + (6.25 * p.altura_cm) - (5 * p.idade) - 161
    fatores = {NivelAtividadeEnum.SEDENTARIO: 1.2, NivelAtividadeEnum.LEVE: 1.375, NivelAtividadeEnum.MODERADO: 1.55, NivelAtividadeEnum.INTENSO: 1.725}
    tdee = tmb * fatores[p.nivel_atividade]
    
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

    macros = Macronutrientes(proteinas_g=round(proteinas_g, 1), carboidratos_g=round(carboidratos_g, 1), gorduras_g=round(gorduras_g, 1), calorias_totais=round(meta_calorica, 0))
    return round(tmb, 1), round(tdee, 1), round(meta_calorica, 1), macros

def gerar_com_gemini(perfil: PerfilUsuarioInput, meta_calorica: float, macros: Macronutrientes, api_key: str) -> List[RefeicaoIA]:
    client = genai.Client(api_key=api_key)
    prompt = f"Crie um plano alimentar JSON para peso {perfil.peso_kg}kg, meta {meta_calorica}kcal, macros: P{macros.proteinas_g} C{macros.carboidratos_g} G{macros.gorduras_g}. Estilo {perfil.estilo_culinario.value}."
    response = client.models.generate_content(
        model='gemini-3.7-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    dados = json.loads(response.text)
    return [RefeicaoIA(**ref) for ref in dados]

def gerar_consulta_funcional(dados_input: ConsultaFuncionalInput, api_key: str) -> ConsultaFuncionalResponse:
    client = genai.Client(api_key=api_key)
    prompt = f"Protocolo funcional para: {dados_input.objetivo_especifico}. Retorne JSON."
    response = client.models.generate_content(
        model='gemini-3.7-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    return ConsultaFuncionalResponse(**json.loads(response.text))

# --- 3. APP FASTAPI ---
app = FastAPI(title="NutriCore Pro Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/manifest.json")
def serve_manifest():
    return FileResponse("manifest.json")

@app.get("/")
def home():
    return FileResponse("index.html")

@app.post("/api/v1/diet/generate", response_model=PlanoAlimentarResponse)
def criar_plano(perfil: PerfilUsuarioInput):
    tmb, tdee, meta_calorica, macros = calcular_metas(perfil)
    api_key = perfil.gemini_api_key or os.getenv("GEMINI_API_KEY")
    if not api_key: raise HTTPException(status_code=400, detail="Chave necessária")
    refeicoes = gerar_com_gemini(perfil, meta_calorica, macros, api_key)
    return PlanoAlimentarResponse(tmb=tmb, tdee=tdee, meta_calorica=meta_calorica, macros=macros, refeicoes=refeicoes)

@app.post("/api/v1/nutrition/consult", response_model=ConsultaFuncionalResponse)
def consultar_nutricao(dados: ConsultaFuncionalInput):
    api_key = dados.gemini_api_key or os.getenv("GEMINI_API_KEY")
    if not api_key: raise HTTPException(status_code=400, detail="Chave necessária")
    return gerar_consulta_funcional(dados, api_key)
