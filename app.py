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

# --- 1. MODELOS DE DADOS DO PERFIL ---
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

# --- 2. MODELOS DA CONSULTA FUNCIONAL ---
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

# --- 3. CÁLCULO BIOMÉTRICO ---
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
    cal_carb = meta_calorica - (cal_prot + cal_gord)
    carboidratos_g = max(cal_carb / 4, 30.0)

    macros = Macronutrientes(
        proteinas_g=round(proteinas_g, 1),
        carboidratos_g=round(carboidratos_g, 1),
        gorduras_g=round(gorduras_g, 1),
        calorias_totais=round(meta_calorica, 0)
    )
    return round(tmb, 1), round(tdee, 1), round(meta_calorica, 1), macros

# --- 4. GERAÇÃO DE PLANO ALIMENTAR ---
def gerar_com_gemini(perfil: PerfilUsuarioInput, meta_calorica: float, macros: Macronutrientes, api_key: str) -> List[RefeicaoIA]:
    client = genai.Client(api_key=api_key)
    
    cal_ref = round(meta_calorica / perfil.refeicoes_por_dia, 0)
    prot_ref = round(macros.proteinas_g / perfil.refeicoes_por_dia, 1)
    carb_ref = round(macros.carboidratos_g / perfil.refeicoes_por_dia, 1)
    gord_ref = round(macros.gorduras_g / perfil.refeicoes_por_dia, 1)

    intolerancias_str = ", ".join(perfil.intolerancias_saude) if perfil.intolerancias_saude else "Nenhuma restrição informada."

    prompt = f"""
    Você é um Nutricionista Clínico Esportivo e Chef de Gastronomia Funcional renomado.
    Crie um plano alimentar ultra-personalizado.
    
    PERFIL BIOMÉTRICO:
    - Peso: {perfil.peso_kg} kg | Altura: {perfil.altura_cm} cm | Idade: {perfil.idade} anos | Sexo: {perfil.sexo.value}
    - Objetivo: {perfil.objetivo.value} (Ritmo: {perfil.ritmo_objetivo})
    - Padrão Alimentar: {perfil.preferencia.value} | Estilo Culinário: {perfil.estilo_culinario.value}
    - Nível Culinário: {perfil.habilidade_culinaria} | Faixa de Orçamento: {perfil.orcamento}

    CRONONUTRIÇÃO:
    - Acorda: {perfil.horario_acordar} | Dorme: {perfil.horario_dormir} | Treino: {perfil.horario_treino}
    
    RESTRIÇÕES & INTOLERÂNCIAS:
    - {intolerancias_str}
    - Favoritos: {perfil.alimentos_favoritos if perfil.alimentos_favoritos else 'Variados'}
    - Evitar: {perfil.alimentos_evitar if perfil.alimentos_evitar else 'Nenhum'}

    METAS DO DIA ({perfil.refeicoes_por_dia} refeições):
    - Calorias: {meta_calorica} kcal | Proteínas: {macros.proteinas_g}g | Carbos: {macros.carboidratos_g}g | Gorduras: {macros.gorduras_g}g

    Retorne APENAS um array JSON com {perfil.refeicoes_por_dia} objetos:
    [
      {{
        "nome_refeicao": "Ex: Café da Manhã",
        "titulo_prato": "Nome apetitoso do prato",
        "horario_sugerido": "07:30",
        "calorias_alvo": {cal_ref},
        "proteinas_refeicao_g": {prot_ref},
        "carboidratos_refeicao_g": {carb_ref},
        "gorduras_refeicao_g": {gord_ref},
        "ingredientes": ["Ex: 150g de Filé de frango marinado com alecrim", "Ex: 130g de Batata doce"],
        "modo_preparo": "Instruções claras e sucintas.",
        "dica_chef": "Dica nutricional ou gastronômica."
      }}
    ]
    """

    response = client.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.85
        )
    )

    dados = json.loads(response.text)
    return [RefeicaoIA(**ref) for ref in dados]

# --- 5. GERAÇÃO DE CONSULTA FUNCIONAL ---
def gerar_consulta_funcional(dados_input: ConsultaFuncionalInput, api_key: str) -> ConsultaFuncionalResponse:
    client = genai.Client(api_key=api_key)

    prompt = f"""
    Você é um especialista em Nutrição Clínica Funcional, Bioquímica e Fitoterapia.
    O paciente solicitou uma solução nutricional para o seguinte objetivo:
    "{dados_input.objetivo_especifico}"
    Padrão alimentar: {dados_input.preferencia}

    Crie um protocolo funcional preciso contendo:
    1. Título impactante e profissional.
    2. Explicação fisiológica sucinta.
    3. Lista de 4 a 6 Alimentos-Chave (com porção, por que funciona e como consumir).
    4. Lista de 3 a 5 Alimentos/Hábitos para EVITAR.
    5. Uma Receita Terapêutica Rápida (shot, smoothie ou chá de 2 a 5 minutos).

    Retorne APENAS um JSON no formato:
    {{
      "titulo_estrategia": "Título da Estratégia",
      "explicacao_fisiologica": "Explicação clínica.",
      "alimentos_chave": [
        {{
          "alimento": "Nome",
          "porcao_sugerida": "Porção",
          "por_que_funciona": "Mecanismo",
          "como_consumir": "Momento ideal"
        }}
      ],
      "alimentos_evitar": ["Item 1", "Item 2"],
      "receita_rapida": {{
        "titulo": "Nome da receita",
        "tempo_preparo": "X minutos",
        "ingredientes": ["Item 1", "Item 2"],
        "modo_preparo": "Instruções",
        "quando_tomar": "Momento de consumo"
      }}
    }}
    """

    response = client.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.8
        )
    )

    dados = json.loads(response.text)
    return ConsultaFuncionalResponse(**dados)

# --- 6. FASTAPI ENGINE & ROTAS ---
app = FastAPI(title="NutriCore Pro Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rota principal para servir o site na nuvem
@app.get("/")
def home():
    return FileResponse("index.html")

@app.post("/api/v1/diet/generate", response_model=PlanoAlimentarResponse)
def criar_plano(perfil: PerfilUsuarioInput):
    tmb, tdee, meta_calorica, macros = calcular_metas(perfil)
    api_key = perfil.gemini_api_key or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="Chave de acesso não informada.")
    try:
        refeicoes = gerar_com_gemini(perfil, meta_calorica, macros, api_key)
        return PlanoAlimentarResponse(tmb=tmb, tdee=tdee, meta_calorica=meta_calorica, macros=macros, refeicoes=refeicoes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar plano: {str(e)}")

@app.post("/api/v1/nutrition/consult", response_model=ConsultaFuncionalResponse)
def consultar_nutricao(dados: ConsultaFuncionalInput):
    api_key = dados.gemini_api_key or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="Chave de acesso não informada.")
    try:
        return gerar_consulta_funcional(dados, api_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar consulta funcional: {str(e)}")
